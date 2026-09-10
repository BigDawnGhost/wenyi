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


_GROUPS = ("by_tier", "by_stage", "by_provider", "by_model")
USAGE_SCHEMA_VERSION = 2


def _usage_summary(groups: dict[str, dict], labels: dict[str, str] | None = None) -> dict[str, Any]:
    normalized = {name: _normalize_usage_group(groups.get(name, {})) for name in _GROUPS}
    totals: dict[str, Any] = dict.fromkeys(_USAGE_FIELDS, 0)
    for values in normalized["by_tier"].values():
        for field in _USAGE_FIELDS:
            totals[field] += values[field]
    totals["cache_hit_rate"] = _hit_rate(totals["cache_hit_tokens"], totals["cache_miss_tokens"])
    return {
        "schema_version": USAGE_SCHEMA_VERSION,
        "totals": totals,
        **normalized,
        "labels": dict(labels or {}),
    }


def empty_usage() -> dict[str, Any]:
    return _usage_summary({})


def validate_usage(value: dict[str, Any] | None) -> dict[str, Any]:
    """Reject nonempty historical ledgers until explicitly converted."""
    if not value or value == {}:
        return empty_usage()
    if not isinstance(value, dict):
        raise ValueError("Usage ledger must be an object")
    if value.get("schema_version") != USAGE_SCHEMA_VERSION:
        if not any(value.get("by_tier", {}).values()) and not any(
            value.get("totals", {}).get(field, 0) for field in _USAGE_FIELDS
        ):
            return empty_usage()
        raise ValueError(
            "Usage ledger needs conversion; run trans-novel models migrate-usage RUN_DIR"
        )
    if not all(isinstance(value.get(group), dict) for group in _GROUPS):
        raise ValueError("Invalid usage ledger grouping")
    if not isinstance(value.get("totals"), dict):
        raise ValueError("Invalid usage ledger totals")
    for group in _GROUPS:
        if not all(isinstance(slot, dict) for slot in value[group].values()):
            raise ValueError("Invalid usage ledger slot")
    reconstructed = _usage_summary({"by_tier": value["by_tier"]})["totals"]
    if any(
        read_usage_int(value["totals"], field) != reconstructed[field] for field in _USAGE_FIELDS
    ):
        raise ValueError("Usage totals disagree with tier attribution")
    return value


def convert_usage_ledger(value: dict[str, Any]) -> dict[str, Any]:
    """Convert historical attribution explicitly without guessing past providers or models."""
    if value.get("schema_version") == USAGE_SCHEMA_VERSION:
        return validate_usage(value)
    if value.get("schema_version") not in (None, 1):
        raise ValueError("Unsupported usage ledger version")
    groups = {name: value.get(name, {}) for name in ("by_tier", "by_stage")}
    reconstructed = _usage_summary(groups)
    totals = reconstructed["totals"]
    for field in _USAGE_FIELDS:
        if read_usage_int(value.get("totals", {}), field) != totals[field]:
            raise ValueError(f"Historical usage totals disagree with tiers: {field}")
    if totals["calls"]:
        groups["by_provider"] = {"unknown": totals}
        groups["by_model"] = {"unknown": totals}
    return _usage_summary(groups, {"unknown": "Historical attribution unavailable"})


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
    """Compute each attribution delta without adding the independent views together."""
    current, previous = validate_usage(current), validate_usage(previous)
    return _usage_summary(
        {name: _usage_group_delta(current[name], previous[name]) for name in _GROUPS},
        current.get("labels"),
    )


def merge_usage_summaries(accumulated: dict[str, Any], increment: dict[str, Any]) -> dict[str, Any]:
    """Merge one unpersisted increment into cumulative usage."""
    accumulated, increment = validate_usage(accumulated), validate_usage(increment)
    return _usage_summary(
        {name: _merge_usage_groups(accumulated[name], increment[name]) for name in _GROUPS},
        {**accumulated.get("labels", {}), **increment.get("labels", {})},
    )


class UsageTracker:
    """One thread-safe ledger with independent attribution views."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._groups: dict[str, dict[str, dict[str, int]]] = {name: {} for name in _GROUPS}
        self._labels: dict[str, str] = {}

    def record(
        self,
        tier: str,
        sample: UsageSample | None,
        stage: str | None = None,
        *,
        provider: str = "unknown",
        model: str = "unknown",
        labels: dict[str, str] | None = None,
    ) -> None:
        if sample is None:
            return
        keys = {
            "by_tier": tier,
            "by_stage": stage or "unknown",
            "by_provider": provider,
            "by_model": model,
        }
        with self._lock:
            for group, key in keys.items():
                slot = self._groups[group].setdefault(key, dict.fromkeys(_USAGE_FIELDS, 0))
                slot["calls"] += 1
                for field in _USAGE_FIELDS[1:]:
                    slot[field] += getattr(sample, field)
            self._labels.update(labels or {})

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return _usage_summary(self._groups, self._labels)
