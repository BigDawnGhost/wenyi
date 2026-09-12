"""线程安全的 Token 用量统计、增量计算与持久化合并（schema v2）。

归因维度（同一物理/逻辑事件在多个维度各计一次，totals 只计一次）：
- totals：每个带 usage 的 provider 响应直接累加一次；各归因维度分别统计，
  不再反向汇总到 totals，以免重复计数。
- by_agent：按功能 Agent（translator/editor/analyst/preparer），token 字段 + logical_calls/attempts/failed_attempts/
  elapsed_ms/reasoning_tokens/accepted/rejected/fallbacks。
- by_operation：按内部 operation（业务标签），字段集合与 by_agent 完全相同。
- by_provider / by_model：token 字段 + attempts/failed_attempts（无逻辑/结果计数）。
- by_stage：诊断维度，只记 token 字段。

快照键严格为 schema_version / totals / by_agent / by_operation / by_provider /
by_model / by_stage。merge_usage_summaries 只接受同 schema v2 的累计与增量：
空 {} 允许作为全新累计快照的初始化，任何非空且 schema_version != 2 的快照
直接抛 ValueError（不支持旧 schema 迁移/转换）。
"""

from __future__ import annotations

import threading
from typing import Any

from trans_novel.config import ModelRef

SCHEMA_VERSION = 2

_USAGE_FIELDS = (
    "calls",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cache_hit_tokens",
    "cache_miss_tokens",
)
_AGENT_EXTRA_FIELDS = (
    "logical_calls",
    "attempts",
    "failed_attempts",
    "elapsed_ms",
    "reasoning_tokens",
    "accepted",
    "rejected",
    "fallbacks",
)
_AGENT_FIELDS = _USAGE_FIELDS + _AGENT_EXTRA_FIELDS
_PHYSICAL_FIELDS = (*_USAGE_FIELDS, "attempts", "failed_attempts")
_PROVIDER_FIELDS = _PHYSICAL_FIELDS
_MODEL_FIELDS = _PHYSICAL_FIELDS

_TOKEN_DELTAS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cache_hit_tokens",
    "cache_miss_tokens",
)


def _usage_value(usage: Any, name: str) -> tuple[bool, object]:
    """Read an optional usage field without allowing descriptor failures out."""
    if usage is None:
        return False, None
    try:
        if isinstance(usage, dict):
            if name not in usage:
                return False, None
            return True, usage[name]
        return True, getattr(usage, name)
    except Exception:
        return False, None


def _has_field(usage: Any, name: str) -> bool:
    return _usage_value(usage, name)[0]


def _usage_int(usage: Any, name: str) -> int:
    """Read a nonnegative token count; bool, invalid, and missing values are zero."""
    present, value = _usage_value(usage, name)
    if not present:
        return 0
    if isinstance(value, bool):
        return 0
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(result, 0)


def _reasoning_tokens(usage: Any) -> int:
    """Read direct reasoning tokens, then provider completion/output details."""
    if _has_field(usage, "reasoning_tokens"):
        return _usage_int(usage, "reasoning_tokens")
    for field in ("completion_tokens_details", "output_tokens_details"):
        _, details = _usage_value(usage, field)
        if _has_field(details, "reasoning_tokens"):
            return _usage_int(details, "reasoning_tokens")
    return 0


def has_response_usage(usage: object) -> bool:
    """Whether a response exposes at least one recognized primary usage field."""
    return usage is not None and any(
        _has_field(usage, field)
        for field in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "input_tokens",
            "output_tokens",
        )
    )


