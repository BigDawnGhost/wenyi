"""完整工作流快照的跨切片不变量校验。

切片内部的字段规则委托给 ``slice_validation``；本模块只负责组合切片、绑定
生命周期与恢复游标。校验器是纯函数：不修复、不填默认值，也不修改输入。
"""

from __future__ import annotations

from collections.abc import Mapping

from ..domain.workflow import (
    StageStatus,
    WorkflowPhase,
    WorkflowStatus,
    validate_failure_info,
    validate_json_value,
)
from .slice_validation import (
    require_mapping,
    validate_accounting,
    validate_applied_operations,
    validate_book,
    validate_claimed_event_ids,
    validate_cursor,
    validate_exports,
    validate_glossary,
    validate_preparation,
    validate_quality,
    validate_request,
    validate_review,
    validate_titles,
    validate_translation,
    validate_understanding,
)
from .state import (
    OPTIONAL_STAGE_NAMES,
    REQUIRED_STAGE_NAMES,
    WORKFLOW_SCHEMA_VERSION,
    WORKFLOW_STAGE_NAMES,
    WORKFLOW_STATE_KEYS,
)

_TERMINAL_STAGE_STATUSES = {StageStatus.COMPLETED.value, StageStatus.SKIPPED.value}
_PHASE_ACTIVE_STAGE_NAMES = {
    WorkflowPhase.PREPARE.value: ("preparation",),
    WorkflowPhase.UNDERSTAND.value: ("understanding",),
    WorkflowPhase.TRANSLATE_CHAPTERS.value: ("translation", "glossary"),
    WorkflowPhase.TRANSLATE_TITLES.value: ("titles",),
    WorkflowPhase.REVIEW.value: ("review",),
    WorkflowPhase.QUALITY.value: ("quality",),
    WorkflowPhase.EXPORT.value: ("exports",),
}

# 每个 phase 明确列出先决、当前和未来阶段；只允许跳过 optional 阶段。
_PENDING_ONLY = frozenset({StageStatus.PENDING.value})
_ACTIVE = frozenset({StageStatus.PENDING.value, StageStatus.RUNNING.value})
_ACTIVE_OR_COMPLETED = frozenset(
    {StageStatus.PENDING.value, StageStatus.RUNNING.value, StageStatus.COMPLETED.value}
)
_RUNNING_ONLY = frozenset({StageStatus.RUNNING.value})
_RUNNING_OR_COMPLETED = frozenset({StageStatus.RUNNING.value, StageStatus.COMPLETED.value})
_COMPLETED_ONLY = frozenset({StageStatus.COMPLETED.value})
_TERMINAL = frozenset({StageStatus.COMPLETED.value, StageStatus.SKIPPED.value})
_PHASE_STAGE_REQUIREMENTS = {
    WorkflowPhase.PREPARE.value: {
        "preparation": _ACTIVE,
        "understanding": _PENDING_ONLY,
        "translation": _PENDING_ONLY,
        "glossary": _PENDING_ONLY,
        "titles": _PENDING_ONLY,
        "review": _PENDING_ONLY,
        "quality": _PENDING_ONLY,
        "exports": _PENDING_ONLY,
    },
    WorkflowPhase.UNDERSTAND.value: {
        "preparation": _COMPLETED_ONLY,
        "understanding": _ACTIVE,
        "translation": _PENDING_ONLY,
        "glossary": _PENDING_ONLY,
        "titles": _PENDING_ONLY,
        "review": _PENDING_ONLY,
        "quality": _PENDING_ONLY,
        "exports": _PENDING_ONLY,
    },
    WorkflowPhase.TRANSLATE_CHAPTERS.value: {
        "preparation": _COMPLETED_ONLY,
        "understanding": _TERMINAL,
        "translation": _RUNNING_OR_COMPLETED,
        "glossary": _ACTIVE_OR_COMPLETED,
        "titles": _PENDING_ONLY,
        "review": _PENDING_ONLY,
        "quality": _PENDING_ONLY,
        "exports": _PENDING_ONLY,
    },
    WorkflowPhase.TRANSLATE_TITLES.value: {
        "preparation": _COMPLETED_ONLY,
        "understanding": _TERMINAL,
        "translation": _COMPLETED_ONLY,
        "glossary": _COMPLETED_ONLY,
        "titles": _ACTIVE,
        "review": _PENDING_ONLY,
        "quality": _PENDING_ONLY,
        "exports": _PENDING_ONLY,
    },
    WorkflowPhase.REVIEW.value: {
        "preparation": _COMPLETED_ONLY,
        "understanding": _TERMINAL,
        "translation": _COMPLETED_ONLY,
        "glossary": _COMPLETED_ONLY,
        "titles": _COMPLETED_ONLY,
        "review": _RUNNING_ONLY,
        "quality": _PENDING_ONLY,
        "exports": _PENDING_ONLY,
    },
    WorkflowPhase.QUALITY.value: {
        "preparation": _COMPLETED_ONLY,
        "understanding": _TERMINAL,
        "translation": _COMPLETED_ONLY,
        "glossary": _COMPLETED_ONLY,
        "titles": _COMPLETED_ONLY,
        "review": _TERMINAL,
        "quality": _ACTIVE,
        "exports": _PENDING_ONLY,
    },
    WorkflowPhase.EXPORT.value: {
        "preparation": _COMPLETED_ONLY,
        "understanding": _TERMINAL,
        "translation": _COMPLETED_ONLY,
        "glossary": _COMPLETED_ONLY,
        "titles": _COMPLETED_ONLY,
        "review": _TERMINAL,
        "quality": _TERMINAL,
        "exports": _ACTIVE,
    },
}


