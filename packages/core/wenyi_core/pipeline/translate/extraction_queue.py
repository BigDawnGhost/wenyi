"""Overlap one glossary extraction model call with the next batch's translation.

The worker only calls the model; it never touches run state. The caller prepares each
request from the glossary on its own thread, and stores the result there when it settles
the job, so formal state keeps a single writer. At most one job is pending, and a job is
settled before the next one is prepared, so every extraction sees the glossary exactly as
the serial order leaves it. Only the batch translated while a job runs misses that job's
new entries.
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any


class ExtractionQueue:
    def __init__(self) -> None:
        self._executor: ThreadPoolExecutor | None = None
        self._pending: tuple[Future[Any], Callable[[Any], None]] | None = None

    def submit(self, call: Callable[[], Any], finish: Callable[[Any], None]) -> None:
        """Settle the pending job, then start ``call``; ``finish`` later gets its result."""
        self.settle()
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wenyi-glossary")
        # Carry LLM event observers and timing scopes into the worker thread.
        context = contextvars.copy_context()
        self._pending = (self._executor.submit(context.run, call), finish)

    def settle(self) -> bool:
        """Wait for the pending job and finish it on this thread; return whether one ran.

        A failed model call raises here, as it would have when called inline.
        """
        if self._pending is None:
            return False
        future, finish = self._pending
        self._pending = None
        finish(future.result())
        return True

    def close(self, *, cancel: bool = False) -> None:
        """Release the worker; on cancel, discard the pending job unstored.

        Always wait for a running call so its usage reaches the run's final flush and no
        worker outlives the run lock. A discarded batch has no extraction checkpoint, so a
        resumed run extracts it again.
        """
        if cancel:
            self._pending = None
        executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=True)