def normalize_response_usage(usage: object) -> dict[str, int]:
    """Normalize provider usage into the six persisted/telemetry token fields."""
    prompt = (
        _usage_int(usage, "prompt_tokens")
        if _has_field(usage, "prompt_tokens")
        else _usage_int(usage, "input_tokens")
    )
    completion = (
        _usage_int(usage, "completion_tokens")
        if _has_field(usage, "completion_tokens")
        else _usage_int(usage, "output_tokens")
    )
    total = (
        _usage_int(usage, "total_tokens")
        if _has_field(usage, "total_tokens")
        else prompt + completion
    )
    if _has_field(usage, "prompt_cache_hit_tokens"):
        hit = _usage_int(usage, "prompt_cache_hit_tokens")
    else:
        details_field = (
            "prompt_tokens_details"
            if _has_field(usage, "prompt_tokens_details")
            else "input_tokens_details"
        )
        _, details = _usage_value(usage, details_field)
        hit = _usage_int(details, "cached_tokens")
    if _has_field(usage, "prompt_cache_miss_tokens"):
        miss = _usage_int(usage, "prompt_cache_miss_tokens")
    else:
        miss = max(prompt - hit, 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cache_hit_tokens": hit,
        "cache_miss_tokens": miss,
        "reasoning_tokens": _reasoning_tokens(usage),
    }


def _hit_rate(hit: int, miss: int) -> float:
    total = hit + miss
    return round(hit / total, 4) if total else 0.0


def _normalize_slot(values: dict, fields: tuple[str, ...]) -> dict[str, int]:
    return {field: _usage_int(values, field) for field in fields}


def _slot(fields: tuple[str, ...]) -> dict[str, int]:
    return dict.fromkeys(fields, 0)


def _usage_summary_from_parts(
    *,
    by_agent: dict[str, dict],
    by_operation: dict[str, dict],
    by_provider: dict[str, dict],
    by_model: dict[str, dict],
    by_stage: dict[str, dict],
    totals: dict,
) -> dict[str, Any]:
    """由各归因维度生成规范汇总；cache_hit_rate 一律由 hit/miss 重算。"""
    agents = {name: _normalize_slot(values, _AGENT_FIELDS) for name, values in by_agent.items()}
    operations = {
        name: _normalize_slot(values, _AGENT_FIELDS) for name, values in by_operation.items()
    }
    providers = {
        name: _normalize_slot(values, _PROVIDER_FIELDS) for name, values in by_provider.items()
    }
    models = {name: _normalize_slot(values, _MODEL_FIELDS) for name, values in by_model.items()}
    stages = {name: _normalize_slot(values, _USAGE_FIELDS) for name, values in by_stage.items()}
    total = _normalize_slot(totals, _USAGE_FIELDS)
    for slot in (
        *agents.values(),
        *operations.values(),
        *providers.values(),
        *models.values(),
        *stages.values(),
        total,
    ):
        slot["cache_hit_rate"] = _hit_rate(slot["cache_hit_tokens"], slot["cache_miss_tokens"])
    return {
        "schema_version": SCHEMA_VERSION,
        "totals": total,
        "by_agent": agents,
        "by_operation": operations,
        "by_provider": providers,
        "by_model": models,
        "by_stage": stages,
    }


def _nonneg_delta(
    current: dict[str, dict],
    previous: dict[str, dict],
    fields: tuple[str, ...],
) -> dict[str, dict[str, int]]:
    delta: dict[str, dict[str, int]] = {}
    for key, values in current.items():
        old = previous.get(key) or {}
        slot = {
            field: max(0, _usage_int(values, field) - _usage_int(old, field)) for field in fields
        }
        if any(slot.values()):
            delta[key] = slot
    return delta


def _totals_delta(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, int]:
    return {
        field: max(0, _usage_int(current, field) - _usage_int(previous, field))
        for field in _USAGE_FIELDS
    }