def validate_workflow_state(state: Mapping[str, object]) -> None:
    """验证完整快照及跨切片不变量；成功时不修改输入。"""
    _validate_root_shape(state)

    # 第一层调用局部验证器，让后续交叉检查可以安全读取每个切片。
    request = require_mapping(state["request"], field="request")
    validate_request(request, workflow_id=state["workflow_id"])
    status = _require_workflow_status(state["status"])
    cursor = require_mapping(state["cursor"], field="cursor")
    book = require_mapping(state["book"], field="book")
    stages = {name: require_mapping(state[name], field=name) for name in WORKFLOW_STAGE_NAMES}
    validate_cursor(cursor)
    validate_book(book)
    validate_preparation(stages["preparation"])
    validate_understanding(stages["understanding"])
    validate_translation(stages["translation"])
    validate_glossary(stages["glossary"])
    validate_titles(stages["titles"])
    validate_review(stages["review"])
    validate_quality(stages["quality"])
    validate_exports(stages["exports"])
    accounting = require_mapping(state["accounting"], field="accounting")
    validate_accounting(accounting)
    operations = require_mapping(state["applied_operations"], field="applied_operations")
    claims = require_mapping(state["claimed_event_ids"], field="claimed_event_ids")
    validate_applied_operations(operations)
    validate_claimed_event_ids(claims, operations=operations)
    if state["revision"] != len(operations):
        raise ValueError("revision 必须等于 applied_operations 的唯一操作数量")

    # 第二层把顶层生命周期、阶段状态和失败摘要绑定为一个事实。
    failure = state["failure"]
    if failure is not None:
        if not isinstance(failure, Mapping):
            raise ValueError("failure 必须是 FailureInfo 或 None")
        validate_failure_info(failure)
    if status == WorkflowStatus.FAILED.value and failure is None:
        raise ValueError("failed 工作流必须包含 failure")
    if status != WorkflowStatus.FAILED.value and failure is not None:
        raise ValueError("只有 failed 工作流可以包含 failure")
    failed_stages = [
        name for name, stage in stages.items() if stage["status"] == StageStatus.FAILED.value
    ]
    if status == WorkflowStatus.FAILED.value and not failed_stages:
        raise ValueError("failed 工作流必须至少包含一个 failed 阶段")
    if status != WorkflowStatus.FAILED.value and failed_stages:
        raise ValueError(f"非 failed 工作流不能包含 failed 阶段：{failed_stages}")
    for name in REQUIRED_STAGE_NAMES:
        if stages[name]["status"] == StageStatus.SKIPPED.value:
            raise ValueError(f"必选阶段 {name} 不能标记为 skipped")
    _validate_execution_lifecycle(
        status=status,
        cursor=cursor,
        book=book,
        stages=stages,
        accounting=accounting,
    )

    # 第三层把章节计数、正式产物和恢复游标约束为同一份书籍进度。
    _validate_book_progress(
        cursor=cursor,
        book=book,
        translation=stages["translation"],
        review=stages["review"],
    )

    # 完成游标只能与完成态同时出现；完成态还需满足执行计划的终止规则。
    if cursor["phase"] == WorkflowPhase.COMPLETE.value and status != WorkflowStatus.COMPLETED.value:
        raise ValueError("只有 completed 工作流可以使用 complete 游标")
    if status == WorkflowStatus.COMPLETED.value:
        _validate_completed_workflow(cursor=cursor, book=book, stages=stages)

    # 最后拒绝 JSON 往返会变形或无法写入 UTF-8 的 Python 值。
    try:
        validate_json_value(state, field="WorkflowState")
    except (UnicodeEncodeError, ValueError) as error:
        raise ValueError("WorkflowState 必须只包含可写入 UTF-8 的稳定 JSON 值") from error


