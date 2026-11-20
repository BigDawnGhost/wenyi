"""Bounded background polish pool whose results are applied on the caller's thread.

Workers only call the model; they never touch run state. The translation loop collects
finished jobs at batch boundaries and applies them in submission order, so formal state
has a single writer and results never depend on thread completion order.
"""

from __future__ import annotations

import contextvars
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass

from .translation_batch import PolishJob


@dataclass(frozen=True)
class PolishOutcome:
    job: PolishJob
    polished: tuple[str, ...] | None
    error: BaseException | None = None


class PolishQueue:
    """Run at most ``workers`` polish calls and hold at most ``2 * workers`` jobs."""

    def __init__(
        self,
        polish: Callable[[PolishJob], tuple[str, ...] | None],
        apply: Callable[[PolishOutcome], None],
        *,
        workers: int,
    ):
        self._polish = polish
        self._apply = apply
        self._workers = max(1, workers)
        self._limit = 2 * self._workers
        self._executor: ThreadPoolExecutor | None = None
        self._pending: deque[tuple[PolishJob, Future[tuple[str, ...] | None]]] = deque()

    def __len__(self) -> int:
        return len(self._pending)

    def pending_chapters(self) -> set[int]:
        return {job.chapter for job, _ in self._pending}

    def submit(self, job: PolishJob) -> None:
        """Queue one job, first applying finished jobs and waiting while the queue is full."""
        self.collect()
        while len(self._pending) >= self._limit:
            # Apply in submission order, so wait for the oldest job rather than any job.
            wait([self._pending[0][1]])
            self.collect()
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=self._workers, thread_name_prefix="wenyi-polish"
            )
        # Carry LLM event observers and timing scopes into the worker thread.
        context = contextvars.copy_context()
        self._pending.append((job, self._executor.submit(context.run, self._polish, job)))

    def collect(self, *, block: bool = False) -> None:
        """Apply finished jobs in submission order; ``block`` waits for every job."""
        while self._pending:
            job, future = self._pending[0]
            if not future.done():
                if not block:
                    return
                wait([future])
            self._pending.popleft()
            try:
                outcome = PolishOutcome(job, future.result())
            except Exception as error:  # noqa: BLE001 - a failed polish keeps the draft.
                outcome = PolishOutcome(job, None, error)
            self._apply(outcome)

    def drain(self) -> None:
        self.collect(block=True)
        self.close()

    def close(self, *, cancel: bool = False) -> None:
        """Release workers; on cancel, drop queued jobs and leave their batches pending.

        Always wait for running calls so their usage reaches the run's final flush and no
        worker outlives the run lock.
        """
        if cancel:
            self._pending.clear()
        executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=cancel)
