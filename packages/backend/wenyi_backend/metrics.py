"""Application-owned, best-effort counters with an explicitly injected backing store."""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from typing import Literal, Protocol

log = logging.getLogger(__name__)
Metric = Literal["tasks", "retries"]
Counters = dict[str, dict[tuple[str, str], int]]
MAX_SEEN = 20_000


class CounterStore(Protocol):
    """Atomically deduplicate and increment; adapters own cross-process visibility."""

    def increment(self, metric: Metric, labels: tuple[str, str], identity: str | None) -> None: ...
    def snapshot(self) -> Counters: ...


class MemoryCounters:
    """One Desktop application's thread-safe counters, never process-global state."""

    def __init__(self, *, max_seen: int = MAX_SEEN) -> None:
        self._lock = threading.Lock()
        self._counts: Counters = {"tasks": {}, "retries": {}}
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self.max_seen = max_seen

    def increment(self, metric: Metric, labels: tuple[str, str], identity: str | None) -> None:
        with self._lock:
            if identity is not None:
                key = (metric, identity)
                if key in self._seen:
                    return
                self._seen[key] = None
                while len(self._seen) > self.max_seen:
                    self._seen.popitem(last=False)
            counts = self._counts[metric]
            counts[labels] = counts.get(labels, 0) + 1

    def snapshot(self) -> Counters:
        with self._lock:
            return {metric: dict(counts) for metric, counts in self._counts.items()}


class Metrics:
    """Observe workflow outcomes and retry waits without changing their results."""

    def __init__(self, store: CounterStore | None = None) -> None:
        self.store = store if store is not None else MemoryCounters()

    def _record(self, metric: Metric, labels: tuple[str, str], identity: str | None) -> None:
        try:
            self.store.increment(metric, labels, identity)
        except Exception:
            log.warning("Could not record a %s sample", metric, exc_info=False)

    def record_task(self, *, status: str, code: str, identity: str | None = None) -> None:
        self._record("tasks", (status, code), identity)

    def record_retry(
        self, *, provider: str, reason: str, call_id: str | None = None, attempt: int = 1
    ) -> None:
        # A single call can retry more than once, including across model failover.
        identity = f"{call_id}:{attempt}" if call_id else None
        self._record("retries", (provider, reason), identity)

    def record_event(self, event: str, **data) -> None:
        if event != "llm_retry_wait":
            return
        provider, reason = data.get("provider"), data.get("reason")
        call_id = data.get("call_id")
        attempt = data.get("attempt", data.get("failed_attempt", 1))
        self.record_retry(
            provider=provider if isinstance(provider, str) else "unknown",
            reason=reason if isinstance(reason, str) else "unknown",
            call_id=call_id if isinstance(call_id, str) else None,
            attempt=attempt if type(attempt) is int else 1,
        )

    def snapshot(self) -> Counters:
        # A failed scrape is unavailable, not a fabricated zero-valued snapshot.
        return self.store.snapshot()


def _labels(pairs: dict[str, str]) -> str:
    def escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')

    rendered = ",".join(f'{key}="{escape(value)}"' for key, value in sorted(pairs.items()))
    return f"{{{rendered}}}"


def render(counts: Counters, *, depth: int) -> str:
    """Render a stable Prometheus snapshot; queue depth comes from the job catalog."""
    lines = [
        "# HELP wenyi_tasks_total Finished workflow tasks by outcome.",
        "# TYPE wenyi_tasks_total counter",
    ]
    for (status, code), value in sorted(counts["tasks"].items()):
        lines.append(f"wenyi_tasks_total{_labels({'status': status, 'code': code})} {value}")
    lines += [
        "# HELP wenyi_llm_retries_total Model retry waits by provider and reason.",
        "# TYPE wenyi_llm_retries_total counter",
    ]
    for (provider, reason), value in sorted(counts["retries"].items()):
        labels = _labels({"provider": provider, "reason": reason})
        lines.append(f"wenyi_llm_retries_total{labels} {value}")
    lines += [
        "# HELP wenyi_queue_depth Admitted workflow and export jobs waiting to run.",
        "# TYPE wenyi_queue_depth gauge",
        f"wenyi_queue_depth {max(0, depth)}",
    ]
    return "\n".join(lines) + "\n"
