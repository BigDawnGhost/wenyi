"""Bounded, stable-order CPU work owned by one EPUB reader or writer invocation."""

from __future__ import annotations

import multiprocessing
import os
import signal
import sys
from collections import deque
from collections.abc import Callable, Generator, Iterable
from concurrent.futures import Future, ProcessPoolExecutor
from threading import Lock
from types import TracebackType
from typing import TypeVar

_Input = TypeVar("_Input")
_Output = TypeVar("_Output")

MAX_RESOURCE_WORKERS = 6
_MIN_RESOURCE_COUNT = 12
_MIN_SOURCE_BYTES = 2 * 1024 * 1024

# Admission is local to a parent process, not a platform or project identity. Independent
# workflows may wait here, but cannot multiply their CPU pools and peak memory usage.
_POOL_ADMISSION = Lock()


def _initialize_worker() -> None:
    # The parent owns interruption and drains bounded work before releasing its locks.
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def _worker_count(resource_count: int, source_bytes: int) -> int:
    if resource_count < _MIN_RESOURCE_COUNT or source_bytes < _MIN_SOURCE_BYTES:
        return 1
    if multiprocessing.current_process().daemon:
        return 1
    main_file = getattr(sys.modules.get("__main__"), "__file__", "") or ""
    if main_file.startswith("<") and not getattr(sys, "frozen", False):
        # A stdin/notebook entry point cannot be re-imported by spawn.
        return 1
    cpu_count = os.cpu_count() or 1
    if hasattr(os, "sched_getaffinity"):
        try:
            cpu_count = min(cpu_count, len(os.sched_getaffinity(0)))
        except OSError:
            pass
    return max(1, min(MAX_RESOURCE_WORKERS, cpu_count, resource_count))


class ResourcePool:
    """Reuse at most six spawned workers for the CPU phases of one large EPUB.

    Small inputs use exactly the same functions in the caller. Only pure, importable
    module-level callables and their data cross the process boundary; storage, open ZIP
    handles and application context stay with the caller.
    """

    def __init__(self, resource_count: int, source_bytes: int) -> None:
        self.workers = _worker_count(resource_count, source_bytes)
        self._executor: ProcessPoolExecutor | None = None

    def __enter__(self) -> ResourcePool:
        if self.workers > 1:
            _POOL_ADMISSION.acquire()
            try:
                self._executor = ProcessPoolExecutor(
                    max_workers=self.workers,
                    mp_context=multiprocessing.get_context("spawn"),
                    initializer=_initialize_worker,
                )
            except BaseException:
                _POOL_ADMISSION.release()
                raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._executor is not None:
            try:
                self._executor.shutdown(wait=True, cancel_futures=True)
            finally:
                self._executor = None
                _POOL_ADMISSION.release()

    def map(
        self,
        operation: Callable[[_Input], _Output],
        inputs: Iterable[_Input],
    ) -> Generator[_Output, None, None]:
        """Yield in input order with at most two pending resources per worker."""
        if self._executor is None:
            yield from map(operation, inputs)
            return
        iterator = iter(inputs)
        pending: deque[Future[_Output]] = deque()
        try:
            for _ in range(self.workers * 2):
                try:
                    item = next(iterator)
                except StopIteration:
                    break
                pending.append(self._executor.submit(operation, item))
            while pending:
                yield pending.popleft().result()
                try:
                    item = next(iterator)
                except StopIteration:
                    continue
                pending.append(self._executor.submit(operation, item))
        finally:
            for future in pending:
                future.cancel()
