"""Arq jobs: run synchronous domain services with PostgreSQL state and safe pauses."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from uuid import uuid4

from wenyi_core.llm.limits import RequestCancelled, RequestStopped

from .. import dal, paths
from ..config import settings
from ..db import init_pool
from ..emitters import redis_progress_fn
from ..export_retention import publish_export
from ..project_service import effective_config
from ..storage_pg import PostgresStorage


class PauseRequested(KeyboardInterrupt):
    """Stop at a persisted boundary, using the core's interruption recovery path."""


def _resolve_source(pid: str) -> str:
    project = dal.get_project(pid)
    if project is None:
        raise ValueError(f"Project not found: {pid}")
    if not project or not project.get("source_path"):
        raise ValueError("Project has no uploaded source")
    source = Path(project["source_path"])
    if not source.is_absolute():
        source = Path(settings.data_dir) / source
    if not source.is_file():
        raise ValueError("Uploaded source file is missing")
    return str(source)


def _pipeline_storage(pid: str, pool) -> PostgresStorage:
    return PostgresStorage(pid, pool, run_dir=paths.project_dir(pid))


def _build_config_for(pid: str, run_id: str | None = None):
    from wenyi_core.config import Config

    project = dal.get_project(pid)
    if project is None:
        raise ValueError("Project does not exist")
    job = dal.get_job_by_arq_id(run_id) if run_id else None
    snapshot = ((job or {}).get("params") or {}).get("config_snapshot")
    if snapshot:
        return Config.from_dict({**snapshot, "paths": {"state_dir": paths.project_dir(pid)}})
    return effective_config(project)


def _parse_source(pid, storage, config, progress):
    from wenyi_core.pipeline.preparation import PreparationService

    source = _resolve_source(pid)
    project = dal.get_project(pid)
    if project is None:
        raise ValueError(f"Project not found: {pid}")
    progress(0, 1, "解析原文")
    if project["fmt"] == "srt":
        from wenyi_core.ingest.srt_reader import parse_srt

        cues = parse_srt(source)
        storage.write_artifact(
            "subtitle_preview.json",
            [{"index": cue.index, "timestamp": cue.timestamp, "source": cue.text} for cue in cues],
        )
        preview = {
            "title": (project.get("source_meta") or {}).get("original_filename", "Subtitles"),
            "fmt": "srt",
            "chapter_count": 0,
            "total_word_count": len(cues),
            "source_lang": config.source_lang,
            "chapters": [],
        }
    else:
        from wenyi_core.ingest.segmenter import load_document
        from wenyi_core.pipeline.runstore import source_sha256

        digest = source_sha256(source)
        doc = PreparationService.load_parsed_document(storage, source, config, actual_sha256=digest)
        if doc is None:
            doc = load_document(
                source,
                config.source_lang,
                config.target_lang,
                split_segments=config.segment.max_tokens_per_segment,
                cache_dir=storage.source_dir,
                source_hash=digest,
                pdf_backend=config.pipeline.pdf_backend,
                babeldoc_bridge_url=config.pipeline.babeldoc_bridge_url,
                babeldoc_pages=config.pipeline.babeldoc_pages,
                babeldoc_timeout=config.pipeline.babeldoc_timeout,
            )
            if source_sha256(source) != digest:
                raise ValueError("Source changed while generating preview")
            storage.write_artifact(
                "parsed_document.json",
                {
                    "source_sha256": digest,
                    "ingest_config": PreparationService.ingest_config(config),
                    "document": doc.model_dump(mode="json"),
                },
            )
        chapters = [
            {
                "index": chapter.index,
                "title": chapter.title,
                "word_count": len(chapter.text_segments),
            }
            for chapter in doc.chapters
        ]
        preview = {
            "title": doc.title,
            "fmt": project["fmt"],
            "chapter_count": len(chapters),
            "total_word_count": sum(row["word_count"] for row in chapters),
            "source_lang": doc.source_lang,
            "chapters": chapters,
        }
    storage.write_artifact("preview.json", preview)
    progress(1, 1, "原文预览已就绪")
    return "uploaded"


def _book_operation(kind, pid, storage, config, client, progress, params):
    from wenyi_core.pipeline.orchestrator import Orchestrator

    if params.get("autofix") is not None:
        config.pipeline.review_autofix = params["autofix"]
    orch = Orchestrator(config, client=client, storage=storage)
    source = _resolve_source(pid)
    if kind == "prepare":
        if params.get("preview"):
            _parse_source(pid, storage, config, progress)
        orch.prepare_for_translation(source, progress=progress)
        return "prepared"
    if kind == "chapter_translation":
        orch.run(source, only_chapter=params["chapter_index"], progress=progress)
        return "done" if not storage.pending_chapters() else "prepared"
    if kind == "review":
        orch.run_review(source, progress=progress)
        orch.run_report(source)
        return "reviewed"
    steps = {"translate", "report"}
    if config.pipeline.review:
        steps.add("review")
    orch.run_steps(source, steps, progress=progress)
    return "done"


