"""Independent, version-checked paragraph tasks; never acquire the book workflow lock."""

from __future__ import annotations

import asyncio
import threading
from time import monotonic

from wenyi_core.agents.analyzer import Analyzer
from wenyi_core.agents.polisher import Polisher
from wenyi_core.agents.translator import Translator
from wenyi_core.config import Config
from wenyi_core.ingest.segmenter import batch_segments
from wenyi_core.llm.limits import RequestStopped
from wenyi_core.pipeline.annotations import AnnotationService
from wenyi_core.pipeline.translation_batch import BatchPlan, TranslationBatchExecutor

from .. import paths
from ..config import settings
from ..db import init_pool
from ..project_service import storage_for
from ..retranslation_service import get_request, set_result


def execute_retranslation(pid: str, request_id: str, stop: threading.Event | None = None):
    from wenyi_core.llm.factory import build_client

    init_pool(settings.psycopg_dsn)
    storage = storage_for(pid)
    stop = stop or threading.Event()
    finished = threading.Event()
    client = None
    watcher = None
    started = monotonic()
    applied, conflicts = [], []
    try:
        with storage.retranslation_lock(request_id):
            request = get_request(pid, request_id)
            if not request or request["status"] != "queued":
                return
            set_result(pid, request_id, "running")
            try:
                config = Config.from_dict(
                    {**request["config_snapshot"], "paths": {"state_dir": paths.project_dir(pid)}}
                )
                manifest = storage.load_manifest()
                config.source_lang = manifest.get("source_lang") or config.source_lang
                ci = request["chapter_index"]
                chapter = storage.load_chapter(ci)
                inputs = {i["index"]: i for i in request["inputs"]}
                selected = [s for s in chapter.segments if s.index in inputs]
                client = build_client(config)
                client.set_event_sink(storage.log_event)
                operations = ["translation.body"]
                if config.pipeline.polish:
                    operations.append("polish.body")
                client.validate_credentials(operations)

                def monitor():
                    while not finished.wait(0.25):
                        if stop.is_set():
                            client.cancel()
                            return

                watcher = threading.Thread(target=monitor, daemon=True)
                watcher.start()
                executor = TranslationBatchExecutor(
                    Translator(client, config), Polisher(client, config)
                )
                style = Analyzer(client, config).style_brief(storage.load_analysis() or {})
                synopsis = (storage.load_analysis() or {}).get("book_synopsis", "")
                with client.interrupt_scope():
                    for batch in batch_segments(selected, config.segment.max_tokens_per_batch):
                        if stop.is_set():
                            raise RequestStopped("段落重译已中断，请重新提交")
                        # Skip changed text before spending another model request.
                        current = storage.load_chapter(ci)
                        by_index = {s.index: s for s in current.segments}
                        eligible = []
                        for segment in batch:
                            now = by_index.get(segment.index)
                            expected = inputs[segment.index]
                            if (
                                now is None
                                or now.source != expected["source"]
                                or now.target != expected["before"]
                                or now.meta.get("_wenyi_revision", 0)
                                != expected["expected_revision"]
                            ):
                                conflicts.append(segment.index)
                            else:
                                eligible.append(now)
                        if not eligible:
                            continue
                        positions = {s.index: i for i, s in enumerate(current.text_segments)}
                        first = positions[eligible[0].index]
                        last = positions[eligible[-1].index]
                        before = current.text_segments[max(0, first - 3) : first]
                        context = "\n\n".join(f"{s.source}\n{s.target or ''}" for s in before)
                        following = current.text_segments[last + 1 : last + 2]
                        annotations = AnnotationService.annotation_contexts_for_segments(
                            current.text_segments, storage.load_annotation_contexts()
                        )
                        terms = storage.all_terms()
                        if config.pipeline.glossary_scope == "chapter":
                            terms = storage.terms_in(
                                terms, "\n".join(s.source for s in current.text_segments)
                            )
                        plan = BatchPlan.capture(
                            ci,
                            first,
                            eligible,
                            terms,
                            context,
                            style,
                            synopsis,
                            current.meta.get("source_digest", ""),
                            [annotations[positions[s.index]] for s in eligible],
                            following[0].source if following else "",
                        )
                        result = executor.execute(plan, polish=config.pipeline.polish)
                        if stop.is_set():
                            raise RequestStopped("段落重译已中断，请重新提交")
                        replacements = {
                            s.index: {
                                **inputs[s.index],
                                "target": target,
                                "target_before_polish": before_polish,
                            }
                            for s, target, before_polish in zip(
                                eligible, result.targets, result.before_polish
                            )
                        }
                        # Publish content and its request checkpoint in one short transaction.
                        with storage.state_lock():
                            published = storage.publish_retranslations(
                                ci, replacements, request_id=request_id
                            )
                            applied.extend(published["applied"])
                            conflicts.extend(published["conflicts"])
                            with storage._conn as conn:
                                set_result(
                                    pid,
                                    request_id,
                                    "running",
                                    applied=applied,
                                    conflicts=conflicts,
                                    usage=client.usage_summary(),
                                    elapsed_seconds=monotonic() - started,
                                    connection=conn,
                                )
                set_result(
                    pid,
                    request_id,
                    "conflict" if conflicts else "done",
                    applied=applied,
                    conflicts=conflicts,
                    usage=client.usage_summary(),
                    elapsed_seconds=monotonic() - started,
                )
                storage.log_event(
                    "paragraph_retranslation_completed",
                    request_id=request_id,
                    chapter=ci,
                    applied=applied,
                    conflicts=conflicts,
                )
            except (Exception, RequestStopped, KeyboardInterrupt) as error:
                set_result(
                    pid,
                    request_id,
                    "error",
                    applied=applied,
                    conflicts=conflicts,
                    error=str(error) or "段落重译被中断，请重新提交",
                    usage=client.usage_summary() if client else None,
                    elapsed_seconds=monotonic() - started,
                )
                storage.log_event(
                    "paragraph_retranslation_failed", request_id=request_id, error=str(error)
                )
    finally:
        finished.set()
        if watcher:
            watcher.join(timeout=1)
        storage.close()


async def run_retranslation(ctx, *, project_id: str, request_id: str):
    stop = threading.Event()
    task = asyncio.create_task(
        asyncio.to_thread(execute_retranslation, project_id, request_id, stop)
    )
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        stop.set()
        await asyncio.shield(task)
        raise


async def recover_retranslations(ctx):
    from arq.jobs import Job, JobStatus

    from ..db import get_pool

    with get_pool().connection() as conn:
        rows = conn.execute("""SELECT project_id,id FROM retranslation_requests
            WHERE status IN ('queued','running') AND updated_at < now() - interval '2 minutes'""").fetchall()
    for pid, rid in rows:
        storage = storage_for(pid)
        try:
            with storage.retranslation_lock(rid, blocking=False):
                current = get_request(pid, rid)
                if not current or current["status"] not in {"queued", "running"}:
                    continue
                if current["status"] == "queued":
                    remote = await Job(rid, ctx["redis"], _queue_name="wenyi:exports").status()
                    if remote in {JobStatus.queued, JobStatus.deferred, JobStatus.in_progress}:
                        continue
                set_result(
                    pid,
                    rid,
                    "error",
                    error="重译 Worker 已中断，已写回的段落已保留；其余段落请重新提交",
                )
        except BlockingIOError:
            continue
        finally:
            storage.close()