def _validate_root_shape(state: Mapping[str, object]) -> None:
    """固定根字段、schema 版本和 reducer 修订号的基本形状。"""
    actual_keys = set(state)
    if actual_keys != set(WORKFLOW_STATE_KEYS):
        missing = sorted(set(WORKFLOW_STATE_KEYS) - actual_keys)
        extra = sorted(actual_keys - set(WORKFLOW_STATE_KEYS))
        raise ValueError(f"WorkflowState 字段不匹配：missing={missing}, extra={extra}")
    if state["schema_version"] != WORKFLOW_SCHEMA_VERSION:
        raise ValueError(f"仅支持 workflow schema_version={WORKFLOW_SCHEMA_VERSION}")
    revision = state["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ValueError("revision 必须是非负整数")


def _require_workflow_status(value: object) -> str:
    """把动态顶层 status 收窄为 WorkflowStatus 字符串。"""
    allowed = {status.value for status in WorkflowStatus}
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"status 必须是：{', '.join(sorted(allowed))}")
    return value


def _validate_execution_lifecycle(
    *,
    status: str,
    cursor: Mapping[str, object],
    book: Mapping[str, object],
    stages: Mapping[str, Mapping[str, object]],
    accounting: Mapping[str, object],
) -> None:
    """绑定顶层状态、当前 phase 和阶段状态，拒绝彼此矛盾的快照。"""
    if status == WorkflowStatus.PENDING.value:
        non_pending = [
            name for name, stage in stages.items() if stage["status"] != StageStatus.PENDING.value
        ]
        if non_pending:
            raise ValueError(f"pending 工作流不能包含非 pending 阶段：{non_pending}")
        if cursor != {
            "phase": WorkflowPhase.PREPARE.value,
            "chapter_index": None,
            "segment_offset": None,
            "review_round": None,
        }:
            raise ValueError("pending 工作流必须使用初始 prepare 游标")
        if (
            book["document_artifact"] is not None
            or book["chapter_count"] != 0
            or book["source_segment_count"] != 0
        ):
            raise ValueError("pending 工作流不能包含已解析书籍结构")
        if any(accounting[name] != 0 for name in accounting):
            raise ValueError("pending 工作流的 accounting 必须全部为零")
        return
    if status == WorkflowStatus.COMPLETED.value:
        return

    phase = cursor["phase"]
    requirements = _PHASE_STAGE_REQUIREMENTS.get(phase)
    if requirements is None:
        raise ValueError(f"{status} 工作流不能停在 complete phase")
    if phase != WorkflowPhase.PREPARE.value and book["document_artifact"] is None:
        raise ValueError(f"cursor.phase={phase} 必须包含 book.document_artifact")

    # failed 快照仍遵守阶段先决条件；翻译期的两个并行阶段均可能是失败源。
    effective_requirements = dict(requirements)
    if status == WorkflowStatus.FAILED.value:
        active_stage_names = _PHASE_ACTIVE_STAGE_NAMES[phase]
        if len(active_stage_names) == 1:
            effective_requirements[active_stage_names[0]] = frozenset({StageStatus.FAILED.value})
        else:
            for name in active_stage_names:
                effective_requirements[name] = requirements[name] | frozenset(
                    {StageStatus.COMPLETED.value, StageStatus.FAILED.value}
                )

    mismatches = []
    for name, allowed in effective_requirements.items():
        actual = stages[name]["status"]
        if actual not in allowed:
            mismatches.append(f"{name}={actual}（允许 {sorted(allowed)}）")
    if mismatches:
        raise ValueError(f"cursor.phase={phase} 的阶段先决条件不满足：{'; '.join(mismatches)}")


