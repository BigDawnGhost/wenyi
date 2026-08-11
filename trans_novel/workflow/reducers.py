"""确定性、无副作用的工作流状态 reducer。

本层只计算状态与效果，不承诺冷启动后的事件恢复。后续 repository 必须先幂等
写入内容寻址 artifact，再用同一事务提交状态与完整事件 outbox，最后由 EventSink
执行 ``append_if_absent``。``claimed_event_ids`` 只是事件所有权投影，不能替代 outbox。
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from typing import TypeVar, cast

from ..domain.workflow import (
    ArtifactRef,
    WorkflowEvent,
    WorkflowStatus,
    copy_json_value,
    validate_artifact_ref,
    validate_operation_id,
    validate_workflow_event,
)
from .patches import (
    InvalidStatePatch,
    MergeConflict,
    OperationConflict,
    PatchApplication,
    RevisionConflict,
    StatePatch,
)
from .state import ALLOWED_UPDATE_KEYS, RESERVED_UPDATE_KEYS, WorkflowState
from .transitions import _validate_state_transition
from .validation import validate_workflow_state

_Value = TypeVar("_Value")

# 控制边界只允许与状态转换同义的单个生命周期事件。业务事件必须等恢复后再提交。
_CONTROL_EVENT_TYPE_BY_TRANSITION = {
    (WorkflowStatus.RUNNING.value, WorkflowStatus.PAUSED.value): "workflow.paused",
    (WorkflowStatus.PENDING.value, WorkflowStatus.FAILED.value): "workflow.failed",
    (WorkflowStatus.RUNNING.value, WorkflowStatus.FAILED.value): "workflow.failed",
    (WorkflowStatus.PAUSED.value, WorkflowStatus.FAILED.value): "workflow.failed",
    (WorkflowStatus.PAUSED.value, WorkflowStatus.RUNNING.value): "workflow.resumed",
    (WorkflowStatus.FAILED.value, WorkflowStatus.RUNNING.value): "workflow.resumed",
}


def apply_state_patch(state: WorkflowState, patch: StatePatch) -> PatchApplication:
    """以乐观锁和操作幂等规则原子应用一个状态补丁。"""
    try:
        validate_workflow_state(state)
    except ValueError as error:
        raise InvalidStatePatch("当前 WorkflowState 无效") from error

    updates, events, artifacts, fingerprint = _validate_and_copy_patch(patch)
    applied_operations = state["applied_operations"]
    previous_fingerprint = applied_operations.get(patch.operation_id)

    # 重放检查必须早于 revision 检查；成功提交后的合法重放天然携带旧 revision。
    if previous_fingerprint is not None:
        if previous_fingerprint != fingerprint:
            raise OperationConflict(f"operation_id {patch.operation_id!r} 已用于不同补丁")
        return PatchApplication(
            state=copy.deepcopy(state),
            events=copy.deepcopy(events),
            created_artifacts=copy.deepcopy(artifacts),
            duplicate=True,
        )

    current_revision = state["revision"]
    if patch.expected_revision != current_revision:
        raise RevisionConflict(
            f"expected_revision={patch.expected_revision}，当前 revision={current_revision}"
        )
    if state["status"] == WorkflowStatus.COMPLETED.value:
        raise InvalidStatePatch("completed 工作流是终态，不能接受新的普通补丁")

    # 更新采用完整顶层切片替换，不做隐式深层 merge；调用方必须看见完整边界。
    candidate = copy.deepcopy(dict(state))
    for key, value in updates.items():
        candidate[key] = copy.deepcopy(value)
    candidate["revision"] = current_revision + 1

    # reducer 独占维护操作和事件认领账本，节点补丁不能伪造幂等历史。
    next_operations = copy.deepcopy(applied_operations)
    next_operations[patch.operation_id] = fingerprint
    candidate["applied_operations"] = next_operations
    next_claims = copy.deepcopy(state["claimed_event_ids"])
    for event in events:
        event_id = event["event_id"]
        previous_owner = next_claims.get(event_id)
        if previous_owner is not None and previous_owner != patch.operation_id:
            raise OperationConflict(
                f"event_id {event_id!r} 已由 operation_id {previous_owner!r} 认领"
            )
        next_claims[event_id] = patch.operation_id
    candidate["claimed_event_ids"] = next_claims

    # 候选状态先通过完整形状校验，再检查相邻版本之间的单调演进规则。
    try:
        validate_workflow_state(candidate)
    except ValueError as error:
        raise InvalidStatePatch(f"补丁产生了无效状态：{error}") from error
    _validate_state_transition(state, cast(WorkflowState, candidate))
    _validate_control_effects(
        state,
        cast(WorkflowState, candidate),
        events=events,
        artifacts=artifacts,
    )
    return PatchApplication(
        state=cast(WorkflowState, candidate),
        events=events,
        created_artifacts=artifacts,
        duplicate=False,
    )


def _validate_control_effects(
    current: WorkflowState,
    candidate: WorkflowState,
    *,
    events: tuple[WorkflowEvent, ...],
    artifacts: tuple[ArtifactRef, ...],
) -> None:
    """防止暂停、失败或恢复补丁夹带新产物和无关业务事件。"""
    transition = (current["status"], candidate["status"])
    expected_event_type = _CONTROL_EVENT_TYPE_BY_TRANSITION.get(transition)
    if expected_event_type is None:
        return
    if artifacts:
        raise InvalidStatePatch("控制状态转换不能提交 created_artifacts")
    if len(events) > 1 or any(event["event_type"] != expected_event_type for event in events):
        raise InvalidStatePatch(f"控制状态转换只能发布一个 {expected_event_type!r} 生命周期事件")
    if not events:
        return

    # 控制事件只携带可从状态重建的最小负载，不得作为业务正文的旁路。
    expected_payload: dict[str, object] = {}
    if expected_event_type == "workflow.failed":
        failure = candidate["failure"]
        assert failure is not None  # 完整快照校验保证 failed 候选状态有失败摘要。
        expected_payload = {
            "code": failure["code"],
            "retryable": failure["retryable"],
        }
    if events[0]["payload"] != expected_payload:
        raise InvalidStatePatch(
            f"{expected_event_type!r} 生命周期事件必须使用规范 payload {expected_payload!r}"
        )


def merge_unique_mapping(
    current: Mapping[str, _Value],
    incoming: Mapping[str, _Value],
) -> dict[str, _Value]:
    """按稳定键合并并行结果；同键异值时报错而不是最后写入获胜。"""
    merged: dict[str, _Value] = {}
    for source in (current, incoming):
        for key, value in source.items():
            if not isinstance(key, str) or not key.strip():
                raise MergeConflict("并行结果键必须是非空字符串")
            if key in merged and merged[key] != value:
                raise MergeConflict(f"并行结果键 {key!r} 收到互相矛盾的值")
            merged[key] = copy.deepcopy(value)

    # 排序消除并行分支完成顺序对序列化结果的影响。
    return {key: merged[key] for key in sorted(merged)}


def _validate_and_copy_patch(
    patch: StatePatch,
) -> tuple[
    dict[str, object],
    tuple[WorkflowEvent, ...],
    tuple[ArtifactRef, ...],
    str,
]:
    """校验补丁外壳和效果负载，并生成规范幂等指纹。"""
    try:
        validate_operation_id(patch.operation_id)
    except ValueError as error:
        raise InvalidStatePatch(str(error)) from error
    if (
        isinstance(patch.expected_revision, bool)
        or not isinstance(patch.expected_revision, int)
        or patch.expected_revision < 0
    ):
        raise InvalidStatePatch("expected_revision 必须是非负整数")
    if not isinstance(patch.updates, Mapping) or not patch.updates:
        raise InvalidStatePatch("updates 必须是非空映射")
    if any(not isinstance(key, str) for key in patch.updates):
        raise InvalidStatePatch("updates 的顶层键必须是字符串")

    update_keys = set(patch.updates)
    reserved = sorted(update_keys & RESERVED_UPDATE_KEYS)
    if reserved:
        raise InvalidStatePatch(f"补丁不能更新保留字段：{reserved}")
    unknown = sorted(update_keys - ALLOWED_UPDATE_KEYS)
    if unknown:
        raise InvalidStatePatch(f"补丁包含未知顶层字段：{unknown}")

    # 所有效果先规范化为稳定 JSON 副本，避免输入突变或编码往返改变指纹。
    try:
        normalized_updates = copy_json_value(
            dict(patch.updates),
            field="StatePatch.updates",
        )
        events = tuple(validate_workflow_event(event) for event in patch.events)
        artifacts = tuple(validate_artifact_ref(artifact) for artifact in patch.created_artifacts)
    except (TypeError, ValueError) as error:
        raise InvalidStatePatch(f"补丁效果无效：{error}") from error
    updates = cast(dict[str, object], normalized_updates)

    # 一个补丁内也不能重复认领 event_id，否则投递次数会依赖 sink 实现细节。
    event_ids = [event["event_id"] for event in events]
    if len(event_ids) != len(set(event_ids)):
        raise InvalidStatePatch("同一补丁内的 WorkflowEvent.event_id 不能重复")
    fingerprint = _patch_fingerprint(updates, events, artifacts)
    return updates, events, artifacts, fingerprint


def _patch_fingerprint(
    updates: Mapping[str, object],
    events: tuple[WorkflowEvent, ...],
    artifacts: tuple[ArtifactRef, ...],
) -> str:
    """计算确定性补丁指纹；事件负载也必须在重试间保持稳定。"""
    payload = {
        "updates": dict(updates),
        "events": list(events),
        "created_artifacts": list(artifacts),
    }
    try:
        stable_payload = copy_json_value(payload, field="StatePatch fingerprint")
        encoded = json.dumps(
            stable_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise InvalidStatePatch("补丁必须可安全序列化为 JSON") from error
    return hashlib.sha256(encoded).hexdigest()


__all__ = ["apply_state_patch", "merge_unique_mapping"]
