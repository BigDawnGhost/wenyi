"""Own workflow identity, cancellation, telemetry and cleanup independently of scheduling."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from contextlib import ExitStack, closing
from contextvars import copy_context
from dataclasses import dataclass
from functools import partial
from typing import TypeVar

from wenyi_core.events import ProgressPayload
from wenyi_core.llm.base import LLMClient
from wenyi_core.llm.limits import RequestCancelled, RequestStopped

from ..emitters import publish_error, publish_outage_pause, redis_progress_fn
from ..live_statistics import LiveStatistics
from .contracts import ExecutionDependencies, ExecutionRequest
from .outage import (
    ProviderOutageExhausted,
    auto_resume_exhausted,
    auto_resume_schedule,
    is_outage_interrupt,
    outage_attempt,
)

log = logging.getLogger(__name__)
CANCEL_DRAIN_WARNING_SECONDS = 30.0
_T = TypeVar("_T")


async def drain(future: asyncio.Future[_T]) -> _T:
    """Wait for an owned operation after cancellation, including repeated requests to cancel."""
    while not future.done():
        try:
            # Unlike a direct await, this cannot cancel the future it observes.
            await asyncio.wait((future,))
        except asyncio.CancelledError:
            continue
    return future.result()


def error_code(error: BaseException) -> str:
    """Map a pipeline failure onto the same codes the HTTP layer uses."""
    code = getattr(error, "code", None)
    if isinstance(code, str) and code:
        return code
    return "pipeline_failed"


class PauseRequested(KeyboardInterrupt):
    """Stop at a persisted boundary, using the core's interruption recovery path."""


@dataclass(frozen=True)
class OutagePause:
    """Automatic retry scheduled after a resumable provider outage paused the run."""

    attempt: int
    delay_seconds: int
    next_retry_at: str


@dataclass(frozen=True)
class WorkflowOutcome:
    status: str
    project_status: str | None = None
    error: BaseException | None = None
    outage: OutagePause | None = None


def record_terminal_status(
    request: ExecutionRequest,
    dependencies: ExecutionDependencies,
    error: BaseException | None = None,
    *,
    status: str = "error",
    project_status: str | None = None,
) -> bool:
    """Finalize the job; update the project only if this is still its active run."""
    projects, jobs = dependencies.projects, dependencies.jobs
    pid, run_id = request.project_id, request.run_id
    project_status = project_status or status
    message = str(error) if error is not None else None
    job = jobs.get_job_by_arq_id(run_id) if run_id else None
    if job:
        if (
            job["project_id"] != pid
            or job["kind"] != request.kind
            or job["status"] not in {"queued", "running"}
        ):
            return False
        updated_project = False
        finalize_job = True
        try:
            latest = next((item for item in jobs.list_jobs(pid) if item["kind"] != "export"), None)
            if not latest or latest["id"] != job["id"]:
                return False
            # Wait for a brief API write boundary rather than dropping the terminal status.
            with closing(dependencies.storage_for(pid)) as storage, storage.lock():
                latest = next(
                    (item for item in jobs.list_jobs(pid) if item["kind"] != "export"), None
                )
                if latest and latest["id"] == job["id"]:
                    finalize_job = latest["status"] in {"queued", "running"}
                    if finalize_job:
                        projects.set_project_status(pid, project_status, error=message)
                        updated_project = True
        finally:
            if finalize_job:
                jobs.set_job_status(job["id"], status, error=message)
        return updated_project
    if not run_id:
        projects.set_project_status(pid, project_status, error=message)
        return True
    return False