def usage_delta(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    """计算两个 v2 快照间的非负增量，避免重复落盘。"""
    return _usage_summary_from_parts(
        by_agent=_nonneg_delta(
            current.get("by_agent") or {}, previous.get("by_agent") or {}, _AGENT_FIELDS
        ),
        by_operation=_nonneg_delta(
            current.get("by_operation") or {}, previous.get("by_operation") or {}, _AGENT_FIELDS
        ),
        by_provider=_nonneg_delta(
            current.get("by_provider") or {}, previous.get("by_provider") or {}, _PROVIDER_FIELDS
        ),
        by_model=_nonneg_delta(
            current.get("by_model") or {}, previous.get("by_model") or {}, _MODEL_FIELDS
        ),
        by_stage=_nonneg_delta(
            current.get("by_stage") or {}, previous.get("by_stage") or {}, _USAGE_FIELDS
        ),
        totals=_totals_delta(current.get("totals") or {}, previous.get("totals") or {}),
    )


def _merge_slots(
    target: dict[str, dict[str, int]], source: dict | None, fields: tuple[str, ...]
) -> None:
    for key, values in (source or {}).items():
        slot = target.setdefault(key, dict.fromkeys(fields, 0))
        for field in fields:
            slot[field] += _usage_int(values, field)


def _require_v2(summary: dict[str, Any], label: str) -> None:
    """非空快照必须声明 schema_version == 2；不支持旧 schema 的迁移/转换。"""
    if summary and summary.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"{label}：不支持的 usage 快照 schema（schema_version="
            f"{summary.get('schema_version')!r}，需要 {SCHEMA_VERSION}；"
            "旧版快照不再迁移，请从空累计重新开始）"
        )


def merge_usage_summaries(accumulated: dict[str, Any], increment: dict[str, Any]) -> dict[str, Any]:
    """将一次运行增量合并进历史累计用量（同 schema v2，各维度分别求和）。

    空 {} 允许作为全新累计快照的初始化；任何非空累计/增量缺少
    schema_version == 2 时抛出 ValueError（不支持旧 schema 迁移）。
    totals 按字段分别相加（每个带 usage 的响应在 totals 只计一次），
    不反向从各归因维度推导。
    """
    _require_v2(accumulated, "accumulated usage")
    _require_v2(increment, "usage increment")

    agents: dict[str, dict[str, int]] = {}
    _merge_slots(agents, accumulated.get("by_agent"), _AGENT_FIELDS)
    _merge_slots(agents, increment.get("by_agent"), _AGENT_FIELDS)

    operations: dict[str, dict[str, int]] = {}
    _merge_slots(operations, accumulated.get("by_operation"), _AGENT_FIELDS)
    _merge_slots(operations, increment.get("by_operation"), _AGENT_FIELDS)

    providers: dict[str, dict[str, int]] = {}
    _merge_slots(providers, accumulated.get("by_provider"), _PROVIDER_FIELDS)
    _merge_slots(providers, increment.get("by_provider"), _PROVIDER_FIELDS)

    models: dict[str, dict[str, int]] = {}
    _merge_slots(models, accumulated.get("by_model"), _MODEL_FIELDS)
    _merge_slots(models, increment.get("by_model"), _MODEL_FIELDS)

    stages: dict[str, dict[str, int]] = {}
    _merge_slots(stages, accumulated.get("by_stage"), _USAGE_FIELDS)
    _merge_slots(stages, increment.get("by_stage"), _USAGE_FIELDS)

    totals: dict[str, int] = dict.fromkeys(_USAGE_FIELDS, 0)
    for source in (accumulated.get("totals"), increment.get("totals")):
        for field in _USAGE_FIELDS:
            totals[field] += _usage_int(source, field)

    return _usage_summary_from_parts(
        by_agent=agents,
        by_operation=operations,
        by_provider=providers,
        by_model=models,
        by_stage=stages,
        totals=totals,
    )


