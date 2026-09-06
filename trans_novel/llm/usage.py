"""Thread-safe token usage accounting, deltas and persisted merges."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

_USAGE_FIELDS = (
    "calls",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cache_hit_tokens",
    "cache_miss_tokens",
)


@dataclass(frozen=True)
class UsageSample:
    """Normalized provider usage for one call."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0


def read_usage_value(usage: Any, name: str) -> Any:
    """Read a field from an SDK object or dictionary, distinguishing absence from zero."""
    if usage is None:
        return None
    value = getattr(usage, name, None)
    if value is None and isinstance(usage, dict):
        return usage.get(name)
    return value


def read_usage_int(usage: Any, name: str) -> int:
    """Read integer usage fields; return zero for missing or nonnumeric values."""
    value = read_usage_value(usage, name)
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def make_usage_sample(
    usage: Any,
    *,
    cache_hit_tokens: int = 0,
    cache_miss_tokens: int = 0,
) -> UsageSample | None:
    """Build a provider-independent usage record from common API token fields."""
    if usage is None:
        return None
    prompt_tokens = read_usage_int(usage, "prompt_tokens")
    completion_tokens = read_usage_int(usage, "completion_tokens")
    total_tokens = read_usage_int(usage, "total_tokens") or (prompt_tokens + completion_tokens)
    return UsageSample(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cache_hit_tokens=max(0, cache_hit_tokens),
        cache_miss_tokens=max(0, cache_miss_tokens),
    )


def _hit_rate(hit: int, miss: int) -> float:
    """Compute the cache-token hit rate, or zero when no tokens are available."""
    total = hit + miss
    return round(hit / total, 4) if total else 0.0


def _normalize_usage_group(
    group: dict[str, dict[str, int]],
) -> dict[str, dict[str, Any]]:
    """Normalize usage slots and recompute each slot's cache hit rate."""
    normalized: dict[str, dict[str, Any]] = {
        name: {field: read_usage_int(values, field) for field in _USAGE_FIELDS}
        for name, values in group.items()
    }
    for slot in normalized.values():
        slot["cache_hit_rate"] = _hit_rate(slot["cache_hit_tokens"], slot["cache_miss_tokens"])
    return normalized


def _usage_summary(
    by_tier: dict[str, dict[str, int]],
    by_stage: dict[str, dict[str, int]],
) -> dict[str, Any]:
    """Build normalized totals from tiers only; stages are another attribution of the same
    usage.
    """
    tiers = _normalize_usage_group(by_tier)
    stages = _normalize_usage_group(by_stage)
    totals: dict[str, Any] = dict.fromkeys(_USAGE_FIELDS, 0)
    for values in tiers.values():
        for field in _USAGE_FIELDS:
            totals[field] += values[field]
    totals["cache_hit_rate"] = _hit_rate(totals["cache_hit_tokens"], totals["cache_miss_tokens"])
    return {"totals": totals, "by_tier": tiers, "by_stage": stages}


def _usage_group_delta(
    current: dict[str, dict[str, int]], previous: dict[str, dict[str, int]]
) -> dict[str, dict[str, int]]:
    """Compute nonnegative cumulative deltas by slot and remove all-zero slots."""
    delta: dict[str, dict[str, int]] = {}
    for name, values in current.items():
        old = previous.get(name) or {}
        slot = {
            field: max(
                0,
                read_usage_int(values, field) - read_usage_int(old, field),
            )
            for field in _USAGE_FIELDS
        }
        if any(slot.values()):
            delta[name] = slot
    return delta


def _merge_usage_groups(
    *groups: dict[str, dict[str, int]],
) -> dict[str, dict[str, int]]:
    """Add usage records field by field within each slot."""
    merged: dict[str, dict[str, int]] = {}
    for group in groups:
        for name, values in group.items():
            slot = merged.setdefault(name, dict.fromkeys(_USAGE_FIELDS, 0))
            for field in _USAGE_FIELDS:
                slot[field] += read_usage_int(values, field)
    return merged


def usage_delta(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    """Compute a nonnegative delta between cumulative snapshots to avoid duplicate persistence."""
    tier_delta = _usage_group_delta(current["by_tier"], previous["by_tier"])
    stage_delta = _usage_group_delta(current["by_stage"], previous["by_stage"])
    return _usage_summary(tier_delta, stage_delta)


def merge_usage_summaries(accumulated: dict[str, Any], increment: dict[str, Any]) -> dict[str, Any]:
    """Merge one run's usage delta into the book's historical totals."""
    tiers = _merge_usage_groups(accumulated["by_tier"], increment["by_tier"])
    stages = _merge_usage_groups(accumulated["by_stage"], increment["by_stage"])
    return _usage_summary(tiers, stages)


class UsageTracker:
    """Accumulate normalized usage under a lock, attributing independently by tier and stage."""

    def __init__(self) -> None:
        """Initialize tier and stage attribution views; derive totals from tiers only."""
        self._lock = threading.Lock()
        self._by_tier: dict[str, dict[str, int]] = {}
        self._by_stage: dict[str, dict[str, int]] = {}

    def record(
        self,
        tier: str,
        sample: UsageSample | None,
        stage: str | None = None,
    ) -> None:
        """Accumulate normalized provider usage; silently skip absent records."""
        if sample is None:
            return
        with self._lock:
            slots = [self._by_tier.setdefault(tier, dict.fromkeys(_USAGE_FIELDS, 0))]
            if stage:
                slots.append(self._by_stage.setdefault(stage, dict.fromkeys(_USAGE_FIELDS, 0)))
            for slot in slots:
                slot["calls"] += 1
                slot["prompt_tokens"] += sample.prompt_tokens
                slot["completion_tokens"] += sample.completion_tokens
                slot["total_tokens"] += sample.total_tokens
                slot["cache_hit_tokens"] += sample.cache_hit_tokens
                slot["cache_miss_tokens"] += sample.cache_miss_tokens

    def summary(self) -> dict[str, Any]:
        """Return totals, by_tier and by_stage, each with cache_hit_rate."""
        with self._lock:
            by_tier = {tier: dict(values) for tier, values in self._by_tier.items()}
            by_stage = {stage: dict(values) for stage, values in self._by_stage.items()}
        return _usage_summary(by_tier, by_stage)
