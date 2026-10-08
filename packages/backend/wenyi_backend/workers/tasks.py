"""Run synchronous domain services through the configured storage and telemetry ports."""

from __future__ import annotations

import asyncio
import json
import threading
from contextlib import ExitStack
from contextvars import copy_context
from functools import wraps
from pathlib import Path
from typing import Literal
from uuid import uuid4

from wenyi_core.llm.limits import RequestCancelled, RequestStopped

from .. import dal, paths
from ..context import current_context, use_context
from ..emitters import redis_progress_fn
from ..live_statistics import LiveStatistics
from ..project_service import effective_config, storage_for
from ..source_view import book_preview


class PauseRequested(KeyboardInterrupt):
    """Stop at a persisted boundary, using the core's interruption recovery path."""


def _data_dir() -> str:
    return paths.data_dir()


def _resolve_source(pid: str) -> str:
    project = dal.get_project(pid)
    if project is None:
        raise ValueError(f"Project not found: {pid}")
    if not project or not project.get("source_path"):
        raise ValueError("Project has no uploaded source")
    source = Path(project["source_path"])
    if not source.is_absolute():
        source = Path(_data_dir()) / source
    if not source.is_file():
        raise ValueError("Uploaded source file is missing")
    return str(source)


def _pipeline_storage(pid: str):
    return storage_for(pid)


def _worker_entry(function):
    @wraps(function)
    async def run(ctx, **kwargs):
        with use_context(ctx["backend"]):
            return await function(ctx, **kwargs)

    return run


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
    from wenyi_core.pipeline.input_preparation import parse_document

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
        doc = parse_document(
            storage, source, config, mineru_token_resolver=current_context().mineru_token_resolver
        )
        preview = book_preview(doc, project["fmt"])
    storage.write_artifact("preview.json", preview)
    progress(1, 1, "原文预览已就绪")
    return "uploaded"


def _book_operation(kind, pid, storage, config, client, progress, params):
    from wenyi_core.pipeline.orchestrator import Orchestrator

    if params.get("autofix") is not None:
        config.pipeline.review_autofix = params["autofix"]
    orch = Orchestrator(
        config,
        client=client,
        storage=storage,
        mineru_token_resolver=current_context().mineru_token_resolver,
    )
    source = _resolve_source(pid)
    if kind == "prepare":
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
        storage = _pipeline_storage(pid)
        try:
            latest = next((item for item in dal.list_jobs(pid) if item["kind"] != "export"), None)
            if not latest or latest["id"] != job["id"]:
                return
            # Wait for a brief API write boundary rather than dropping the terminal status.
            with storage.lock():
                latest = next(
                    (item for item in dal.list_jobs(pid) if item["kind"] != "export"), None
                )
                if latest and latest["id"] == job["id"]:
                    dal.set_project_status(pid, status, error=message)
        finally:
            storage.close()
    elif not run_id:
        dal.set_project_status(pid, status, error=message)