class UsageTracker:
    """线程安全的用量累加器；实际请求及响应的用量由 Provider 传输记账，
    逻辑调用/结果/候选切换由 AgentRouter 与业务调用方记账。

    每个物理/逻辑事件同时计入 by_agent（功能 Agent）与 by_operation（业务
    标签）两个视图；totals 只在带 usage 的响应处累加一次。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._totals = dict.fromkeys(_USAGE_FIELDS, 0)
        self._by_agent: dict[str, dict[str, int]] = {}
        self._by_operation: dict[str, dict[str, int]] = {}
        self._by_provider: dict[str, dict[str, int]] = {}
        self._by_model: dict[str, dict[str, int]] = {}
        self._by_stage: dict[str, dict[str, int]] = {}
        self._persistence: Any = None
        self._active_attempts = 0
        self._active_scopes = 0

    def bind_persistence(self, persistence: Any) -> None:
        with self._lock:
            if self._active_attempts or self._active_scopes:
                raise RuntimeError("cannot rebind usage while accounting is active")
            self._persistence = persistence

    def has_active_work(self) -> bool:
        with self._lock:
            return bool(self._active_attempts or self._active_scopes)

    def begin_scope(self) -> None:
        with self._lock:
            self._active_scopes += 1

    def end_scope(self) -> None:
        with self._lock:
            if self._active_scopes <= 0:
                raise RuntimeError("usage scope is not active")
            self._active_scopes -= 1

    def begin_attempt(self) -> None:
        with self._lock:
            self._active_attempts += 1

    def _finish_attempt(self) -> None:
        if self._active_attempts:
            self._active_attempts -= 1

    def _persist_locked(self) -> None:
        if self._persistence is not None:
            self._persistence.persist(self._summary_locked())

    def _summary_locked(self) -> dict[str, Any]:
        return _usage_summary_from_parts(
            by_agent={name: dict(values) for name, values in self._by_agent.items()},
            by_operation={name: dict(values) for name, values in self._by_operation.items()},
            by_provider={name: dict(values) for name, values in self._by_provider.items()},
            by_model={name: dict(values) for name, values in self._by_model.items()},
            by_stage={name: dict(values) for name, values in self._by_stage.items()},
            totals=dict(self._totals),
        )

    # ── 物理尝试（provider 传输调用）────────────────────────────────────
    def record_attempt_result(
        self,
        *,
        agent: str,
        operation: str,
        provider: str | None = None,
        model_ref: ModelRef | None = None,
        usage: Any = None,
        stage: str | None = None,
        failed: bool = False,
    ) -> None:
        """Atomically account one completed physical attempt and persist it."""
        normalized = normalize_response_usage(usage) if usage is not None else None
        with self._lock:
            self._finish_attempt()
            for values in (
                self._by_agent.setdefault(agent, _slot(_AGENT_FIELDS)),
                self._by_operation.setdefault(operation, _slot(_AGENT_FIELDS)),
            ):
                values["attempts"] += 1
                if failed:
                    values["failed_attempts"] += 1
            if provider:
                values = self._by_provider.setdefault(provider, _slot(_PROVIDER_FIELDS))
                values["attempts"] += 1
                if failed:
                    values["failed_attempts"] += 1
            if model_ref is not None:
                values = self._by_model.setdefault(model_ref.full_name, _slot(_MODEL_FIELDS))
                values["attempts"] += 1
                if failed:
                    values["failed_attempts"] += 1
            if normalized is not None:
                self._record_usage_locked(
                    agent=agent,
                    operation=operation,
                    provider=provider,
                    model_ref=model_ref,
                    stage=stage,
                    normalized=normalized,
                )
            self._persist_locked()

    def record_attempt(
        self,
        *,
        agent: str,
        operation: str,
        provider: str | None = None,
        model_ref: ModelRef | None = None,
    ) -> None:
        """Compatibility for non-provider callers; production uses terminal attempts."""
        with self._lock:
            self._by_agent.setdefault(agent, _slot(_AGENT_FIELDS))["attempts"] += 1
            self._by_operation.setdefault(operation, _slot(_AGENT_FIELDS))["attempts"] += 1
            if provider:
                self._by_provider.setdefault(provider, _slot(_PROVIDER_FIELDS))["attempts"] += 1
            if model_ref is not None:
                self._by_model.setdefault(model_ref.full_name, _slot(_MODEL_FIELDS))[
                    "attempts"
                ] += 1
            self._persist_locked()

    def record_attempt_failed(
        self,
        *,
        agent: str,
        operation: str,
        provider: str | None = None,
        model_ref: ModelRef | None = None,
    ) -> None:
        """Compatibility for non-provider callers."""
        with self._lock:
            self._by_agent.setdefault(agent, _slot(_AGENT_FIELDS))["failed_attempts"] += 1
            self._by_operation.setdefault(operation, _slot(_AGENT_FIELDS))["failed_attempts"] += 1
            if provider:
                self._by_provider.setdefault(provider, _slot(_PROVIDER_FIELDS))[
                    "failed_attempts"
                ] += 1
            if model_ref is not None:
                self._by_model.setdefault(model_ref.full_name, _slot(_MODEL_FIELDS))[
                    "failed_attempts"
                ] += 1
            self._persist_locked()

    # ── 响应用量（provider 传输调用，响应带 usage 时）────────────────────
    def record(
        self,
        *,
        agent: str,
        operation: str,
        provider: str | None = None,
        model_ref: ModelRef | None = None,
        usage: Any = None,
        stage: str | None = None,
    ) -> None:
        """Compatibility for direct callers; provider transports use terminal attempts."""
        if usage is None:
            return
        normalized = normalize_response_usage(usage)
        with self._lock:
            self._record_usage_locked(
                agent=agent,
                operation=operation,
                provider=provider,
                model_ref=model_ref,
                stage=stage,
                normalized=normalized,
            )
            self._persist_locked()

    def _record_usage_locked(
        self,
        *,
        agent: str,
        operation: str,
        provider: str | None,
        model_ref: ModelRef | None,
        stage: str | None,
        normalized: dict[str, int],
    ) -> None:
        token_values = {field: normalized[field] for field in _TOKEN_DELTAS}
        reasoning_tokens = normalized["reasoning_tokens"]
        self._totals["calls"] += 1
        for field in _TOKEN_DELTAS:
            self._totals[field] += token_values[field]
        for values in (
            self._by_agent.setdefault(agent, _slot(_AGENT_FIELDS)),
            self._by_operation.setdefault(operation, _slot(_AGENT_FIELDS)),
        ):
            values["calls"] += 1
            for field in _TOKEN_DELTAS:
                values[field] += token_values[field]
            values["reasoning_tokens"] += reasoning_tokens
        for values in (
            self._by_provider.setdefault(provider, _slot(_PROVIDER_FIELDS)) if provider else None,
            self._by_model.setdefault(model_ref.full_name, _slot(_MODEL_FIELDS))
            if model_ref is not None
            else None,
        ):
            if values is not None:
                values["calls"] += 1
                for field in _TOKEN_DELTAS:
                    values[field] += token_values[field]
        if stage:
            values = self._by_stage.setdefault(stage, _slot(_USAGE_FIELDS))
            values["calls"] += 1
            for field in _TOKEN_DELTAS:
                values[field] += token_values[field]

    # ── 逻辑调用 / 结果 / 降级（by_agent 与 by_operation 同时计入）────────
    def record_logical_call(self, agent: str, operation: str, elapsed_ms: float) -> None:
        with self._lock:
            slot = self._by_agent.setdefault(agent, _slot(_AGENT_FIELDS))
            slot["logical_calls"] += 1
            slot["elapsed_ms"] += round(elapsed_ms)
            slot = self._by_operation.setdefault(operation, _slot(_AGENT_FIELDS))
            slot["logical_calls"] += 1
            slot["elapsed_ms"] += round(elapsed_ms)
            self._persist_locked()

    def record_outcome(self, agent: str, operation: str, *, accepted: bool) -> None:
        with self._lock:
            self._by_agent.setdefault(agent, _slot(_AGENT_FIELDS))[
                "accepted" if accepted else "rejected"
            ] += 1
            self._by_operation.setdefault(operation, _slot(_AGENT_FIELDS))[
                "accepted" if accepted else "rejected"
            ] += 1
            self._persist_locked()

    def record_fallback(self, agent: str, operation: str) -> None:
        with self._lock:
            self._by_agent.setdefault(agent, _slot(_AGENT_FIELDS))["fallbacks"] += 1
            self._by_operation.setdefault(operation, _slot(_AGENT_FIELDS))["fallbacks"] += 1
            self._persist_locked()

    def summary(self) -> dict[str, Any]:
        with self._lock:
            return self._summary_locked()
