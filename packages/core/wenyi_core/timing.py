"""Measure invocation wall time and accumulate completed invocations across resumes."""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from collections.abc import Callable
from datetime import datetime
from types import TracebackType
from typing import Any, Protocol
from uuid import uuid4


def format_duration(seconds: float) -> str:
    """Format elapsed seconds without wrapping at 24 hours."""
    hours, remainder = divmod(max(0, int(seconds)), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def load_timing(run_dir: str) -> dict[str, Any] | None:
    """Read the last committed timing ledger without creating state."""
    try:
        with open(os.path.join(run_dir, "timing.json"), encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return None


def save_timing(run_dir: str, record: dict[str, Any]) -> dict[str, Any]:
    """Upsert one invocation atomically; the caller must hold the store's timing lock."""
    ledger = load_timing(run_dir) or {"runs": []}
    runs = {run["id"]: run for run in ledger["runs"]}
    runs[record["id"]] = record
    ledger = {
        "total_seconds": sum(run["elapsed_seconds"] for run in runs.values()),
        "runs": list(runs.values()),
    }
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=run_dir, prefix=".timing-", suffix=".tmp", delete=False
        ) as handle:
            temporary_path = handle.name
            json.dump(ledger, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary_path, os.path.join(run_dir, "timing.json"))
    finally:
        if temporary_path is not None and os.path.exists(temporary_path):
            os.unlink(temporary_path)
    return ledger


class TimingStore(Protocol):
    def record_timing(self, record: dict[str, Any]) -> dict[str, Any]:
        """Serialize and persist one invocation's timing."""
        ...


class RunTimer:
    """Time one outer workflow, including waits and I/O, using a monotonic clock."""

    def __init__(self, operation: str, *, clock: Callable[[], float] | None = None) -> None:
        self.operation = operation
        self._clock = clock or time.monotonic
        self._started = self._clock()
        self._stopped: float | None = None
        self._started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self._id = uuid4().hex
        # Bind only after the source identity has been validated or initialized.
        self.store: TimingStore | None = None

    @property
    def elapsed(self) -> float:
        end = self._clock() if self._stopped is None else self._stopped
        return max(0.0, end - self._started)

    def __enter__(self) -> RunTimer:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stopped = self._clock()
        status = "completed"
        if exc_type is not None:
            status = (
                "interrupted" if issubclass(exc_type, (KeyboardInterrupt, SystemExit)) else "failed"
            )
        if self.store is not None:
            try:
                self.store.record_timing(
                    {
                        "id": self._id,
                        "operation": self.operation,
                        "started_at": self._started_at,
                        "finished_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "elapsed_seconds": self.elapsed,
                        "status": status,
                    }
                )
            except (OSError, ValueError):
                if exc_type is None:
                    raise
                # Preserve the workflow's original exception when persistence also fails.
                logging.getLogger(__name__).warning("Could not save workflow timing.")
