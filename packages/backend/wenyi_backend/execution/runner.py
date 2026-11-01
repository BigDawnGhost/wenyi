"""Own workflow identity, cancellation, telemetry and cleanup independently of scheduling."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import traceback
from contextlib import ExitStack, closing
from contextvars import copy_context
from functools import partial

from wenyi_core.llm.base import LLMClient
from wenyi_core.llm.limits import RequestCancelled, RequestStopped

from .. import metrics
from ..emitters import publish_error, redis_progress_fn
from ..live_statistics import LiveStatistics
from .contracts import ExecutionDependencies, ExecutionRequest

log = logging.getLogger(__name__)
# Bound the persisted traceback so one large failure cannot bloat the event ledger.
TRACEBACK_LIMIT = 4096


def error_code(error: BaseException) -> str:
    """Map a pipeline failure onto the same codes the HTTP layer uses."""
    code = getattr(error, "code", None)
    if isinstance(code, str) and code:
        return code
    return "pipeline_failed"


class PauseRequested(KeyboardInterrupt):
    """Stop at a persisted boundary, using the core's interruption recovery path."""


def record_terminal_status(
    request: ExecutionRequest,
    dependencies: ExecutionDependencies,
    error: BaseException | None = None,
    *,
    status: str = "error",
) -> None:
    """A late Future or duplicate delivery may update only its own task's project."""
    projects, jobs = dependencies.projects, dependencies.jobs
    pid, run_id = request.project_id, request.run_id
    message = str(error) if error is not None else None
    job = jobs.get_job_by_arq_id(run_id) if run_id else None
    if job:
        if job["project_id"] != pid:
            return
        try:
            latest = next((item for item in jobs.list_jobs(pid) if item["kind"] != "export"), None)
            if not latest or latest["id"] != job["id"]:
                return
            project = projects.get_project(pid)
            if (
                job["status"] == status
                and project
                and project["status"] == status
                and project.get("error") == message
            ):
                return
            # Wait for a brief API write boundary rather than dropping the terminal status.
            with closing(dependencies.storage_for(pid)) as storage, storage.lock():
                latest = next(
                    (item for item in jobs.list_jobs(pid) if item["kind"] != "export"), None
                )
                if latest and latest["id"] == job["id"]:
                    projects.set_project_status(pid, status, error=message)
        finally:
            jobs.set_job_status(job["id"], status, error=message)
    elif not run_id:
        projects.set_project_status(pid, status, error=message)


def execute(
    request: ExecutionRequest,
    dependencies: ExecutionDependencies,
    stop: threading.Event | None = None,
) -> None:
    """Run one synchronous workflow, retaining ownership until all callbacks are drained."""
    projects, jobs = dependencies.projects, dependencies.jobs
    pid, run_id, kind = request.project_id, request.run_id, request.kind
    stop = stop or threading.Event()
    finished = threading.Event()
    client: LLMClient | None = None

    def monitor_pause() -> None:
        while not finished.wait(0.25):
            try:
                if projects.is_paused(pid):
                    stop.set()
                if stop.is_set() and client is not None:
                    client.cancel()
            except Exception:
                # A transient database outage is handled by the operation itself.
                continue

    with ExitStack() as resources:
        storage = resources.enter_context(closing(dependencies.storage_for(pid)))
        cache = dependencies.telemetry.connect()
        resources.callback(dependencies.telemetry.release, cache)
        watcher = threading.Thread(target=copy_context().run, args=(monitor_pause,), daemon=True)
        watcher.start()

        def stop_watcher() -> None:
            if not finished.is_set():
                finished.set()
                watcher.join(timeout=1)

        resources.callback(stop_watcher)
        emitter = redis_progress_fn(cache, pid, kind=kind, run_id=run_id)

        def progress(done: int, total: int, label: str) -> None:
            if stop.is_set() or projects.is_paused(pid):
                if client is not None:
                    client.cancel()
                raise PauseRequested(pid)
            emitter(done, total, label)

        job = None
        try:
            with storage.lock(), ExitStack() as scope:
                if run_id:
                    current = jobs.get_job_by_arq_id(run_id)
                    latest = next(
                        (item for item in jobs.list_jobs(pid) if item["kind"] != "export"), None
                    )
                    if (
                        not current
                        or current["project_id"] != pid
                        or current["kind"] != kind
                        or not latest
                        or current["id"] != latest["id"]
                        or current["status"] != "queued"
                    ):
                        return
                    job = current
                    jobs.set_job_status(job["id"], "running")
                storage.recover_usage()
                live = scope.enter_context(LiveStatistics(cache, pid, run_id or "", storage))

                def bind_client(value: LLMClient) -> None:
                    nonlocal client
                    client = value
                    live.bind_client(value)
                    value.set_event_sink(storage.log_event)
                    if stop.is_set():
                        value.cancel()

                progress(0, 0, "任务启动")
                result_status = dependencies.operation(request, storage, progress, bind_client)
                progress(1, 1, "任务完成")
                storage.log_event("task_completed", kind=kind, run_id=run_id)
            # Publish completion only after telemetry and the run lock have exited.
            projects.set_project_status(pid, result_status)
            if job:
                jobs.set_job_status(job["id"], "done")
        except (KeyboardInterrupt, RequestCancelled):
            record_terminal_status(request, dependencies, status="paused")
            storage.log_event("task_paused", kind=kind, run_id=run_id)
        except RequestStopped as error:
            record_terminal_status(request, dependencies, error, status="paused")
            storage.log_event("task_paused", kind=kind, run_id=run_id, reason=str(error))
        except Exception as error:
            # Provider credentials are stripped before they reach this point; the
            # traceback stays on the operator side and is never returned to clients.
            log.exception("Pipeline task failed: kind=%s run_id=%s", kind, run_id)
            record_terminal_status(request, dependencies, error)
            storage.log_event(
                "pipeline_error",
                kind=kind,
                run_id=run_id,
                error=str(error),
                error_type=type(error).__name__,
                traceback=traceback.format_exc()[-TRACEBACK_LIMIT:],
            )
            # Tell live subscribers the run is over; failure here must not mask the
            # original error, so publish_error swallows transport problems.
            publish_error(cache, pid, code=error_code(error), message=str(error), run_id=run_id)
            # Observability only: the counter swallows its own failures.
            metrics.record_task(status="error", code=error_code(error))
            raise
        finally:
            stop_watcher()
            try:
                cache.publish(
                    f"project:{pid}",
                    json.dumps({"kind": "state", "project_id": pid, "run_id": run_id}),
                )
            except Exception:
                pass


async def run(request: ExecutionRequest, dependencies: ExecutionDependencies) -> None:
    stop = threading.Event()
    task = asyncio.create_task(dependencies.run_sync(partial(execute, request, dependencies, stop)))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        # Timeout/shutdown must drain synchronous checkpoint writes before releasing the slot.
        stop.set()
        await asyncio.shield(task)
        raise
    except Exception as error:
        # Startup may fail before the synchronous runner can enter its recovery scope.
        await asyncio.to_thread(record_terminal_status, request, dependencies, error)
        raise
