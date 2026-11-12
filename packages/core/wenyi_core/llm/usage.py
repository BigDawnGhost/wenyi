"""Thread-safe token usage accounting, deltas and persisted merges."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

_LEGACY_FIELDS = (
    "calls",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cache_hit_tokens",
    "cache_miss_tokens",
)
# Presence counts are additive across calls, unlike a boolean "reported" flag.
# They preserve explicit zero, partial coverage and absence in cumulative ledgers.
_REPORTED_FIELDS = {
    "cache_hit_tokens": "cache_hit_reported_calls",
    "cache_write_tokens": "cache_write_reported_calls",
    "uncached_tokens": "uncached_reported_calls",
    "cache_write_5m_tokens": "cache_write_5m_reported_calls",
    "cache_write_1h_tokens": "cache_write_1h_reported_calls",
}
_USAGE_FIELDS = (
    *_LEGACY_FIELDS,
    "cache_write_tokens",
    "uncached_tokens",
    "cache_unknown_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
    *_REPORTED_FIELDS.values(),
)


@dataclass(frozen=True)
class UsageSample:
    """One inclusive input total; None means a cache counter was not available.

    cache_miss_tokens is retained only as a compatibility counter. It cannot
    distinguish writes from ordinary input and is never used for that purpose.
    """

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cache_hit_tokens: int | None = None
    cache_miss_tokens: int = 0
    cache_write_tokens: int | None = None
    uncached_tokens: int | None = None
    cache_write_5m_tokens: int | None = None
    cache_write_1h_tokens: int | None = None

    @property
    def cache_unknown_tokens(self) -> int:
        return max(
            0,
            self.prompt_tokens
            - (self.cache_hit_tokens or 0)
            - (self.cache_write_tokens or 0)
            - (self.uncached_tokens or 0),
        )


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


def read_usage_counter(usage: Any, name: str) -> int | None:
    """Read a nonnegative integer counter without turning missing/invalid data into zero."""
    value = read_usage_value(usage, name)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def make_usage_sample(
    usage: Any,
    *,
    cache_hit_tokens: int | None = None,
    cache_miss_tokens: int = 0,
    cache_write_tokens: int | None = None,
    uncached_tokens: int | None = None,
    cache_write_5m_tokens: int | None = None,
    cache_write_1h_tokens: int | None = None,
) -> UsageSample | None:
    """Build usage from inclusive totals supplied by an adapter, not native wire fields."""
    if usage is None:
        return None
    prompt_tokens = read_usage_int(usage, "prompt_tokens")
    completion_tokens = read_usage_int(usage, "completion_tokens")
    total_tokens = read_usage_int(usage, "total_tokens") or (prompt_tokens + completion_tokens)
    known = (cache_hit_tokens, cache_write_tokens, uncached_tokens)
    if (
        any(value is not None and value < 0 for value in known)
        or sum(value or 0 for value in known) > prompt_tokens
    ):
        # Keep billable totals, but do not publish an impossible cache partition.
        cache_hit_tokens = cache_write_tokens = uncached_tokens = None
        cache_write_5m_tokens = cache_write_1h_tokens = None
        cache_miss_tokens = 0
    ttl_total = (cache_write_5m_tokens or 0) + (cache_write_1h_tokens or 0)
    if ttl_total > (cache_write_tokens if cache_write_tokens is not None else prompt_tokens):
        cache_write_5m_tokens = cache_write_1h_tokens = None
    return UsageSample(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cache_hit_tokens=cache_hit_tokens,
        cache_miss_tokens=max(0, cache_miss_tokens),
        cache_write_tokens=cache_write_tokens,
        uncached_tokens=uncached_tokens,
        cache_write_5m_tokens=cache_write_5m_tokens,
        cache_write_1h_tokens=cache_write_1h_tokens,
    )


def _hit_rate(slot: dict[str, Any]) -> float | None:
    """Reported reads / all input, including unknown input; never a classified-only rate."""
    total = slot["prompt_tokens"]
    known_read = slot["cache_hit_reported_calls"] or slot["cache_hit_tokens"]
    return round(slot["cache_hit_tokens"] / total, 4) if total and known_read else None


def _normalize_usage_group(
    group: dict[str, dict[str, int]],
) -> dict[str, dict[str, Any]]:
    """Normalize usage slots and recompute each slot's cache hit rate."""
    normalized: dict[str, dict[str, Any]] = {
        name: {field: read_usage_int(values, field) for field in _USAGE_FIELDS}
        for name, values in group.items()
    }
    for slot in normalized.values():
        slot["cache_hit_rate"] = _hit_rate(slot)
    return normalized


