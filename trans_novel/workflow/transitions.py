"""相邻工作流状态之间的生命周期和单调性约束。

``validation`` 判断单个快照是否自洽；本模块只比较两个已经通过快照校验的
状态，防止普通节点撤销已提交进度。需要重置终态阶段时，应走未来单独审计的
管理操作，而不是放宽这里的普通转换规则。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from ..domain.translation_batch import parse_translation_batch_key
from ..domain.workflow import ArtifactRef, StageStatus, WorkflowPhase, WorkflowStatus
from .patches import InvalidStatePatch
from .state import WORKFLOW_STAGE_NAMES, GlossaryState, TitleTranslationState, WorkflowState

# 顶层状态允许暂停和失败恢复，但完成态没有普通出边。
_WORKFLOW_TRANSITIONS = {
    WorkflowStatus.PENDING.value: frozenset(
        {
            WorkflowStatus.PENDING.value,
            WorkflowStatus.RUNNING.value,
            WorkflowStatus.FAILED.value,
        }
    ),
    WorkflowStatus.RUNNING.value: frozenset(
        {
            WorkflowStatus.RUNNING.value,
            WorkflowStatus.PAUSED.value,
            WorkflowStatus.COMPLETED.value,
            WorkflowStatus.FAILED.value,
        }
    ),
    WorkflowStatus.PAUSED.value: frozenset(
        {
            WorkflowStatus.RUNNING.value,
            WorkflowStatus.FAILED.value,
        }
    ),
    WorkflowStatus.FAILED.value: frozenset({WorkflowStatus.RUNNING.value}),
    WorkflowStatus.COMPLETED.value: frozenset({WorkflowStatus.COMPLETED.value}),
}

# 阶段必须先 running 再 completed；skipped 与 completed 都是普通流程终态。
_STAGE_TRANSITIONS = {
    StageStatus.PENDING.value: frozenset(
        {
            StageStatus.PENDING.value,
            StageStatus.RUNNING.value,
            StageStatus.SKIPPED.value,
            StageStatus.FAILED.value,
        }
    ),
    StageStatus.RUNNING.value: frozenset(
        {
            StageStatus.RUNNING.value,
            StageStatus.COMPLETED.value,
            StageStatus.FAILED.value,
        }
    ),
    StageStatus.FAILED.value: frozenset({StageStatus.FAILED.value, StageStatus.RUNNING.value}),
    StageStatus.COMPLETED.value: frozenset({StageStatus.COMPLETED.value}),
    StageStatus.SKIPPED.value: frozenset({StageStatus.SKIPPED.value}),
}

# phase 采用线性 DAG；可选阶段通过向前跳跃省略，不允许普通补丁回到旧阶段。
_PHASE_ORDER = {phase.value: index for index, phase in enumerate(WorkflowPhase)}


def _validate_state_transition(current: WorkflowState, candidate: WorkflowState) -> None:
    """验证一次普通补丁既沿生命周期前进，也不删除累计领域事实。"""
    _validate_lifecycle(current, candidate)
    _validate_control_boundaries(current, candidate)
    _validate_cursor_progress(current, candidate)
    _validate_monotonic_progress(current, candidate)


def _validate_lifecycle(current: WorkflowState, candidate: WorkflowState) -> None:
    """拒绝顶层或任一阶段跳过必要步骤、倒退或重开终态。"""
    current_status = current["status"]
    next_status = candidate["status"]
    if next_status not in _WORKFLOW_TRANSITIONS[current_status]:
        raise InvalidStatePatch(f"不允许工作流状态从 {current_status!r} 转换为 {next_status!r}")

    # 未被补丁触及的阶段走同状态自环，因此可以统一比较全部阶段。
    for name in WORKFLOW_STAGE_NAMES:
        current_stage = cast(Mapping[str, object], current[name])
        next_stage = cast(Mapping[str, object], candidate[name])
        current_stage_status = cast(str, current_stage["status"])
        next_stage_status = cast(str, next_stage["status"])
        if next_stage_status not in _STAGE_TRANSITIONS[current_stage_status]:
            raise InvalidStatePatch(
                f"不允许 {name}.status 从 {current_stage_status!r} 转换为 {next_stage_status!r}"
            )

        # 终态阶段的完整切片均不可重写；后续 review/export 使用独立子运行。
        if (
            current_stage_status in {StageStatus.COMPLETED.value, StageStatus.SKIPPED.value}
            and next_stage != current_stage
        ):
            raise InvalidStatePatch(f"终态阶段 {name} 的内容不能由普通补丁修改")


def _validate_cursor_progress(current: WorkflowState, candidate: WorkflowState) -> None:
    """按 phase DAG 和章内位置阻止恢复游标回退。"""
    current_cursor = current["cursor"]
    next_cursor = candidate["cursor"]
    current_phase = current_cursor["phase"]
    next_phase = next_cursor["phase"]
    if _PHASE_ORDER[next_phase] < _PHASE_ORDER[current_phase]:
        raise InvalidStatePatch(f"cursor.phase 不能从 {current_phase!r} 回退到 {next_phase!r}")
    if next_phase != current_phase:
        return

    # 只有正文翻译使用章节位置；标题阶段按稳定 title ID 账本恢复。
    if current_phase != WorkflowPhase.TRANSLATE_CHAPTERS.value:
        return
    current_chapter = current_cursor["chapter_index"]
    next_chapter = next_cursor["chapter_index"]
    if current_chapter is None or next_chapter is None:
        # 完整快照校验保证 None 只会出现在章节数为零的翻译状态。
        if current_chapter != next_chapter:  # pragma: no cover - 防御未来 schema 变更。
            raise InvalidStatePatch("cursor.chapter_index 不能在空书翻译中改变")
        return
    if next_chapter < current_chapter:
        raise InvalidStatePatch("cursor.chapter_index 不能倒退")
    if next_chapter > current_chapter:
        return

    current_offset = current_cursor["segment_offset"]
    next_offset = next_cursor["segment_offset"]
    old_position = -1 if current_offset is None else current_offset
    new_position = -1 if next_offset is None else next_offset
    if new_position < old_position:
        raise InvalidStatePatch("同一章节的 cursor.segment_offset 不能倒退")


def _validate_monotonic_progress(
    current: WorkflowState,
    candidate: WorkflowState,
) -> None:
    """保护正式书籍进度、内容寻址产物和累计账本不被普通补丁撤销。"""
    _validate_book_and_translation_progress(current, candidate)
    _validate_immutable_artifact_progress(current, candidate)
    _validate_glossary_progress(current, candidate)
    _validate_title_progress(current["titles"], candidate["titles"])
    _validate_review_progress(current, candidate)
    _validate_accounting_and_export_progress(current, candidate)


def _validate_book_and_translation_progress(
    current: WorkflowState,
    candidate: WorkflowState,
) -> None:
    """冻结已解析的书籍结构，并只允许追加已完成章节及其产物。"""
    if current["book"]["document_artifact"] is not None:
        for field in ("document_artifact", "chapter_count", "source_segment_count"):
            if candidate["book"][field] != current["book"][field]:
                raise InvalidStatePatch(f"book.{field} 一旦确定就不能修改")

    # 已完成章节和其内容寻址引用只能追加，不能删除或改指向。
    old_completed = set(current["translation"]["completed_chapters"])
    new_completed = set(candidate["translation"]["completed_chapters"])
    if not old_completed <= new_completed:
        raise InvalidStatePatch("translation.completed_chapters 只能追加")
    _require_preserved_mapping(
        current["translation"]["batch_artifacts"],
        candidate["translation"]["batch_artifacts"],
        field="translation.batch_artifacts",
    )
    _require_preserved_mapping(
        current["translation"]["chapter_artifacts"],
        candidate["translation"]["chapter_artifacts"],
        field="translation.chapter_artifacts",
    )

    old_batches = current["translation"]["batch_artifacts"]
    new_batches = candidate["translation"]["batch_artifacts"]
    added_batches = set(new_batches) - set(old_batches)
    newly_completed = new_completed - old_completed
    chapter_finalized = bool(newly_completed)

    # A normal translation node commits exactly one completed range and moves
    # the cursor to its stop.  Chapter publication is a separate atomic patch,
    # so a crash can never leave ambiguous batch-vs-chapter ownership.
    if added_batches:
        if len(added_batches) != 1:
            raise InvalidStatePatch("普通翻译批次补丁每次必须恰好追加一个 batch_artifact")
        if chapter_finalized:
            raise InvalidStatePatch("章节 finalize 不能与新增 batch_artifact 位于同一补丁")
        if current["cursor"]["phase"] != WorkflowPhase.TRANSLATE_CHAPTERS.value:
            raise InvalidStatePatch("只有正文翻译阶段可以追加 batch_artifact")
        if candidate["cursor"]["phase"] != WorkflowPhase.TRANSLATE_CHAPTERS.value:
            raise InvalidStatePatch("批次补丁必须保留正文翻译 phase")
        if (
            current["status"] != WorkflowStatus.RUNNING.value
            or candidate["status"] != WorkflowStatus.RUNNING.value
            or current["translation"]["status"] != StageStatus.RUNNING.value
            or candidate["translation"]["status"] != StageStatus.RUNNING.value
        ):
            raise InvalidStatePatch("批次补丁只能在 running 翻译阶段独立提交")
        key = next(iter(added_batches))
        chapter, start, stop = parse_translation_batch_key(key)
        if chapter in old_completed:
            raise InvalidStatePatch("已 finalize 章节不能再追加 batch_artifact")
        old_cursor = current["cursor"]
        new_cursor = candidate["cursor"]
        if (
            old_cursor["chapter_index"] != chapter
            or old_cursor["segment_offset"] != start
            or new_cursor["chapter_index"] != chapter
            or new_cursor["segment_offset"] != stop
        ):
            raise InvalidStatePatch("批次补丁必须从当前游标范围起点推进到新增范围终点")

    # Chapter publication is also one atomic recovery unit.  Accepting two
    # chapters in one patch would let a faulty node jump over the per-chapter
    # finalize boundary even though the resulting snapshot looks like a valid
    # prefix.  The current cursor identifies the only chapter this patch owns.
    if chapter_finalized:
        current_chapter = current["cursor"]["chapter_index"]
        if (
            current["cursor"]["phase"] != WorkflowPhase.TRANSLATE_CHAPTERS.value
            or current_chapter is None
            or newly_completed != {current_chapter}
        ):
            raise InvalidStatePatch("章节 finalize 每次只能提交当前游标指向的一章")
        added_chapter_artifacts = set(candidate["translation"]["chapter_artifacts"]) - set(
            current["translation"]["chapter_artifacts"]
        )
        if added_chapter_artifacts != {str(current_chapter)}:
            raise InvalidStatePatch("章节 finalize 必须同时提交当前章唯一的 chapter_artifact")


def _validate_immutable_artifact_progress(
    current: WorkflowState,
    candidate: WorkflowState,
) -> None:
    """保护没有独立修订号的产物指针，使其在首次提交后不可改写。"""
    _require_preserved_optional(
        current["preparation"]["normalized_source"],
        candidate["preparation"]["normalized_source"],
        field="preparation.normalized_source",
    )
    for field in ("analysis", "book_synopsis", "chapter_synopses"):
        _require_preserved_optional(
            current["understanding"][field],
            candidate["understanding"][field],
            field=f"understanding.{field}",
        )
    _require_preserved_optional(
        current["quality"]["report"],
        candidate["quality"]["report"],
        field="quality.report",
    )


def _validate_glossary_progress(
    current: WorkflowState,
    candidate: WorkflowState,
) -> None:
    """要求术语快照修订号单调，且同修订不能改指向。"""
    _validate_snapshot_revision(
        current=current["glossary"],
        candidate=candidate["glossary"],
        field="glossary",
    )


def _validate_review_progress(
    current: WorkflowState,
    candidate: WorkflowState,
) -> None:
    """绑定每轮审校的输入和正式结果，同时保留已提交块产物。"""
    old_review_round = current["review"]["round"]
    new_review_round = candidate["review"]["round"]
    if new_review_round < old_review_round:
        raise InvalidStatePatch("review.round 不能倒退")
    if new_review_round > old_review_round + 1:
        raise InvalidStatePatch("review.round 每次只能增加一轮")
    old_review_digest = current["review"]["reviewed_content_digest"]
    new_review_digest = candidate["review"]["reviewed_content_digest"]
    if (
        new_review_round == old_review_round
        and old_review_digest is not None
        and new_review_digest != old_review_digest
    ):
        raise InvalidStatePatch("review.reviewed_content_digest 一旦确定就不能修改")
    if new_review_round > old_review_round:
        if new_review_digest is None:
            raise InvalidStatePatch("review.round 增长时必须绑定 reviewed_content_digest")
        if old_review_round > 0 and current["review"]["latest_result"] is None:
            raise InvalidStatePatch("开始新 review.round 前必须提交上一轮 latest_result")
        if (
            candidate["review"]["latest_result"] is not None
            or candidate["review"]["latest_result_round"] is not None
        ):
            raise InvalidStatePatch("开始新 review.round 时必须清空上一轮 latest_result")
    _require_preserved_mapping(
        current["review"]["chunk_results"],
        candidate["review"]["chunk_results"],
        field="review.chunk_results",
    )
    if (
        new_review_round == old_review_round
        and current["review"]["latest_result"] is not None
        and candidate["review"]["latest_result"] != current["review"]["latest_result"]
    ):
        raise InvalidStatePatch("同一 review.round 不能改写 latest_result")


def _validate_accounting_and_export_progress(
    current: WorkflowState,
    candidate: WorkflowState,
) -> None:
    """阻止 token 累计值倒退，并保护已请求格式和已发布导出产物。"""
    for field in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if candidate["accounting"][field] < current["accounting"][field]:
            raise InvalidStatePatch(f"accounting.{field} 不能倒退")

    # 当前执行实例中的导出意图和已发布输出均可追加，但不能抹除。
    old_formats = set(current["exports"]["requested_formats"])
    new_formats = set(candidate["exports"]["requested_formats"])
    if not old_formats <= new_formats:
        raise InvalidStatePatch("exports.requested_formats 只能追加")
    _require_preserved_mapping(
        current["exports"]["outputs"],
        candidate["exports"]["outputs"],
        field="exports.outputs",
    )


def _require_preserved_mapping(
    current: Mapping[str, object],
    candidate: Mapping[str, object],
    *,
    field: str,
) -> None:
    """要求候选映射保留所有已提交键值，同时允许追加新键。"""
    for key, value in current.items():
        if key not in candidate or candidate[key] != value:
            raise InvalidStatePatch(f"{field}.{key} 已提交后不能删除或修改")


def _require_preserved_optional(current: object, candidate: object, *, field: str) -> None:
    """允许一次性从 None 设置不可变值，设置后禁止删除或改指向。"""
    if current is not None and candidate != current:
        raise InvalidStatePatch(f"{field} 已提交后不能删除或修改")


def _validate_title_progress(
    current: TitleTranslationState,
    candidate: TitleTranslationState,
) -> None:
    """将标题完成账本与它的修订号、快照绑定为一个原子进度单元。"""
    # 输入摘要和预期 ID 共同定义标题批次身份，首次绑定后不可替换。
    if current["input_digest"] is not None:
        if candidate["input_digest"] != current["input_digest"]:
            raise InvalidStatePatch("titles.input_digest 一旦确定就不能修改")
        if candidate["expected_title_ids"] != current["expected_title_ids"]:
            raise InvalidStatePatch("titles.expected_title_ids 一旦确定就不能修改")

    old_completed = set(current["completed_title_ids"])
    new_completed = set(candidate["completed_title_ids"])
    if not old_completed <= new_completed:
        raise InvalidStatePatch("titles.completed_title_ids 只能追加")

    progress_changed = new_completed != old_completed
    revision_changed = _validate_snapshot_revision(
        current=current,
        candidate=candidate,
        field="titles",
    )
    if progress_changed and not revision_changed:
        raise InvalidStatePatch("标题进度变化必须提交新的 titles.revision 和 snapshot")


def _validate_snapshot_revision(
    *,
    current: GlossaryState | TitleTranslationState,
    candidate: GlossaryState | TitleTranslationState,
    field: str,
) -> bool:
    """验证快照修订连续性，并确保新修订真的指向不同不可变内容。"""
    old_revision = current["revision"]
    new_revision = candidate["revision"]
    old_snapshot = current["snapshot"]
    new_snapshot = candidate["snapshot"]
    if new_revision == old_revision:
        if new_snapshot != old_snapshot:
            raise InvalidStatePatch(f"同一 {field}.revision 不能修改 snapshot")
        return False
    if new_revision != old_revision + 1:
        raise InvalidStatePatch(f"{field}.revision 每次只能增加 1")
    if new_snapshot is None:
        raise InvalidStatePatch(f"{field}.revision 增长时必须提交新 snapshot")
    if old_snapshot is not None and not _is_distinct_artifact(old_snapshot, new_snapshot):
        raise InvalidStatePatch(f"{field}.revision 增长时必须提交全新内容寻址 snapshot")
    return True


def _is_distinct_artifact(current: ArtifactRef, candidate: ArtifactRef) -> bool:
    """新快照必须同时更换不可变 URI 和内容摘要，不只是改写元数据。"""
    return current["uri"] != candidate["uri"] and current["sha256"] != candidate["sha256"]


def _validate_control_boundaries(current: WorkflowState, candidate: WorkflowState) -> None:
    """冻结暂停/失败快照；恢复操作只改变控制状态，不夹带新的业务进度。"""
    current_status = current["status"]
    next_status = candidate["status"]
    if next_status == WorkflowStatus.FAILED.value and current_status in {
        WorkflowStatus.PENDING.value,
        WorkflowStatus.RUNNING.value,
        WorkflowStatus.PAUSED.value,
    }:
        # 失败提交只记录 failure 摘要和失败阶段的 status。游标、书籍、用量及
        # 各阶段 payload 必须停留在最后一次成功提交，恢复时才有唯一可信起点。
        _require_status_only_stage_changes(current, candidate)
        return
    if (
        current_status == WorkflowStatus.RUNNING.value
        and next_status == WorkflowStatus.PAUSED.value
    ):
        _require_same_progress(current, candidate)
        return
    if current_status == WorkflowStatus.PAUSED.value:
        if next_status == WorkflowStatus.RUNNING.value:
            _require_same_progress(current, candidate)
            return
    if current_status != WorkflowStatus.FAILED.value:
        return
    if next_status == WorkflowStatus.RUNNING.value:
        failure = current["failure"]
        assert failure is not None  # 完整快照校验已保证 failed 状态携带摘要。
        if not failure["retryable"]:
            raise InvalidStatePatch("不可重试 failure 不能通过普通补丁恢复")
        _require_status_only_stage_changes(current, candidate, resume_failed=True)


def _require_same_progress(current: WorkflowState, candidate: WorkflowState) -> None:
    """要求控制操作不修改游标、业务阶段、书籍结构或累计用量。"""
    for field in ("cursor", "book", "accounting", *WORKFLOW_STAGE_NAMES):
        if candidate[field] != current[field]:
            raise InvalidStatePatch(f"控制状态转换不能同时修改 {field} 业务进度")


def _require_status_only_stage_changes(
    current: WorkflowState,
    candidate: WorkflowState,
    *,
    resume_failed: bool = False,
) -> None:
    """允许活动阶段只改变 status，同时冻结其余字段和非阶段进度。"""
    for field in ("cursor", "book", "accounting"):
        if candidate[field] != current[field]:
            raise InvalidStatePatch(f"控制状态转换不能同时修改 {field} 业务进度")
    for name in WORKFLOW_STAGE_NAMES:
        old_stage = current[name]
        new_stage = candidate[name]
        old_payload = {key: value for key, value in old_stage.items() if key != "status"}
        new_payload = {key: value for key, value in new_stage.items() if key != "status"}
        if old_payload != new_payload:
            raise InvalidStatePatch(f"控制状态转换不能同时修改 {name} 业务内容")
        if resume_failed:
            expected = (
                StageStatus.RUNNING.value
                if old_stage["status"] == StageStatus.FAILED.value
                else old_stage["status"]
            )
            if new_stage["status"] != expected:
                raise InvalidStatePatch(f"failed 恢复只能重启失败阶段 {name}")
        elif (
            old_stage["status"] != new_stage["status"]
            and new_stage["status"] != StageStatus.FAILED.value
        ):
            raise InvalidStatePatch(f"工作流转失败时 {name} 只能保持原状态或标记 failed")