def _record_terminal_status(pid, run_id, error: BaseException | None = None, *, status="error"):
    """A late Future or duplicate delivery may update only its own task's project."""
    message = str(error) if error is not None else None
    job = dal.get_job_by_arq_id(run_id) if run_id else None
    if job:
        dal.set_job_status(job["id"], status, error=message)
        storage = _pipeline_storage(pid, init_pool(settings.psycopg_dsn))
        latest = next((item for item in dal.list_jobs(pid) if item["kind"] != "export"), None)
        if not latest or latest["id"] != job["id"]:
            return
        # Brief API reads may own the advisory lock while rejecting a busy project.
        # Wait for that boundary instead of dropping the terminal project status.
        with storage.lock():
            latest = next((item for item in dal.list_jobs(pid) if item["kind"] != "export"), None)
            if latest and latest["id"] == job["id"]:
                dal.set_project_status(pid, status, error=message)
    elif not run_id:
        dal.set_project_status(pid, status, error=message)


def _execute(
    kind: str, pid: str, run_id: str | None, params: dict, stop: threading.Event | None = None
) -> None:
    import redis as redis_lib
    from wenyi_core.llm.factory import build_client

    pool = init_pool(settings.psycopg_dsn)
    storage = _pipeline_storage(pid, pool)
    redis = redis_lib.from_url(settings.redis_url)
    job = dal.get_job_by_arq_id(run_id) if run_id else None
    client = None
    stop = stop or threading.Event()
    finished = threading.Event()

    def monitor_pause():
        while not finished.wait(0.25):
            try:
                if dal.is_paused(pid):
                    stop.set()
                if stop.is_set() and client is not None:
                    client.cancel()
            except Exception:
                # A transient database outage is handled by the operation itself.
                continue

    watcher = threading.Thread(target=monitor_pause, daemon=True)
    watcher.start()
    emitter = redis_progress_fn(redis, pid, kind=kind, run_id=run_id)

    def progress(done, total, label):
        if stop.is_set() or (dal.get_project(pid) or {}).get("status") in {"pausing", "paused"}:
            if client is not None:
                client.cancel()
            raise PauseRequested(pid)
        emitter(done, total, label)

    try:
        with storage.lock():
            if run_id:
                current = dal.get_job_by_arq_id(run_id)
                latest = next(
                    (item for item in dal.list_jobs(pid) if item["kind"] != "export"), None
                )
                if (
                    not current
                    or not latest
                    or current["id"] != latest["id"]
                    or current["status"] != "queued"
                ):
                    return
                job = current
                dal.set_job_status(job["id"], "running")
            progress(0, 0, "任务启动")
            config = _build_config_for(pid, run_id)
            if kind == "parse":
                result_status = _parse_source(pid, storage, config, progress)
            else:
                client = build_client(config)
                client.set_event_sink(storage.log_event)
                with client.interrupt_scope():
                    if kind == "srt":
                        from wenyi_core.srt.translate import translate_srt

                        client.validate_credentials(("srt.translate",))
                        output_dir = Path(paths.exports_dir(pid)) / (run_id or uuid4().hex)
                        output_dir.mkdir(parents=True, exist_ok=True)
                        base = output_dir / f"subtitles.{config.target_lang}.srt"
                        result = translate_srt(
                            _resolve_source(pid),
                            config,
                            client=client,
                            out=str(base),
                            progress=progress,
                            storage=storage,
                        )
                        for output in result["outputs"]:
                            eid = dal.create_export(pid, "srt", {"bilingual": "-bi.srt" in output})
                            publish_export(
                                pool,
                                pid,
                                eid,
                                output,
                                data_dir=settings.data_dir,
                            )
                        result_status = "done"
                    else:
                        from wenyi_core.llm.operations import configured_operations

                        if params.get("autofix") is not None:
                            config.pipeline.review_autofix = params["autofix"]
                        client.validate_credentials(
                            configured_operations(
                                config,
                                "review"
                                if kind == "review"
                                else "prepare"
                                if kind == "prepare"
                                else "translate",
                            )
                        )
                        result_status = _book_operation(
                            kind, pid, storage, config, client, progress, params
                        )
            progress(1, 1, "任务完成")
            dal.set_project_status(pid, result_status)
            if job:
                dal.set_job_status(job["id"], "done")
            storage.log_event("task_completed", kind=kind, run_id=run_id)
    except (KeyboardInterrupt, RequestCancelled):
        _record_terminal_status(pid, run_id, status="paused")
        storage.log_event("task_paused", kind=kind, run_id=run_id)
    except RequestStopped as error:
        _record_terminal_status(pid, run_id, error, status="paused")
        storage.log_event("task_paused", kind=kind, run_id=run_id, reason=str(error))
    except Exception as error:
        _record_terminal_status(pid, run_id, error)
        storage.log_event("pipeline_error", kind=kind, run_id=run_id, error=str(error))
        raise
    finally:
        finished.set()
        watcher.join(timeout=1)
        redis.close()
        storage.close()


async def _run(kind, project_id, run_id, params):
    stop = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(_execute, kind, project_id, run_id, params, stop))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        # Arq timeout/shutdown must stop the synchronous pipeline before releasing its slot.
        stop.set()
        await asyncio.shield(task)
        raise
    except Exception as error:
        _record_terminal_status(project_id, run_id, error)
        raise


