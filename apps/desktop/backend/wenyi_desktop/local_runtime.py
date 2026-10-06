"""Bounded local workers and progress delivery, without Redis or a second server.

The catalog records intent before admission. A workspace process lock prevents
two Desktop services from recovering each other's jobs. Domain services retain
their own project/export locks and checkpoint transactions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from wenyi_backend.context import BackendContext
from wenyi_backend.live_statistics import read_live_statistics

from .local_backend import LocalBackend

log = logging.getLogger(__name__)
TASK_NAMES = frozenset(
    {
        "run_parse",
        "run_prepare",
        "run_translation",
        "run_chapter_translation",
        "run_review",
        "run_srt",
        "run_export",
    }
)


class WorkspaceLock:
    """Keep one task-owning Desktop process per workspace, including after crashes."""

    def __init__(self, workspace: Path):
        self.path = workspace / ".desktop.lock"
        self._handle = None

    def acquire(self) -> None:
        handle = self.path.open("a+b")
        try:
            if os.name == "nt":  # pragma: no cover - Windows-specific
                import msvcrt

                if handle.seek(0, os.SEEK_END) == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            handle.close()
            raise RuntimeError(
                "This Desktop workspace is already open or cannot be locked."
            ) from error
        self._handle = handle

    def close(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            if os.name == "nt":  # pragma: no cover - Windows-specific
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


class ProgressHub:
    """Implement the existing telemetry cache port with bounded local subscriptions."""

    def __init__(self, backend: LocalBackend, loop: asyncio.AbstractEventLoop):
        self.backend = backend
        self.loop = loop
        self._lock = threading.Lock()
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


class LocalRuntime:
    """Own bounded task slots; cancellation waits for the synchronous checkpoint boundary."""

    def __init__(self, backend: LocalBackend, context: BackendContext):
        self.backend = backend
        self.context = context
        self.lock = WorkspaceLock(backend.workspace)
        self.hub = ProgressHub(backend, asyncio.get_running_loop())
        self.workflow_queue: asyncio.Queue = asyncio.Queue(maxsize=128)
        self.export_queue: asyncio.Queue = asyncio.Queue(maxsize=128)
        self._submitted: set[str] = set()
        self._active: dict[str, asyncio.Task] = {}
        self._cancelling: set[asyncio.Task] = set()
        self._consumers: list[asyncio.Task] = []
        self._maintenance: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._stopped = asyncio.Event()
        self.accepting = False

    @property
    def idle(self) -> bool:
        """Inspect actual task ownership, not just possibly terminal catalog status."""
        return not (self._submitted or self._active or self._cancelling)

    async def start(self) -> None:
        self.lock.acquire()
        try:
            await asyncio.to_thread(self.recover, startup=True)
            self.hub.start()
            self.accepting = True
            self._consumers = [
                asyncio.create_task(self._consume(self.workflow_queue, 86400)),
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
        queue = self.export_queue if name == "run_export" else self.workflow_queue
        queue.put_nowait((name, values, run_id))
        self._submitted.add(run_id)
        return LocalJob(run_id)

    def _cancel(self, task: asyncio.Task) -> None:
        # Repeated cancellation could interrupt the worker's checkpoint-flush wait.
        if not task.done() and task not in self._cancelling:
            self._cancelling.add(task)
            task.cancel()

    async def _consume(self, queue: asyncio.Queue, timeout: float) -> None:
        from wenyi_backend.workers import tasks

        while True:
            item = await queue.get()
            if item is None:
                queue.task_done()
                return
            name, values, run_id = item
            try:
                if not self.accepting:
                    await asyncio.to_thread(self._interrupt, run_id, "Desktop is shutting down.")
                    continue
                task = asyncio.create_task(
                    getattr(tasks, name)({"backend": self.context}, **values)
                )
                self._active[run_id] = task
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
                except asyncio.TimeoutError:
                    self._cancel(task)
                    await asyncio.gather(task, return_exceptions=True)
                except asyncio.CancelledError:
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
                queue.task_done()

    def _interrupt(self, run_id: str, reason: str) -> None:
        job = self.backend.get_job_by_arq_id(run_id)
        if job is None or job["status"] not in {"queued", "running"}:
            return
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
                    return
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
        except BlockingIOError:
            # A live writer must never be declared dead just because its work is slow.
            return
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
        if self._stopping.is_set():
            await self._stopped.wait()
            return
        self.accepting = False
        self._stopping.set()
        try:
            for queue in (self.workflow_queue, self.export_queue):
                while not queue.empty():
                    item = queue.get_nowait()
                    if item is not None:
                        await asyncio.to_thread(
                            self._interrupt, item[2], "Desktop is shutting down."
                        )
                        self._submitted.discard(item[2])
                    queue.task_done()
            for task in tuple(self._active.values()):
                self._cancel(task)
            await asyncio.gather(*tuple(self._active.values()), return_exceptions=True)
            self.workflow_queue.put_nowait(None)
            self.export_queue.put_nowait(None)
            self.export_queue.put_nowait(None)
            await asyncio.gather(*self._consumers, return_exceptions=True)
            if self._maintenance is not None:
                await self._maintenance
            await self.hub.stop()
        finally:
            self.lock.close()
            self._stopped.set()


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