def _validate_book_progress(
    *,
    cursor: Mapping[str, object],
    book: Mapping[str, object],
    translation: Mapping[str, object],
    review: Mapping[str, object],
) -> None:
    """交叉校验章节计数、翻译产物和恢复位置。"""
    chapter_count = book["chapter_count"]
    source_segment_count = book["source_segment_count"]
    assert isinstance(chapter_count, int)  # 局部校验已排除 bool 与负数。
    assert isinstance(source_segment_count, int)
    if (chapter_count > 0 or source_segment_count > 0) and book["document_artifact"] is None:
        raise ValueError("book 结构计数非零时必须包含 document_artifact")
    if chapter_count == 0 and source_segment_count != 0:
        raise ValueError("book.chapter_count 为零时 source_segment_count 也必须为零")

    completed = translation["completed_chapters"]
    artifacts = require_mapping(
        translation["chapter_artifacts"],
        field="translation.chapter_artifacts",
    )
    assert isinstance(completed, list)  # 局部校验已收窄元素类型与顺序。
    if any(chapter >= chapter_count for chapter in completed):
        raise ValueError("translation.completed_chapters 不能超出 book.chapter_count")
    if {int(key) for key in artifacts} != set(completed):
        raise ValueError("translation 完成章节必须与 chapter_artifacts 严格对应")
    if translation["status"] == StageStatus.COMPLETED.value and completed != list(
        range(chapter_count)
    ):
        raise ValueError("completed translation 必须包含 book 的全部章节产物")
    if cursor["phase"] == WorkflowPhase.TRANSLATE_TITLES.value and completed != list(
        range(chapter_count)
    ):
        raise ValueError("translate_titles 前必须完成 book 的全部章节翻译")

    chapter_index = cursor["chapter_index"]
    if chapter_index is not None and chapter_index >= chapter_count:
        raise ValueError("cursor.chapter_index 不能超出 book.chapter_count")
    if (
        cursor["phase"] == WorkflowPhase.TRANSLATE_CHAPTERS.value
        and chapter_count > 0
        and chapter_index is None
    ):
        raise ValueError("非空书籍的翻译游标必须包含 cursor.chapter_index")
    if cursor["review_round"] is not None and cursor["review_round"] != review["round"]:
        raise ValueError("cursor.review_round 必须与 review.round 一致")


def _validate_completed_workflow(
    *,
    cursor: Mapping[str, object],
    book: Mapping[str, object],
    stages: Mapping[str, Mapping[str, object]],
) -> None:
    """要求关闭的执行实例已经终止所有必选、可选和导出阶段。"""
    if cursor["phase"] != WorkflowPhase.COMPLETE.value:
        raise ValueError("completed 工作流的 cursor.phase 必须是 complete")
    if book["document_artifact"] is None:
        raise ValueError("completed 工作流必须包含 book.document_artifact")
    for name in REQUIRED_STAGE_NAMES:
        if stages[name]["status"] != StageStatus.COMPLETED.value:
            raise ValueError(f"completed 工作流的必选阶段 {name} 必须是 completed")
    for name in OPTIONAL_STAGE_NAMES:
        if stages[name]["status"] not in _TERMINAL_STAGE_STATUSES:
            raise ValueError(f"completed 工作流的可选阶段 {name} 必须已终止")
    if stages["exports"]["status"] not in _TERMINAL_STAGE_STATUSES:
        raise ValueError("completed 工作流的 exports.status 必须已终止")


__all__ = ["validate_workflow_state"]