async def run_parse(ctx, *, project_id: str, run_id: str | None = None, **params):
    await _run("parse", project_id, run_id, params)


async def run_prepare(ctx, *, project_id: str, run_id: str | None = None, **params):
    await _run("prepare", project_id, run_id, params)


async def run_translation(ctx, *, project_id: str, run_id: str | None = None, **params):
    await _run("translation", project_id, run_id, params)


async def run_chapter_translation(ctx, *, project_id: str, run_id: str | None = None, **params):
    await _run("chapter_translation", project_id, run_id, params)


async def run_review(ctx, *, project_id: str, run_id: str | None = None, **params):
    await _run("review", project_id, run_id, params)


async def run_srt(ctx, *, project_id: str, run_id: str | None = None, **params):
    await _run("srt", project_id, run_id, params)


def _render_export_sync(
    pid: str,
    *,
    export_id: int,
    fmt: str,
    run_id: str | None = None,
    bilingual: bool = False,
    order: str = "target_first",
    about_page: bool = True,
    preserve_source_style: bool = False,
    punctuation_normalize: bool | None = None,
    pdf_engine: str = "weasyprint",
) -> int:
    from wenyi_core.assemble.writer import assemble
    from wenyi_core.pipeline.runstore import source_sha256

    pool = init_pool(settings.psycopg_dsn)
    storage = _pipeline_storage(pid, pool)
    source = _resolve_source(pid)
    config = _build_config_for(pid, run_id)
    project = dal.get_project(pid)
    if project is None:
        raise ValueError(f"Project not found: {pid}")
    original = Path(
        (project.get("source_meta") or {}).get("original_filename") or "translation"
    ).stem
    out_dir = Path(paths.exports_dir(pid)) / str(export_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "md" if fmt == "markdown" else fmt
    out_path = str(
        out_dir / f"{original}.{config.target_lang}{'-bi' if bilingual else ''}.{suffix}"
    )
    if fmt == "srt":
        if source_sha256(source) != project.get("source_sha256"):
            raise ValueError("Subtitle source no longer matches project state")
        from wenyi_core.assemble.srt_writer import write_srt_outputs
        from wenyi_core.ingest.srt_reader import parse_srt
        from wenyi_core.srt.store import SrtRunStore

        with storage.state_lock():
            srt = SrtRunStore(storage.run_dir, storage=storage)
            rows = srt.load_cues()
            translations = srt.translations_from_cues(rows)
        if not translations:
            raise ValueError("No translated subtitles are available")
        write_srt_outputs(
            parse_srt(source),
            translations,
            mono_path=None if bilingual else out_path,
            bilingual_path=out_path if bilingual else None,
        )
    else:
        snapshot = storage.create_export_snapshot(actual_sha256=source_sha256(source))
        with storage.assemble_lock():
            assemble(
                snapshot,
                source,
                out_path=out_path,
                out_format=fmt,
                bilingual=bilingual,
                order=order,
                about_page=about_page,
                preserve_source_style=preserve_source_style,
                punctuation_normalize=config.output.punctuation_normalize
                if punctuation_normalize is None
                else punctuation_normalize,
                pdf_engine=pdf_engine,
                babeldoc_timeout=config.pipeline.babeldoc_timeout,
            )
    publish_export(
        pool,
        pid,
        export_id,
        out_path,
        data_dir=settings.data_dir,
    )
    return export_id


def _export_sync(pid, *, export_id, run_id=None, **params):
    pool = init_pool(settings.psycopg_dsn)
    storage = _pipeline_storage(pid, pool)
    job = dal.get_job_by_arq_id(run_id) if run_id else None
    try:
        with storage.export_lock(export_id):
            if run_id:
                job = dal.get_job_by_arq_id(run_id)
                if (
                    not job
                    or job["status"] != "queued"
                    or job["project_id"] != pid
                    or job["params"].get("export_id") != export_id
                ):
                    return export_id
                dal.set_job_status(job["id"], "running")
            dal.set_export_status(export_id, "running")
            try:
                result = _render_export_sync(pid, export_id=export_id, run_id=run_id, **params)
                if job:
                    dal.set_job_status(job["id"], "done")
                return result
            except Exception as error:
                if job:
                    dal.set_job_status(job["id"], "error", error=str(error))
                dal.set_export_status(export_id, "error", error=str(error))
                raise
    finally:
        storage.close()


async def run_export(
    ctx, *, project_id: str, export_id: int, run_id: str | None = None, **params
) -> int:
    task = asyncio.create_task(
        asyncio.to_thread(_export_sync, project_id, export_id=export_id, run_id=run_id, **params)
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Rendering uses a stable snapshot. Finish and publish it before the worker
        # releases its slot, avoiding orphan threads or lost completed files.
        await asyncio.shield(task)
        raise
    except Exception as error:
        dal.set_export_status(export_id, "error", error=str(error))
        job = dal.get_job_by_arq_id(run_id) if run_id else None
        if job:
            dal.set_job_status(job["id"], "error", error=str(error))
        raise
