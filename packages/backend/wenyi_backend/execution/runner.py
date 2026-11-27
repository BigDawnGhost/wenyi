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

from wenyi_core.errors import WenyiError
from wenyi_core.events import ProgressPayload
from wenyi_core.llm.base import LLMClient
from wenyi_core.llm.limits import RequestCancelled, RequestStopped

from ..emitters import publish_error, redis_progress_fn
from ..live_statistics import LiveStatistics
from ..ports.jobs import JobRepository
from .contracts import ExecutionDependencies, ExecutionRequest

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
class WorkflowOutcome:
    status: str
    project_status: str | None = None
    error: BaseException | None = None

    def as_record(self) -> dict:
        return {
            "status": self.status,
            "project_status": self.project_status,
            "message": str(self.error)[:4000] if self.error is not None else None,
            "code": error_code(self.error) if self.error is not None else None,
        }

    @classmethod
    def from_record(cls, record: dict) -> WorkflowOutcome:
        error = (
            WenyiError(record["message"], code=record["code"])
            if record["message"] is not None
            else None
        )
        return cls(record["status"], record["project_status"], error)


def finish_catalog_job(
    jobs: JobRepository, job: dict, fallback: WorkflowOutcome
) -> WorkflowOutcome | None:
    """Publish the writer's receipt if present; never replace a known result with a guess."""
    if job["status"] not in {"queued", "running"} or job["kind"] == "export":
        return None
    receipt = (job.get("result") or {}).get("workflow_outcome")
    outcome = WorkflowOutcome.from_record(receipt) if receipt is not None else fallback
    changed = jobs.finish_workflow(
        job["project_id"],
        job["id"],
        outcome.status,
        expected_status=job["status"],
        project_status=outcome.project_status or outcome.status,
        error=str(outcome.error) if outcome.error is not None else None,
    )
    return outcome if changed else None


def current_job(request: ExecutionRequest, dependencies: ExecutionDependencies, status: str):
    job = dependencies.jobs.get_job_by_arq_id(request.run_id) if request.run_id else None
    if (
        not job
        or job["project_id"] != request.project_id
        or job["kind"] != request.kind
        or job["status"] != status
    ):
        return None
    latest = next(
        (row for row in dependencies.jobs.list_jobs(request.project_id) if row["kind"] != "export"),
        None,
    )
    return job if latest and latest["id"] == job["id"] else None


def claim(request: ExecutionRequest, dependencies: ExecutionDependencies) -> bool:
    """One delivery owns the job; another delivery must not stop its writer."""
    job = current_job(request, dependencies, "queued")
    return job is not None and dependencies.jobs.set_job_status(
        job["id"], "running", expected_status="queued"
    )


def record_terminal_status(
    request: ExecutionRequest,
    dependencies: ExecutionDependencies,
    outcome: WorkflowOutcome,
) -> WorkflowOutcome | None:
    """The writer is gone. Publication needs only the catalog, not the book directory."""
    pid, run_id = request.project_id, request.run_id
    if not run_id:
        dependencies.projects.set_project_status(
            pid,
            outcome.project_status or outcome.status,
            error=str(outcome.error) if outcome.error is not None else None,
        )
        return outcome
    job = dependencies.jobs.get_job_by_arq_id(run_id)
    if job is None or job["project_id"] != pid or job["kind"] != request.kind:
        return None
    return finish_catalog_job(dependencies.jobs, job, outcome)


