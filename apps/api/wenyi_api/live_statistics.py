"""Short-lived Web statistics; canonical usage and timing remain in PostgreSQL."""

from __future__ import annotations

import json
import threading
from contextlib import ExitStack
from datetime import datetime, timezone
from types import TracebackType
from typing import Any, Protocol

from wenyi_core.llm.base import LLMClient
from wenyi_core.llm.usage import empty_usage, merge_usage_summaries, usage_delta
from wenyi_core.timing import RunTimer, observe_timers

LIVE_TTL_SECONDS = 10


class StatisticsCache(Protocol):
    def get(self, key: str, /) -> Any: ...

    def set(self, key: str, value: str, /, *, ex: int) -> object: ...

    def publish(self, channel: str, value: str, /) -> object: ...


class StatisticsStore(Protocol):
    def load_usage(self) -> dict[str, Any] | None: ...

    def read_artifact(self, key: str) -> Any: ...


class LiveStatistics:
    """Publish immutable run-baseline plus current client usage, never ledger increments."""

    def __init__(
        self, redis: StatisticsCache, project_id: str, run_id: str, store: StatisticsStore
    ) -> None:
        self.redis = redis
        self.project_id = project_id
        self.run_id = run_id
        self.store = store
        self._baseline = store.load_usage() or empty_usage()
        self._client: LLMClient | None = None
        self._checkpoint = empty_usage()
        self._timers: list[RunTimer] = []
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._scope = ExitStack()
        self._thread: threading.Thread | None = None

    def bind_client(self, client: LLMClient) -> None:
        with self._lock:
            self._checkpoint = client.usage_summary()
            self._client = client

    def observe_timer(self, timer: RunTimer) -> None:
        with self._lock:
            self._timers.append(timer)

    def publish(self) -> None:
        """Telemetry failure must not change the workflow or its durable ledgers."""
        try:
            with self._lock:
                client = self._client
                usage = merge_usage_summaries(
                    self._baseline,
                    usage_delta(client.usage_summary(), self._checkpoint)
                    if client
                    else empty_usage(),
                )
                clocks = []
                for timer in self._timers:
                    row = timer.snapshot()
                    if timer.store is not None or row["status"] == "running":
                        clocks.append(row)
            timing = self.store.read_artifact("timing.json") or {"runs": [], "total_seconds": 0}
            runs = {row["id"]: row for row in timing["runs"]}
            for row in clocks:
                # A timer can finish between the snapshot and the database read.
                if runs.get(row["id"], {}).get("status") not in {
                    "completed",
                    "failed",
                    "interrupted",
                }:
                    runs[row["id"]] = row
            snapshot = {
                "project_id": self.project_id,
                "run_id": self.run_id,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "usage": usage,
                "timing": {
                    "runs": list(runs.values()),
                    "total_seconds": sum(row["elapsed_seconds"] for row in runs.values()),
                },
            }
            self.redis.set(
                f"project:{self.project_id}:stats", json.dumps(snapshot), ex=LIVE_TTL_SECONDS
            )
            self.redis.publish(
                f"project:{self.project_id}",
                json.dumps(
                    {
                        "kind": "stats",
                        "project_id": self.project_id,
                        "run_id": self.run_id,
                    }
                ),
            )
        except Exception:
            # The regular REST polling path can still return the committed ledgers.
            pass

    def _heartbeat(self) -> None:
        while not self._stopped.wait(1):
            self.publish()

    def __enter__(self) -> LiveStatistics:
        self._scope.enter_context(observe_timers(self.observe_timer))
        self.publish()
        self._thread = threading.Thread(target=self._heartbeat, daemon=True)
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._stopped.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._scope.close()


def read_live_statistics(
    redis: StatisticsCache, project_id: str, job: dict[str, Any]
) -> dict[str, Any] | None:
    """Accept only a fresh snapshot belonging to the currently executing job."""
    if job.get("status") != "running":
        return None
    try:
        raw = redis.get(f"project:{project_id}:stats")
        if not raw:
            return None
        snapshot = json.loads(raw)
        if snapshot["project_id"] != project_id or snapshot["run_id"] != job.get("run_id"):
            return None
        timestamp = datetime.fromisoformat(snapshot["updated_at"])
        age = (datetime.now(timezone.utc) - timestamp).total_seconds()
        if not 0 <= age < LIVE_TTL_SECONDS:
            return None
        return {
            "usage": snapshot["usage"],
            "timing": snapshot["timing"],
            "live": {
                "run_id": snapshot["run_id"],
                "updated_at": snapshot["updated_at"],
                "valid_for_seconds": LIVE_TTL_SECONDS - age,
            },
        }
    except (ValueError, TypeError, KeyError, OSError):
        return None