_GROUPS = ("by_tier", "by_stage", "by_provider", "by_model")
USAGE_SCHEMA_VERSION = 3


def _usage_summary(groups: dict[str, dict], labels: dict[str, str] | None = None) -> dict[str, Any]:
    normalized = {name: _normalize_usage_group(groups.get(name, {})) for name in _GROUPS}
    totals: dict[str, Any] = dict.fromkeys(_USAGE_FIELDS, 0)
    for values in normalized["by_tier"].values():
        for field in _USAGE_FIELDS:
            totals[field] += values[field]
    totals["cache_hit_rate"] = _hit_rate(totals)
    return {
        "schema_version": USAGE_SCHEMA_VERSION,
        "totals": totals,
        **normalized,
        "labels": dict(labels or {}),
    }


def empty_usage() -> dict[str, Any]:
    return _usage_summary({})


def validate_usage(value: dict[str, Any] | None) -> dict[str, Any]:
    """Upgrade v2 in memory; older attribution formats still require explicit conversion."""
    if not value or value == {}:
        return empty_usage()
    if not isinstance(value, dict):
        raise ValueError("Usage ledger must be an object")
    version = value.get("schema_version")
    if version not in (2, USAGE_SCHEMA_VERSION):
        if not any(value.get("by_tier", {}).values()) and not any(
            value.get("totals", {}).get(field, 0) for field in _USAGE_FIELDS
        ):
            return empty_usage()
        raise ValueError("Usage ledger needs conversion; run wenyi models migrate-usage RUN_DIR")
    if not all(isinstance(value.get(group), dict) for group in _GROUPS):
        raise ValueError("Invalid usage ledger grouping")
    if not isinstance(value.get("totals"), dict):
        raise ValueError("Invalid usage ledger totals")
    for group in _GROUPS:
        if not all(isinstance(slot, dict) for slot in value[group].values()):
            raise ValueError("Invalid usage ledger slot")
    reconstructed = _usage_summary({"by_tier": value["by_tier"]})["totals"]
    fields = _LEGACY_FIELDS if version == 2 else _USAGE_FIELDS
    if any(read_usage_int(value["totals"], field) != reconstructed[field] for field in fields):
        raise ValueError("Usage totals disagree with tier attribution")
    if version == 2:
        return _usage_summary(
            {name: _upgrade_cache_group(value[name]) for name in _GROUPS}, value.get("labels")
        )
    return value


def _upgrade_cache_group(group: dict[str, dict]) -> dict[str, dict]:
    """Preserve old misses verbatim; their write/ordinary split is unknowable."""
    upgraded = {}
    for name, slot in group.items():
        values = {field: read_usage_int(slot, field) for field in _LEGACY_FIELDS}
        if not 0 <= values["cache_hit_tokens"] <= values["prompt_tokens"]:
            raise ValueError("Historical cache reads exceed input tokens")
        values["cache_unknown_tokens"] = values["prompt_tokens"] - values["cache_hit_tokens"]
        upgraded[name] = values
    return upgraded


def convert_usage_ledger(value: dict[str, Any]) -> dict[str, Any]:
    """Convert historical attribution explicitly without guessing past providers or models."""
    if value.get("schema_version") in (2, USAGE_SCHEMA_VERSION):
        return validate_usage(value)
    if value.get("schema_version") not in (None, 1):
        raise ValueError("Unsupported usage ledger version")
    groups = {name: _upgrade_cache_group(value.get(name, {})) for name in ("by_tier", "by_stage")}
    reconstructed = _usage_summary(groups)
    totals = reconstructed["totals"]
    for field in _LEGACY_FIELDS:
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
                    if field not in _REPORTED_FIELDS.values():
                        slot[field] += getattr(sample, field) or 0
                for field, presence in _REPORTED_FIELDS.items():
                    slot[presence] += int(getattr(sample, field) is not None)
            self._labels.update(labels or {})

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return _usage_summary(self._groups, self._labels)
