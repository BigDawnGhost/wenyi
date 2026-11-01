"""Run-owned usage checkpoint and recoverable book/Review ledger publication."""

from __future__ import annotations

from typing import Any, Protocol

from ..llm.usage import empty_usage, merge_usage_summaries, usage_delta


class UsageSource(Protocol):
    def usage_summary(self) -> dict[str, Any]: ...


class UsageLedger(Protocol):
    def load_usage(self) -> dict[str, Any] | None: ...
    def prepare_usage_commit(self, ledgers: dict[str, dict]) -> None: ...
    def recover_usage(self) -> None: ...
    def log_event(self, event: str, **payload: Any) -> None: ...


class ReviewUsageLedger(Protocol):
    @property
    def review_id(self) -> str: ...
    def load_usage(self) -> dict[str, Any] | None: ...
    def save_usage(self, data: dict[str, Any]) -> None: ...


class UsageCoordinator:
    """Own one cumulative client checkpoint, independent of agents and runtime assembly.

    The durable journal must precede the in-memory checkpoint. Once prepared, recovery
    publishes absolute totals, so retries in this process or a new one cannot replay a delta.
    Callers serialize flushes under the existing run lock; this adds no second lock domain.
    """

    def __init__(self, source: UsageSource):
        self._source = source
        self._checkpoint = source.usage_summary()

    def flush(
        self,
        store: UsageLedger,
        *,
        scope: str,
        review: ReviewUsageLedger | None = None,
    ) -> dict[str, Any]:
        store.recover_usage()
        current = self._source.usage_summary()
        increment = usage_delta(current, self._checkpoint)
        accumulated = store.load_usage() or empty_usage()
        if not increment["totals"]["calls"]:
            if review is not None and review.load_usage() is None:
                review.save_usage(empty_usage())
            return merge_usage_summaries(accumulated, increment)
        cumulative = merge_usage_summaries(accumulated, increment)
        ledgers = {"usage.json": cumulative}
        if review is not None:
            ledgers[f"reviews/{review.review_id}/usage.json"] = merge_usage_summaries(
                review.load_usage() or empty_usage(), increment
            )
        store.prepare_usage_commit(ledgers)
        self._checkpoint = current
        store.recover_usage()
        store.log_event(
            "usage_summary",
            scope=scope,
            increment=increment,
            cumulative=cumulative,
        )
        return cumulative