def _execute(
    kind: str, pid: str, run_id: str | None, params: dict, stop: threading.Event | None = None
) -> None:
    context = current_context()
    storage = _pipeline_storage(pid)
    redis = context.telemetry.connect()
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

    watcher_scope = copy_context()
    watcher = threading.Thread(target=watcher_scope.run, args=(monitor_pause,), daemon=True)
    watcher.start()
    emitter = redis_progress_fn(redis, pid, kind=kind, run_id=run_id)

    def progress(done, total, label):
        if stop.is_set() or (dal.get_project(pid) or {}).get("status") in {"pausing", "paused"}:
            if client is not None:
                client.cancel()
            raise PauseRequested(pid)
        emitter(done, total, label)

    try:
        with storage.lock(), ExitStack() as scope:
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
            storage.recover_usage()
            live = scope.enter_context(LiveStatistics(redis, pid, run_id or "", storage))
            progress(0, 0, "任务启动")
            config = _build_config_for(pid, run_id)
            if kind == "parse":
                result_status = _parse_source(pid, storage, config, progress)
            else:
                if kind == "prepare" and not storage.exists():
                    # Source inspection must survive missing credentials or failed AI preparation.
                    _parse_source(pid, storage, config, progress)
                client = context.build_client(config)
                live.bind_client(client)
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
                            context.exports.publish_export(
                                pid,
                                eid,
                                output,
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
        try:
            redis.publish(
                f"project:{pid}",
                json.dumps(
                    {
                        "kind": "state",
                        "project_id": pid,
                        "run_id": run_id,
                    }
                ),
            )
        except Exception:
            pass
        context.telemetry.release(redis)
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


@_worker_entry
async def run_parse(ctx, *, project_id: str, run_id: str | None = None, **params):
    await _run("parse", project_id, run_id, params)


@_worker_entry
async def run_prepare(ctx, *, project_id: str, run_id: str | None = None, **params):
    await _run("prepare", project_id, run_id, params)


@_worker_entry
async def run_translation(ctx, *, project_id: str, run_id: str | None = None, **params):
    await _run("translation", project_id, run_id, params)


@_worker_entry
async def run_chapter_translation(ctx, *, project_id: str, run_id: str | None = None, **params):
    await _run("chapter_translation", project_id, run_id, params)


@_worker_entry
async def run_review(ctx, *, project_id: str, run_id: str | None = None, **params):
    await _run("review", project_id, run_id, params)


@_worker_entry
async def run_srt(ctx, *, project_id: str, run_id: str | None = None, **params):
    await _run("srt", project_id, run_id, params)


def _render_export_sync(
    pid: str,
    *,
    export_id: int,
    fmt: str,
    run_id: str | None = None,
    bilingual: bool = False,
    order: Literal["target_first", "source_first"] = "target_first",
    about_page: bool = True,
    preserve_source_style: bool = False,
    punctuation_normalize: bool | None = None,
    pdf_engine: str = "weasyprint",
) -> int:
    from wenyi_core.assemble.policy import export_plan
    from wenyi_core.assemble.writer import assemble
    from wenyi_core.pipeline.runstore import source_sha256
    from wenyi_core.storage.language_policies import persist_plan

    from ..export_plan import export_filename

    storage = _pipeline_storage(pid)
    try:
        source = _resolve_source(pid)
        source_digest = source_sha256(source)
        config = _build_config_for(pid, run_id)
        project = dal.get_project(pid)
        if project is None:
            raise ValueError(f"Project not found: {pid}")
        out_dir = Path(paths.exports_dir(pid)) / str(export_id)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = str(out_dir / export_filename(project, config.target_lang, fmt, bilingual))
        if fmt == "srt":
            if source_sha256(source) != project.get("source_sha256"):
                raise ValueError("Subtitle source no longer matches project state")
            from wenyi_core.assemble.srt_writer import write_srt_outputs
            from wenyi_core.ingest.srt_reader import parse_srt
            from wenyi_core.srt.policy import export_plan as srt_export_plan
            from wenyi_core.srt.store import SrtRunStore

            with storage.state_lock():
                srt = SrtRunStore(storage.run_dir, storage=storage)
                rows = srt.load_cues()
                translations = srt.translations_from_cues(rows)
                manifest = srt.load_manifest()
            if not translations:
                raise ValueError("No translated subtitles are available")
            cues = parse_srt(source)
            persist_plan(
                storage, srt_export_plan(config, manifest, cues, translations, bilingual=bilingual)
            )
            write_srt_outputs(
                cues,
                translations,
                mono_path=None if bilingual else out_path,
                bilingual_path=out_path if bilingual else None,
            )
        else:
            snapshot = storage.create_export_snapshot(actual_sha256=source_digest)
            with storage.assemble_lock():
                plan = export_plan(
                    snapshot,
                    fmt,
                    pdf_engine=pdf_engine,
                    punctuation_normalize=config.output.punctuation_normalize
                    if punctuation_normalize is None
                    else punctuation_normalize,
                    bilingual=bilingual,
                    order=order,
                    preserve_source_style=preserve_source_style,
                    about_page=about_page,
                )
                persist_plan(storage, plan)
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
                    language_policy=plan,
                )
        if source_sha256(source) != source_digest:
            raise ValueError("Source changed during export; ensure the file is stable and retry")
        current_context().exports.publish_export(
            pid,
            export_id,
            out_path,
        )
        return export_id
    finally:
        storage.close()


def _export_sync(pid, *, export_id, run_id=None, **params):
    storage = _pipeline_storage(pid)
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


@_worker_entry
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