def execute(
    request: ExecutionRequest,
    dependencies: ExecutionDependencies,
    stop: threading.Event | None = None,
) -> WorkflowOutcome | None:
    """Run one checkpoint writer; return its outcome only after releasing its resources."""
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
        # A model response during this job ends any outage streak carried in from earlier
        # automatic resumes. Progress counts are not used: resuming Review reports rising
        # chapter counts while it only reloads saved state.
        provider_answered = threading.Event()

        def note_provider_answer(event: str, **_data: object) -> None:
            if event == "llm_request_completed":
                provider_answered.set()

        def progress(
            done: int, total: int, label: str, payload: ProgressPayload | None = None
        ) -> None:
            if stop.is_set() or projects.is_paused(pid):
                if client is not None:
                    client.cancel()
                raise PauseRequested(pid)
            emitter(done, total, label, payload)

        with storage.lock():
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
                    return None
                jobs.set_job_status(current["id"], "running")
            try:
                with ExitStack() as scope:
                    storage.recover_usage()
                    live = scope.enter_context(LiveStatistics(cache, pid, run_id or "", storage))

                    def bind_client(value: LLMClient) -> None:
                        nonlocal client
                        client = value
                        live.bind_client(value)
                        value.set_event_sink(storage.log_event)
                        scope.enter_context(value.observe_events(dependencies.metrics.record_event))
                        scope.enter_context(value.observe_events(note_provider_answer))
                        if stop.is_set():
                            value.cancel()

                    progress(0, 0, "progress.stage.taskStart")
                    result = dependencies.operation(request, storage, progress, bind_client)
                    progress(1, 1, "progress.stage.taskDone")
                    storage.log_event("task_completed", kind=kind, run_id=run_id)
                return WorkflowOutcome("done", project_status=result)
            except (KeyboardInterrupt, RequestCancelled):
                storage.log_event("task_paused", kind=kind, run_id=run_id)
                return WorkflowOutcome("paused")
            except RequestStopped as error:
                storage.log_event("task_paused", kind=kind, run_id=run_id, reason=str(error))
                return WorkflowOutcome("paused", error=error)
            except BaseException as error:
                original = error
                outage = is_outage_interrupt(error)
                attempt = 0 if provider_answered.is_set() else outage_attempt(request.params)
                if outage and auto_resume_exhausted(attempt):
                    error = ProviderOutageExhausted(original, attempts=attempt)
                elif outage:
                    delay_seconds, next_retry_at = auto_resume_schedule(attempt)
                    log.warning(
                        "Pipeline paused after resumable provider outage: kind=%s run_id=%s "
                        "attempt=%s retry_in=%ss error=%s",
                        kind,
                        run_id,
                        attempt + 1,
                        delay_seconds,
                        error,
                    )
                    storage.log_event(
                        "task_paused_outage",
                        kind=kind,
                        run_id=run_id,
                        error=str(error),
                        error_type=type(error).__name__,
                        attempt=attempt + 1,
                        delay_seconds=delay_seconds,
                        next_retry_at=next_retry_at,
                    )
                    return WorkflowOutcome(
                        "paused",
                        error=error,
                        outage=OutagePause(attempt + 1, delay_seconds, next_retry_at),
                    )
                # Include native panics, which PyO3 wraps in BaseException.
                log.exception("Pipeline task failed: kind=%s run_id=%s", kind, run_id)
                storage.log_event(
                    "pipeline_error",
                    kind=kind,
                    run_id=run_id,
                    error=str(error),
                    error_type=type(error).__name__,
                )
                if error is original:
                    raise
                raise error from original


def _publish_outcome(
    request: ExecutionRequest, dependencies: ExecutionDependencies, outcome: WorkflowOutcome
) -> None:
    if not record_terminal_status(
        request,
        dependencies,
        outcome.error,
        status=outcome.status,
        project_status=outcome.project_status,
    ):
        return
    pid, run_id = request.project_id, request.run_id
    outage = outcome.outage
    failed = outcome.error if outcome.status == "error" else None
    if failed is not None:
        code = error_code(failed)
    elif outage is not None and outcome.error is not None:
        code = error_code(outcome.error)
    else:
        code = "none"
    dependencies.metrics.record_task(
        status=outcome.status, code=code, identity=f"{pid}:{run_id}" if run_id else None
    )
    try:
        cache = dependencies.telemetry.connect()
        try:
            if failed is not None:
                publish_error(cache, pid, code=code, message=str(failed), run_id=run_id)
            if outage is not None:
                publish_outage_pause(
                    cache,
                    pid,
                    kind=request.kind,
                    run_id=run_id,
                    next_retry_at=outage.next_retry_at,
                    delay_seconds=outage.delay_seconds,
                    attempt=outage.attempt,
                    message=str(outcome.error or ""),
                )
            cache.publish(
                f"project:{pid}",
                json.dumps({"kind": "state", "project_id": pid, "run_id": run_id}),
            )
        finally:
            dependencies.telemetry.release(cache)
    except Exception:
        # The durable result is authoritative; a transport failure cannot change it.
        pass
    if outage is not None and dependencies.schedule_resume is not None:
        try:
            dependencies.schedule_resume(
                request, outage.attempt, outage.delay_seconds, outage.next_retry_at
            )
        except Exception:
            log.warning(
                "Could not schedule automatic resume: project_id=%s run_id=%s",
                pid,
                run_id,
                exc_info=True,
            )


async def _run_owned(
    request: ExecutionRequest, dependencies: ExecutionDependencies, stop: threading.Event
) -> None:
    try:
        outcome = await dependencies.run_sync(partial(execute, request, dependencies, stop))
    except asyncio.CancelledError:
        raise
    except BaseException as error:
        await asyncio.to_thread(
            _publish_outcome, request, dependencies, WorkflowOutcome("error", error=error)
        )
        raise
    if outcome is not None:
        await asyncio.to_thread(_publish_outcome, request, dependencies, outcome)


async def run(request: ExecutionRequest, dependencies: ExecutionDependencies) -> None:
    stop = threading.Event()
    task = asyncio.create_task(_run_owned(request, dependencies, stop))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        stop.set()
        started = time.monotonic()
        warning = asyncio.get_running_loop().call_later(
            CANCEL_DRAIN_WARNING_SECONDS,
            log.warning,
            "Workflow %s is still draining after %.1fs; retaining ownership",
            request.run_id or request.project_id,
            CANCEL_DRAIN_WARNING_SECONDS,
        )
        try:
            await drain(task)
        finally:
            warning.cancel()
            dependencies.metrics.record_cancel_drain(
                seconds=time.monotonic() - started, identity=request.run_id
            )
        raise
