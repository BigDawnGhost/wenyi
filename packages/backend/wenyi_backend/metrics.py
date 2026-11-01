"""Task and model counters exposed as a scrape-ready text response.

Two rules govern everything here:

1. Observability never changes behaviour. Recording a sample is bounded and
   silent, so measuring cannot fail the request or the run that produced it.
2. Counters are keyed by `call_id`, not by log line. The event ledger already
   merges a retried call once; counting lines would double every retry.

The registry renders its own text rather than pulling in a client library.
The deployment decision (D-5 option C) keeps scraping out of scope for now, so
a dependency would buy nothing yet; `render()` already emits the exposition
format a scraper would read.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class _Registry:
    """Counters plus the set of call ids already counted."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    tasks: dict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))
    retries: dict[tuple[str, str], int] = field(default_factory=lambda: defaultdict(int))
    depth: int = 0
    seen_calls: set[str] = field(default_factory=set)
    # Bound the dedupe set so a long-lived process cannot grow without limit.
    max_seen: int = 20_000


_REGISTRY = _Registry()


def record_task(*, status: str, code: str) -> None:
    """Count one finished task by its outcome; never raises."""
    try:
        with _REGISTRY.lock:
            _REGISTRY.tasks[(status, code)] += 1
    except Exception:  # noqa: BLE001 - a counter must not fail the caller
        log.warning("Could not record a task sample", exc_info=False)


def record_retry(*, provider: str, reason: str, call_id: str | None = None) -> None:
    """Count one model retry, ignoring a repeat report for the same call."""
    try:
        with _REGISTRY.lock:
            if call_id is not None:
                if call_id in _REGISTRY.seen_calls:
                    return
                if len(_REGISTRY.seen_calls) >= _REGISTRY.max_seen:
                    _REGISTRY.seen_calls.clear()
                _REGISTRY.seen_calls.add(call_id)
            _REGISTRY.retries[(provider, reason)] += 1
    except Exception:  # noqa: BLE001 - a counter must not fail the caller
        log.warning("Could not record a retry sample", exc_info=False)


def record_queue_depth(*, depth: int) -> None:
    try:
        with _REGISTRY.lock:
            _REGISTRY.depth = max(0, depth)
    except Exception:  # noqa: BLE001
        log.warning("Could not record the queue depth", exc_info=False)


def reset() -> None:
    """Drop every sample. Only tests should need this."""
    with _REGISTRY.lock:
        _REGISTRY.tasks.clear()
        _REGISTRY.retries.clear()
        _REGISTRY.depth = 0
        _REGISTRY.seen_calls.clear()


def _labels(pairs: dict[str, str]) -> str:
    if not pairs:
        return ""
    rendered = ",".join(f'{key}="{value}"' for key, value in sorted(pairs.items()))
    return f"{{{rendered}}}"


def render() -> str:
    """Render the exposition text, sorted so two scrapes are easy to diff."""
    lines: list[str] = []
    with _REGISTRY.lock:
        lines.append("# HELP wenyi_tasks_total Finished pipeline tasks by outcome.")
        lines.append("# TYPE wenyi_tasks_total counter")
        for (status, code), value in sorted(_REGISTRY.tasks.items()):
            lines.append(f"wenyi_tasks_total{_labels({'status': status, 'code': code})} {value}")

        lines.append("# HELP wenyi_llm_retries_total Model retries by provider and reason.")
        lines.append("# TYPE wenyi_llm_retries_total counter")
        for (provider, reason), value in sorted(_REGISTRY.retries.items()):
            labels = _labels({"provider": provider, "reason": reason})
            lines.append(f"wenyi_llm_retries_total{labels} {value}")

        lines.append("# HELP wenyi_queue_depth Tasks waiting in the queue.")
        lines.append("# TYPE wenyi_queue_depth gauge")
        lines.append(f"wenyi_queue_depth {_REGISTRY.depth}")
    return "\n".join(lines) + "\n"