def execute(
    request: ExecutionRequest,
    dependencies: ExecutionDependencies,
    stop: threading.Event | None = None,
    *,
    preclaimed: bool = False,
) -> WorkflowOutcome | None:
    """Run one checkpoint writer; return its outcome only after releasing its resources."""
    projects = dependencies.projects
    pid, run_id, kind = request.project_id, request.run_id, request.kind
    stop = stop or threading.Event()
    finished = threading.Event()
    client: LLMClient | None = None

    def monitor_pause() -> None:
        while not finished.wait(0.25):
            try:
                if not stop.is_set() and projects.is_paused(pid):
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
        if not preclaimed:
            watcher = threading.Thread(
                target=copy_context().run, args=(monitor_pause,), daemon=True
            )
            watcher.start()

            def stop_watcher() -> None:
                finished.set()
                watcher.join(timeout=1)

            resources.callback(stop_watcher)
        emitter = redis_progress_fn(cache, pid, kind=kind, run_id=run_id)

        def progress(
            done: int, total: int, label: str, payload: ProgressPayload | None = None
        ) -> None:
            if not preclaimed and (stop.is_set() or projects.is_paused(pid)):
                if client is not None:
                    client.cancel()
                raise PauseRequested(pid)
            emitter(done, total, label, payload)

        with storage.lock():
            claimed = None
            if run_id:
                if preclaimed:
                    claimed = current_job(request, dependencies, "running")
                    if claimed is None:
                        return None
                elif not claim(request, dependencies):
                    return None
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
                        if stop.is_set():
                            value.cancel()

                    progress(0, 0, "progress.stage.taskStart")
                    result = dependencies.operation(request, storage, progress, bind_client)
                    progress(1, 1, "progress.stage.taskDone")
                    storage.log_event("task_completed", kind=kind, run_id=run_id)
                outcome = WorkflowOutcome("done", project_status=result)
            except (KeyboardInterrupt, RequestCancelled):
                storage.log_event("task_paused", kind=kind, run_id=run_id)
                outcome = WorkflowOutcome("paused")
            except RequestStopped as error:
                storage.log_event("task_paused", kind=kind, run_id=run_id, reason=str(error))
                outcome = WorkflowOutcome("paused", error=error)
            except BaseException as error:
                # Include native panics, which PyO3 wraps in BaseException.
                log.exception("Pipeline task failed: kind=%s run_id=%s", kind, run_id)
                storage.log_event(
                    "pipeline_error",
                    kind=kind,
                    run_id=run_id,
                    error=str(error),
                    error_type=type(error).__name__,
                )
                if claimed is None:
                    raise
                outcome = WorkflowOutcome("error", error=error)
            if claimed is not None:
                # Commit before releasing the writer lock. A crash after this point,
                # or recovery racing the supervisor, must publish the same outcome.
                dependencies.jobs.set_job_status(
                    claimed["id"],
                    "running",
                    expected_status="running",
                    result={"workflow_outcome": outcome.as_record()},
                )
            return outcome


def publish_outcome(
    request: ExecutionRequest,
    dependencies: ExecutionDependencies,
    fallback: WorkflowOutcome,
    *,
    forced: bool = False,
) -> WorkflowOutcome | None:
    outcome = record_terminal_status(request, dependencies, fallback)
    if outcome is None:
        return None
    pid, run_id = request.project_id, request.run_id
    if forced and outcome is fallback:
        # The stopped writer cannot emit its final event. A disconnected library
        # must not prevent the catalog transaction above from making the job resumable.
        try:
            with closing(dependencies.storage_for(pid)) as storage:
                event = "task_paused" if outcome.status == "paused" else "task_interrupted"
                storage.log_event(event, kind=request.kind, run_id=run_id)
        except Exception:
            log.warning("Could not log workflow stop: run_id=%s", run_id, exc_info=True)
    failed = outcome.error if outcome.status == "error" else None
    code = error_code(failed) if failed is not None else "none"
    dependencies.metrics.record_task(
        status=outcome.status, code=code, identity=f"{pid}:{run_id}" if run_id else None
    )
    try:
        cache = dependencies.telemetry.connect()
        try:
            if failed is not None:
                publish_error(cache, pid, code=code, message=str(failed), run_id=run_id)
            cache.publish(
                f"project:{pid}",
                json.dumps({"kind": "state", "project_id": pid, "run_id": run_id}),
            )
        finally:
            dependencies.telemetry.release(cache)
    except Exception:
        # The durable result is authoritative; a transport failure cannot change it.
        pass
    return outcome


async def _run_owned(
    request: ExecutionRequest, dependencies: ExecutionDependencies, stop: threading.Event
) -> None:
    try:
        outcome = await dependencies.run_sync(partial(execute, request, dependencies, stop))
    except asyncio.CancelledError:
        raise
    except BaseException as error:
        await asyncio.to_thread(
            publish_outcome, request, dependencies, WorkflowOutcome("error", error=error)
        )
        raise
    if outcome is not None:
        await asyncio.to_thread(publish_outcome, request, dependencies, outcome)


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
