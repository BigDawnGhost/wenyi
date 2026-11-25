"""Independent local workflows and bounded exports/progress, without Redis.

The catalog records intent before admission. A workspace process lock prevents
two Desktop services from recovering each other's jobs. Domain services retain
their own project/export locks and checkpoint transactions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, TypeVar

from wenyi_backend.context import BackendContext
from wenyi_backend.execution.runner import drain
from wenyi_backend.live_statistics import read_live_statistics
from wenyi_core.storage.locks import FileLockHandle, StorageBusyError

from . import notices, paths
from .diagnostics import Reporter, emit, failure_attributes, scope
from .local_backend import LocalBackend

log = logging.getLogger(__name__)
_T = TypeVar("_T")
TASK_NAMES = frozenset(
    {
        "run_parse",
        "run_prepare",
        "run_translation",
        "run_chapter_translation",
        "run_review",
        "run_export",
    }
)


class WorkspaceLock:
    """Keep one task-owning Desktop process per workspace, including after crashes."""

    def __init__(self, workspace: Path):
        self.path = paths.instance_lock(workspace)
        self._lock: FileLockHandle | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLockHandle(str(self.path))
        try:
            lock.acquire(blocking=False)
        except (StorageBusyError, OSError) as error:
            raise RuntimeError(
                "This Desktop workspace is already open or cannot be locked."
            ) from error
        self._lock = lock

    def close(self) -> None:
        lock, self._lock = self._lock, None
        if lock is not None:
            lock.release()


class ProgressHub:
    """Implement the existing telemetry cache port with bounded local subscriptions."""

    def __init__(self, backend: LocalBackend, loop: asyncio.AbstractEventLoop):
        self.backend = backend
        self.loop = loop
        self._lock = threading.Lock()
        self._flush_lock = asyncio.Lock()
        self._cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._dirty: dict[str, dict] = {}
        self._subscribers: dict[str, set[asyncio.Queue[str | None]]] = {}
        self._wake = asyncio.Event()
        self._closing = False
        self._writer: asyncio.Task | None = None

    def start(self) -> None:
        self._writer = asyncio.create_task(self._persist(), name="desktop-progress")

    def get(self, key: str) -> str | None:
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            deadline, value = entry
            if deadline <= time.monotonic():
                del self._cache[key]
                return None
            self._cache.move_to_end(key)
            return value

    def set(self, key: str, value: str, *, ex: int) -> None:
        if self._closing:
            return
        with self._lock:
            self._cache[key] = (time.monotonic() + ex, value)
            self._cache.move_to_end(key)
            while len(self._cache) > 256:
                self._cache.popitem(last=False)
            if key.startswith("project:") and key.endswith(":progress"):
                pid = key[len("project:") : -len(":progress")]
                payload = json.loads(value)
                if isinstance(payload, dict) and payload.get("project_id") == pid:
                    self._dirty[pid] = payload
        # Never wait on the catalog while a domain callback may hold a state transaction.
        self.loop.call_soon_threadsafe(self._wake.set)

    def publish(self, channel: str, value: str) -> None:
        if not self._closing:
            self.loop.call_soon_threadsafe(self._deliver, channel, value)

    def _deliver(self, channel: str, value: str | None) -> None:
        for queue in tuple(self._subscribers.get(channel, ())):
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(value)

    @contextmanager
    def subscribe(self, pid: str) -> Iterator[asyncio.Queue[str | None]]:
        channel = f"project:{pid}"
        queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=64)
        subscribers = self._subscribers.setdefault(channel, set())
        subscribers.add(queue)
        try:
            yield queue
        finally:
            subscribers.discard(queue)
            if not subscribers:
                self._subscribers.pop(channel, None)

    async def flush(self) -> None:
        """Wait for earlier saves before draining pending progress in order."""
        async with self._flush_lock:
            with self._lock:
                pending, self._dirty = self._dirty, {}
            for pid, payload in pending.items():
                try:
                    await asyncio.to_thread(self.backend.save_progress, pid, payload)
                except Exception:
                    # Advisory progress is not a replacement for committed checkpoints.
                    log.warning("Could not persist Desktop progress", exc_info=True)

    async def _persist(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            if self._closing:
                await self.flush()
                return
            await asyncio.sleep(0.25)
            await self.flush()

    async def stop(self) -> None:
        self._closing = True
        self._wake.set()
        if self._writer is not None:
            await self._writer
        for channel in tuple(self._subscribers):
            self._deliver(channel, None)
        with self._lock:
            self._cache.clear()


@dataclass(frozen=True)
class LocalJob:
    job_id: str


async def _await_owned(future: asyncio.Future[_T]) -> _T:
    """Delay propagating caller cancellation until the owned work is finished."""
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        await drain(future)
        raise


async def run_workflow_sync(operation: Callable[[], _T]) -> _T:
    """Own one thread per run, leaving the default executor free for control requests."""
    loop = asyncio.get_running_loop()
    # This executor belongs to one invocation, not to a fixed set of workflow slots.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="desktop-workflow") as executor:
        future = loop.run_in_executor(executor, copy_context().run, operation)
        return await _await_owned(future)


class LocalRuntime:
    """Supervise independent workflows and bounded exports until their writers exit."""

    def __init__(
        self, backend: LocalBackend, context: BackendContext, *, diagnostics: Reporter | None = None
    ):
        self.backend = backend
        self.context = context
        self.diagnostics = diagnostics
        self.lock = WorkspaceLock(backend.workspace)
        self.hub = ProgressHub(backend, asyncio.get_running_loop())
        self.export_queue: asyncio.Queue[tuple[str, dict, str] | None] = asyncio.Queue(maxsize=128)
        self._submitted: set[str] = set()
        self._active: dict[str, asyncio.Task] = {}
        self._cancelling: set[asyncio.Task] = set()
        self._workflows: set[asyncio.Task] = set()
        self._auto_resumes: dict[str, asyncio.Task] = {}
        self._consumers: list[asyncio.Task] = []
        self._maintenance: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._shutdown_task: asyncio.Task[None] | None = None
        self.accepting = False

    @property
    def idle(self) -> bool:
        """Inspect actual task ownership, not just possibly terminal catalog status."""
        return not (self._submitted or self._active or self._cancelling or self._workflows)

    async def start(self) -> None:
        self.lock.acquire()
        try:
            await asyncio.to_thread(self.recover, startup=True)
            self.hub.start()
            self.accepting = True
            self._consumers = [
                asyncio.create_task(self._consume(self.export_queue, 3600)),
                asyncio.create_task(self._consume(self.export_queue, 3600)),
            ]
            self._maintenance = asyncio.create_task(self._recover_periodically())
        except BaseException:
            self.lock.close()
            raise

    def enqueue(self, name: str, **kwargs: Any) -> LocalJob:
        if not self.accepting:
            raise RuntimeError("The local worker is not accepting tasks.")
        if name not in TASK_NAMES:
            raise ValueError("Unsupported local task.")
        values = dict(kwargs)
        run_id = values.pop("_job_id", None)
        if not run_id or values.get("run_id") != run_id:
            raise ValueError("A local task requires one durable run identity.")
        job = self.backend.get_job_by_arq_id(run_id)
        if job is None or job["project_id"] != values.get("project_id"):
            raise ValueError("The local task has no matching durable record.")
        if run_id in self._submitted or job["status"] != "queued":
            return LocalJob(run_id)
        self._submitted.add(run_id)
        try:
            if name == "run_export":
                self.export_queue.put_nowait((name, values, run_id))
            else:
                pending = self._run_job(name, values, run_id, 86400)
                try:
                    task = asyncio.create_task(pending, name=f"desktop-workflow:{run_id}")
                except BaseException:
                    pending.close()
                    raise
                self._workflows.add(task)
                task.add_done_callback(self._workflow_finished)
        except BaseException:
            self._submitted.discard(run_id)
            raise
        return LocalJob(run_id)

    def schedule_auto_resume(
        self, request: Any, attempt: int, delay_seconds: int, next_retry_at: str
    ) -> None:
        if not self.accepting or self._stopping.is_set():
            return
        self.hub.loop.call_soon_threadsafe(
            self._schedule_auto_resume_on_loop,
            request.project_id,
            request.run_id,
            attempt,
            delay_seconds,
        )

    def _schedule_auto_resume_on_loop(
        self,
        project_id: str,
        expected_run_id: str | None,
        attempt: int,
        delay_seconds: int,
    ) -> None:
        if not self.accepting or self._stopping.is_set():
            return
        self._cancel_auto_resume_on_loop(project_id)
        task = asyncio.create_task(
            self._auto_resume_after(project_id, expected_run_id, attempt, delay_seconds),
            name=f"desktop-auto-resume:{project_id}",
        )
        self._auto_resumes[project_id] = task

    def cancel_auto_resume(self, project_id: str) -> None:
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self.hub.loop:
            self._cancel_auto_resume_on_loop(project_id)
        elif not self.hub.loop.is_closed():
            self.hub.loop.call_soon_threadsafe(self._cancel_auto_resume_on_loop, project_id)

    def _cancel_auto_resume_on_loop(self, project_id: str) -> None:
        existing = self._auto_resumes.pop(project_id, None)
        if existing is not None and not existing.done():
            existing.cancel()

    async def _auto_resume_after(
        self,
        project_id: str,
        expected_run_id: str | None,
        attempt: int,
        delay_seconds: int,
    ) -> None:
        from wenyi_backend.workers import tasks
        from wenyi_backend.workers.signing import SIGNATURE_FIELD, sign_task

        current = asyncio.current_task()
        try:
            await asyncio.sleep(max(0.0, float(delay_seconds)))
            if not self.accepting or self._stopping.is_set():
                return
            values = {
                "project_id": project_id,
                "expected_run_id": expected_run_id,
                "attempt": attempt,
            }
            signature = sign_task("run_auto_resume", values, self.context.api_token)
            await tasks.run_auto_resume(
                {"backend": self.context}, **values, **{SIGNATURE_FIELD: signature}
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Desktop automatic resume failed: project_id=%s", project_id)
        finally:
            if self._auto_resumes.get(project_id) is current:
                self._auto_resumes.pop(project_id, None)

    def _workflow_finished(self, task: asyncio.Task) -> None:
        self._workflows.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            log.error("Desktop workflow supervision failed", exc_info=error)

    def _cancel(self, task: asyncio.Task) -> None:
        # Timeout and shutdown can both request cancellation of the same task.
        if not task.done() and task not in self._cancelling:
            self._cancelling.add(task)
            task.cancel()

    async def _run_observed(self, name: str, values: dict, run_id: str) -> None:
        from wenyi_backend.workers import tasks
        from wenyi_backend.workers.signing import SIGNATURE_FIELD, sign_task

        with scope(self.diagnostics, project_ref=values.get("project_id", ""), run_ref=run_id):
            started = time.monotonic()
            kind = name.removeprefix("run_")
            emit("task_started", kind=kind)
            try:
                signature = sign_task(name, values, self.context.api_token)
                payload = {**values, SIGNATURE_FIELD: signature}
                await getattr(tasks, name)({"backend": self.context}, **payload)
            except asyncio.CancelledError:
                emit("task_cancelled", kind=kind)
                raise
            except Exception as error:
                attributes = failure_attributes(error)
                emit("task_failed", kind=kind, **attributes)
                # The shared runner has already written the terminal job status
                # before it lets the exception reach this point.
                job = await asyncio.to_thread(self._durable_job, run_id)
                if job is not None and job["status"] == "error":
                    await self._task_notice(
                        "export_failed" if name == "run_export" else "task_failed",
                        name,
                        values,
                        run_id,
                        attributes.get("category"),
                    )
                raise
            else:
                # Return can mean completed or paused; the durable job remains authoritative.
                emit(
                    "task_finished", kind=kind, duration_ms=int((time.monotonic() - started) * 1000)
                )
                await self._completed_notice(name, values, run_id)

    def _durable_job(self, run_id: str) -> dict | None:
        try:
            return self.backend.get_job_by_arq_id(run_id)
        except Exception:
            log.debug("Could not read a durable job for a status notice", exc_info=True)
            return None

    async def _completed_notice(self, name: str, values: dict, run_id: str) -> None:
        """Announce only a persisted success, never a pause that returned normally."""
        job = await asyncio.to_thread(self._durable_job, run_id)
        if job is None or job["status"] != "done":
            return
        project = await asyncio.to_thread(self._catalog_project, values)
        if project is None:
            return
        status = project.get("status")
        if name == "run_prepare" and status == "prepared":
            self._emit_notice(
                notices.task_notice(
                    "prepare_completed", project=project, run_id=run_id, task="prepare"
                )
            )
        elif name in {"run_translation", "run_chapter_translation"} and status == "done":
            self._emit_notice(
                notices.task_notice(
                    "project_completed",
                    project=project,
                    run_id=run_id,
                    task=name.removeprefix("run_"),
                )
            )

    async def _task_notice(
        self, kind: str, name: str, values: dict, run_id: str, reason: str | None
    ) -> None:
        project = await asyncio.to_thread(self._catalog_project, values)
        if project is None:
            return
        self._emit_notice(
            notices.task_notice(
                kind,
                project=project,
                run_id=run_id,
                task=name.removeprefix("run_"),
                reason=reason,
            )
        )

    def _catalog_project(self, values: dict) -> dict | None:
        try:
            return self.backend.catalog_project(str(values.get("project_id", "")))
        except Exception:
            log.debug("Could not read a catalog row for a status notice", exc_info=True)
            return None

    def _emit_notice(self, notice: dict | None) -> None:
        if notice is None or self.diagnostics is None:
            return
        try:
            self.diagnostics.notice(notice)
        except Exception:
            # Notifications are advisory and must never change a task's outcome.
            log.debug("Could not queue a Desktop status notice", exc_info=True)

    async def _consume(
        self, queue: asyncio.Queue[tuple[str, dict, str] | None], timeout: float
    ) -> None:
        while True:
            item = await queue.get()
            try:
                if item is None:
                    return
                await self._run_job(*item, timeout)
            finally:
                queue.task_done()

    async def _run_job(self, name: str, values: dict, run_id: str, timeout: float) -> None:
        try:
            if not self.accepting:
                await _await_owned(
                    asyncio.create_task(
                        asyncio.to_thread(self._interrupt, run_id, "Desktop is shutting down.")
                    )
                )
                return
            task = asyncio.create_task(self._run_observed(name, values, run_id))
            self._active[run_id] = task
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except asyncio.TimeoutError:
                await _await_owned(
                    asyncio.create_task(self._stop_job(task, name, values, run_id, timed_out=True))
                )
            except asyncio.CancelledError:
                await drain(asyncio.create_task(self._stop_job(task, name, values, run_id)))
                if not self._stopping.is_set():
                    raise
            except Exception:
                # Task adapters already persist a user-visible failure and its event.
                log.exception("Desktop task failed")
        finally:
            finished = self._active.pop(run_id, None)
            if finished is not None:
                self._cancelling.discard(finished)
            self._submitted.discard(run_id)

    async def _stop_job(
        self, task: asyncio.Task, name: str, values: dict, run_id: str, *, timed_out: bool = False
    ) -> None:
        was_active = False
        if timed_out:
            with scope(self.diagnostics, project_ref=values.get("project_id", ""), run_ref=run_id):
                emit("task_timeout", kind=name.removeprefix("run_"), category="timeout")
            job = await asyncio.to_thread(self._durable_job, run_id)
            was_active = job is not None and job["status"] in {"queued", "running"}
        self._cancel(task)
        await asyncio.gather(task, return_exceptions=True)
        # Cancellation may precede the task adapter's first instruction.
        reason = "Desktop task timed out." if timed_out else "Desktop task stopped."
        stopped = await asyncio.to_thread(self._interrupt, run_id, reason)
        if timed_out:
            if not stopped and was_active:
                # The runner can have already persisted the cooperative pause.
                job = await asyncio.to_thread(self._durable_job, run_id)
                stopped = job is not None and job["status"] in {"paused", "interrupted"}
            if stopped:
                await self._task_notice("task_timeout", name, values, run_id, "timeout")

    def _interrupt(self, run_id: str, reason: str) -> bool:
        """Stop one durable job; True only when this call changed its status."""
        job = self.backend.get_job_by_arq_id(run_id)
        if job is None or job["status"] not in {"queued", "running"}:
            return False
        pid = job["project_id"]
        storage = self.backend.storage_for(pid)
        is_export = job["kind"] == "export"
        try:
            lock = (
                storage.export_lock(job["params"]["export_id"], blocking=False)
                if is_export
                else storage.lock(blocking=False)
            )
            with lock:
                changed = self.backend.set_job_status(
                    job["id"],
                    "error" if is_export else "interrupted",
                    error=reason,
                    expected_status={"queued", "running"},
                )
                if not changed:
                    return False
                if is_export:
                    self.backend.set_export_status(
                        job["params"]["export_id"], "error", error=reason
                    )
                else:
                    latest = next(
                        (row for row in self.backend.list_jobs(pid) if row["kind"] != "export"),
                        None,
                    )
                    if latest and latest["id"] == job["id"]:
                        self.backend.set_project_status(pid, "paused", error=reason)
                return True
        except BlockingIOError:
            # A live writer must never be declared dead just because its work is slow.
            return False
        finally:
            storage.close()

    def recover(self, *, startup: bool = False) -> None:
        now = datetime.now(timezone.utc)
        for job in self.backend.all_jobs():
            if job["status"] not in {"queued", "running"} or job["run_id"] in self._submitted:
                continue
            if not startup:
                stamp = datetime.fromisoformat(job["updated_at"])
                if (now - stamp).total_seconds() < 120:
                    continue
            self._interrupt(job["run_id"], "The previous local worker stopped; resume saved work.")

    async def _recover_periodically(self) -> None:
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=30)
            except asyncio.TimeoutError:
                try:
                    await asyncio.to_thread(self.recover)
                except Exception:
                    log.exception("Could not recover local jobs")

    async def stop(self) -> None:
        if self._shutdown_task is None:
            self.accepting = False
            self._stopping.set()
            # A pending outage auto-resume must not start a new run during shutdown.
            for task in tuple(self._auto_resumes.values()):
                if not task.done():
                    task.cancel()
            # A supervisor may already have scheduled a worker that has not entered yet.
            for task in tuple(self._active.values()):
                self._cancel(task)
            self._shutdown_task = asyncio.create_task(self._shutdown())
        # The caller also owns the services this task needs to finish draining.
        await _await_owned(self._shutdown_task)

    async def _shutdown(self) -> None:
        try:
            await asyncio.gather(*tuple(self._auto_resumes.values()), return_exceptions=True)
            self._auto_resumes.clear()
            await asyncio.gather(*tuple(self._active.values()), return_exceptions=True)
            await asyncio.gather(*tuple(self._workflows), return_exceptions=True)
            while not self.export_queue.empty():
                item = self.export_queue.get_nowait()
                if item is not None:
                    await asyncio.to_thread(self._interrupt, item[2], "Desktop is shutting down.")
                    self._submitted.discard(item[2])
                self.export_queue.task_done()
            self.export_queue.put_nowait(None)
            self.export_queue.put_nowait(None)
            await asyncio.gather(*self._consumers, return_exceptions=True)
            if self._maintenance is not None:
                await self._maintenance
            await self.hub.stop()
        finally:
            self.lock.close()


def progress_snapshot(backend: LocalBackend, runtime: LocalRuntime | None, pid: str) -> dict | None:
    raw = runtime.hub.get(f"project:{pid}:progress") if runtime else None
    snapshot = json.loads(raw) if raw else backend.load_progress(pid)
    if not isinstance(snapshot, dict) or snapshot.get("project_id") != pid:
        return None
    try:
        updated = datetime.fromisoformat(snapshot["updated_at"])
        if not 0 <= (datetime.now(timezone.utc) - updated).total_seconds() < 604800:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return snapshot


def statistics_snapshot(runtime: LocalRuntime | None, pid: str, job: dict) -> dict | None:
    return read_live_statistics(runtime.hub, pid, job) if runtime else None
