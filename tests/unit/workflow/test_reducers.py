"""工作流补丁的原子性、幂等性和并行合并测试。"""

from __future__ import annotations

import copy

import pytest

from trans_novel.domain.translation_batch import TRANSLATION_BATCH_ARTIFACT_MEDIA_TYPE
from trans_novel.domain.workflow import StageStatus, WorkflowPhase, WorkflowStatus
from trans_novel.workflow import (
    InvalidStatePatch,
    MergeConflict,
    OperationConflict,
    RevisionConflict,
    StatePatch,
    apply_state_patch,
    merge_unique_mapping,
    new_workflow_state,
)

SOURCE_HASH = "a" * 64
PROFILE_HASH = "b" * 64


def _artifact(
    *,
    uri: str = "artifact://source/book.epub",
    sha256: str = SOURCE_HASH,
    media_type: str = "application/epub+zip",
) -> dict[str, object]:
    """构造测试用内容寻址引用。"""
    return {
        "uri": uri,
        "sha256": sha256,
        "media_type": media_type,
        "size_bytes": 123,
    }


def _title_ids(chapter_count: int) -> list[str]:
    """用稳定 ID 表示当前测试书籍需要翻译的章节标题。"""
    return [f"chapter-{index}" for index in range(chapter_count)]


def _state():
    """创建每个 reducer 场景独享的合法初始状态。"""
    return new_workflow_state(
        source_artifact=_artifact(),
        source_format="epub",
        source_lang="ja",
        target_lang="zh",
        semantic_profile_hash=PROFILE_HASH,
    )


def _running_patch(
    *,
    operation_id: str = "prepare:start",
    expected_revision: int = 0,
) -> StatePatch:
    """构造进入准备阶段的确定性补丁。"""
    return StatePatch(
        operation_id=operation_id,
        expected_revision=expected_revision,
        updates={
            "status": WorkflowStatus.RUNNING.value,
            "preparation": {
                "status": StageStatus.RUNNING.value,
                "normalized_source": None,
            },
        },
        events=(
            {
                "event_id": "prepare-started",
                "event_type": "preparation.started",
                "payload": {"phase": "prepare"},
            },
        ),
    )


def _completed_updates(state) -> dict[str, object]:
    """把所有阶段明确终止，构造自洽的完成态更新。"""
    chapter_count = state["book"]["chapter_count"]
    return {
        "status": WorkflowStatus.COMPLETED.value,
        "cursor": {
            "phase": WorkflowPhase.COMPLETE.value,
            "chapter_index": None,
            "segment_offset": None,
            "review_round": None,
        },
        "translation": {
            "status": StageStatus.COMPLETED.value,
            "batch_artifacts": copy.deepcopy(state["translation"]["batch_artifacts"]),
            "completed_chapters": list(range(chapter_count)),
            "chapter_artifacts": {
                str(index): _artifact(uri=f"artifact://chapters/{index}.json")
                for index in range(chapter_count)
            },
        },
        "glossary": copy.deepcopy(state["glossary"]),
        "titles": {
            "status": StageStatus.COMPLETED.value,
            "input_digest": state["titles"]["input_digest"],
            "expected_title_ids": state["titles"]["expected_title_ids"],
            "completed_title_ids": state["titles"]["expected_title_ids"],
            "revision": state["titles"]["revision"] + 1,
            "snapshot": _artifact(uri="artifact://titles/revision-1.json"),
        },
        "review": {**state["review"], "status": StageStatus.SKIPPED.value},
        "quality": {**state["quality"], "status": StageStatus.SKIPPED.value},
        "exports": {**state["exports"], "status": StageStatus.SKIPPED.value},
    }


def _translation_running_state(*, chapter_count: int = 1):
    """按真实 phase 顺序推进到正文翻译与术语提取并行阶段。"""
    preparation_started = apply_state_patch(_state(), _running_patch()).state
    preparation_completed = apply_state_patch(
        preparation_started,
        StatePatch(
            operation_id="prepare:complete",
            expected_revision=1,
            updates={
                "cursor": {
                    "phase": WorkflowPhase.UNDERSTAND.value,
                    "chapter_index": None,
                    "segment_offset": None,
                    "review_round": None,
                },
                "book": {
                    "document_artifact": _artifact(uri="artifact://documents/book.json"),
                    "chapter_count": chapter_count,
                    "source_segment_count": chapter_count * 10,
                },
                "preparation": {
                    "status": StageStatus.COMPLETED.value,
                    "normalized_source": _artifact(uri="artifact://normalized/book.epub"),
                },
            },
        ),
    ).state
    translation_started = apply_state_patch(
        preparation_completed,
        StatePatch(
            operation_id="translation:start-chapters",
            expected_revision=2,
            updates={
                "cursor": {
                    "phase": WorkflowPhase.TRANSLATE_CHAPTERS.value,
                    "chapter_index": 0 if chapter_count else None,
                    "segment_offset": 0 if chapter_count else None,
                    "review_round": None,
                },
                "understanding": {
                    **preparation_completed["understanding"],
                    "status": StageStatus.SKIPPED.value,
                },
                "translation": {
                    **preparation_completed["translation"],
                    "status": StageStatus.RUNNING.value,
                },
            },
        ),
    ).state
    return apply_state_patch(
        translation_started,
        StatePatch(
            operation_id="glossary:start",
            expected_revision=3,
            updates={
                "glossary": {
                    **translation_started["glossary"],
                    "status": StageStatus.RUNNING.value,
                },
            },
        ),
    ).state


def _title_running_state(*, chapter_count: int = 1):
    """完成正文与术语快照，并进入具有显式输入账本的标题阶段。"""
    state = _translation_running_state(chapter_count=chapter_count)

    # A chapter artifact is an independent crash-recovery boundary.  Finalize
    # every chapter except the last before the phase-change patch, so fixtures
    # exercise the same one-chapter transition required from real graph nodes.
    for chapter_index in range(max(0, chapter_count - 1)):
        completed = list(range(chapter_index + 1))
        state = apply_state_patch(
            state,
            StatePatch(
                operation_id=f"translation:finalize-chapter-{chapter_index}",
                expected_revision=state["revision"],
                updates={
                    "cursor": {
                        **state["cursor"],
                        "chapter_index": chapter_index + 1,
                        "segment_offset": 0,
                    },
                    "translation": {
                        **state["translation"],
                        "completed_chapters": completed,
                        "chapter_artifacts": {
                            str(index): _artifact(uri=f"artifact://chapters/{index}.json")
                            for index in completed
                        },
                    },
                },
            ),
        ).state
    return apply_state_patch(
        state,
        StatePatch(
            operation_id="titles:start",
            expected_revision=state["revision"],
            updates={
                "cursor": {
                    "phase": WorkflowPhase.TRANSLATE_TITLES.value,
                    "chapter_index": None,
                    "segment_offset": None,
                    "review_round": None,
                },
                "translation": {
                    "status": StageStatus.COMPLETED.value,
                    "batch_artifacts": copy.deepcopy(state["translation"]["batch_artifacts"]),
                    "completed_chapters": list(range(chapter_count)),
                    "chapter_artifacts": {
                        str(index): _artifact(uri=f"artifact://chapters/{index}.json")
                        for index in range(chapter_count)
                    },
                },
                "glossary": {
                    "status": StageStatus.COMPLETED.value,
                    "revision": 1,
                    "snapshot": _artifact(uri="artifact://glossary/revision-1.json"),
                },
                "titles": {
                    "status": StageStatus.RUNNING.value,
                    "input_digest": "d" * 64,
                    "expected_title_ids": _title_ids(chapter_count),
                    "completed_title_ids": [],
                    "revision": 0,
                    "snapshot": None,
                },
            },
        ),
    ).state


def _review_running_state(*, chapter_count: int = 1):
    """完成标题快照，并原子进入第一轮 review。"""
    state = _title_running_state(chapter_count=chapter_count)
    return apply_state_patch(
        state,
        StatePatch(
            operation_id="review:start-round-1",
            expected_revision=state["revision"],
            updates={
                "cursor": {
                    "phase": WorkflowPhase.REVIEW.value,
                    "chapter_index": None,
                    "segment_offset": None,
                    "review_round": 1,
                },
                "titles": {
                    "status": StageStatus.COMPLETED.value,
                    "input_digest": "d" * 64,
                    "expected_title_ids": _title_ids(chapter_count),
                    "completed_title_ids": _title_ids(chapter_count),
                    "revision": 1,
                    "snapshot": _artifact(uri="artifact://titles/revision-1.json"),
                },
                "review": {
                    "status": StageStatus.RUNNING.value,
                    "round": 1,
                    "reviewed_content_digest": "c" * 64,
                    "latest_result": None,
                    "latest_result_round": None,
                    "chunk_results": {},
                },
            },
        ),
    ).state


def test_valid_patch_is_atomic_and_advances_revision_once() -> None:
    state = _state()
    before = copy.deepcopy(state)

    application = apply_state_patch(state, _running_patch())

    assert state == before
    assert application.state["revision"] == 1
    assert application.state["status"] == WorkflowStatus.RUNNING.value
    assert application.state["preparation"]["status"] == StageStatus.RUNNING.value
    assert application.events[0]["event_id"] == "prepare-started"
    assert application.created_artifacts == ()
    assert not application.duplicate


def test_application_does_not_share_patch_or_effect_payloads() -> None:
    state = _translation_running_state()
    translation = copy.deepcopy(state["translation"])
    event = {
        "event_id": "translation-started",
        "event_type": "translation.started",
        "payload": {"chapter": 0},
    }
    patch = StatePatch(
        operation_id="translation:emit-progress",
        expected_revision=4,
        updates={"translation": translation},
        events=(event,),
    )

    application = apply_state_patch(state, patch)
    translation["completed_chapters"].append(99)
    event["payload"]["chapter"] = 99

    assert application.state["translation"]["completed_chapters"] == []
    assert application.events[0]["payload"]["chapter"] == 0


def test_stale_revision_rejects_the_entire_patch() -> None:
    state = _state()
    before = copy.deepcopy(state)

    with pytest.raises(RevisionConflict):
        apply_state_patch(state, _running_patch(expected_revision=1))

    assert state == before


@pytest.mark.parametrize(
    "key",
    ["revision", "workflow_id", "request", "applied_operations", "claimed_event_ids"],
)
def test_patch_cannot_write_reducer_owned_fields(key: str) -> None:
    state = _state()
    patch = StatePatch(
        operation_id=f"invalid:{key}",
        expected_revision=0,
        updates={key: state[key]},
    )

    with pytest.raises(InvalidStatePatch, match="保留字段"):
        apply_state_patch(state, patch)


def test_patch_rejects_unknown_top_level_fields() -> None:
    patch = StatePatch(
        operation_id="invalid:unknown",
        expected_revision=0,
        updates={"runtime_client": "must-not-enter-state"},
    )

    with pytest.raises(InvalidStatePatch, match="未知顶层字段"):
        apply_state_patch(_state(), patch)


@pytest.mark.parametrize("operation_id", ["", " has-space", "bad operation", "x" * 201])
def test_operation_id_must_be_stable_and_path_safe(operation_id: str) -> None:
    patch = StatePatch(
        operation_id=operation_id,
        expected_revision=0,
        updates={"status": WorkflowStatus.RUNNING.value},
    )

    with pytest.raises(InvalidStatePatch, match="operation_id"):
        apply_state_patch(_state(), patch)


def test_full_slice_replacement_exposes_missing_fields() -> None:
    patch = StatePatch(
        operation_id="translation:invalid-partial",
        expected_revision=0,
        updates={"translation": {"status": StageStatus.RUNNING.value}},
    )

    with pytest.raises(InvalidStatePatch, match="translation 字段不匹配"):
        apply_state_patch(_state(), patch)


def test_same_operation_replays_effects_without_advancing_state() -> None:
    patch = _running_patch()
    first = apply_state_patch(_state(), patch)

    replay = apply_state_patch(first.state, patch)

    assert replay.duplicate
    assert replay.state == first.state
    assert replay.events == first.events
    assert replay.created_artifacts == first.created_artifacts
    assert replay.state["revision"] == 1


def test_same_patch_replay_supports_idempotent_effect_delivery() -> None:
    patch = _running_patch()

    # 状态已提交时，同一完整补丁重放会重新给出事件用于幂等补投。
    committed = apply_state_patch(_state(), patch)
    after_state_crash = apply_state_patch(committed.state, patch)
    event_sink: dict[str, object] = {}
    for event in after_state_crash.events:
        event_sink.setdefault(event["event_id"], event)

    # 事件已存在时，append-if-absent 也能吸收状态提交前的调用方重试。
    original_state = _state()
    before_state_crash = apply_state_patch(original_state, patch)
    for event in before_state_crash.events:
        event_sink.setdefault(event["event_id"], event)
    retried = apply_state_patch(original_state, patch)
    for event in retried.events:
        event_sink.setdefault(event["event_id"], event)

    assert list(event_sink) == ["prepare-started"]
    assert after_state_crash.duplicate
    assert not retried.duplicate


def test_event_ids_are_unique_within_and_across_operations() -> None:
    event = {
        "event_id": "workflow-event-1",
        "event_type": "workflow.changed",
        "payload": {},
    }
    duplicate_in_patch = StatePatch(
        operation_id="events:duplicate-in-patch",
        expected_revision=0,
        updates={"status": WorkflowStatus.RUNNING.value},
        events=(event, event),
    )
    with pytest.raises(InvalidStatePatch, match="event_id"):
        apply_state_patch(_state(), duplicate_in_patch)

    first = apply_state_patch(
        _state(),
        StatePatch(
            operation_id="events:first-owner",
            expected_revision=0,
            updates={
                "status": WorkflowStatus.RUNNING.value,
                "preparation": {
                    "status": StageStatus.RUNNING.value,
                    "normalized_source": None,
                },
            },
            events=(event,),
        ),
    )
    with pytest.raises(OperationConflict, match="event_id"):
        apply_state_patch(
            first.state,
            StatePatch(
                operation_id="events:second-owner",
                expected_revision=1,
                updates={"status": WorkflowStatus.PAUSED.value},
                events=(event,),
            ),
        )


def test_same_operation_with_different_content_is_a_conflict() -> None:
    first = apply_state_patch(_state(), _running_patch())
    changed = StatePatch(
        operation_id="prepare:start",
        expected_revision=1,
        updates={"status": WorkflowStatus.PAUSED.value},
    )

    with pytest.raises(OperationConflict):
        apply_state_patch(first.state, changed)


@pytest.mark.parametrize("unstable_value", [object(), (1, 2), {1: "integer-key"}])
def test_non_stable_json_patch_is_rejected_before_state_application(
    unstable_value: object,
) -> None:
    state = _state()
    before = copy.deepcopy(state)
    patch = StatePatch(
        operation_id="invalid:runtime-object",
        expected_revision=0,
        updates={"failure": {"unstable": unstable_value}},
    )

    with pytest.raises(InvalidStatePatch, match="JSON"):
        apply_state_patch(state, patch)
    assert state == before


def test_lifecycle_transitions_cannot_move_backwards_or_skip_running() -> None:
    running = _translation_running_state()
    with pytest.raises(InvalidStatePatch, match="pending 工作流"):
        apply_state_patch(
            running,
            StatePatch(
                operation_id="workflow:back-to-pending",
                expected_revision=4,
                updates={"status": WorkflowStatus.PENDING.value},
            ),
        )

    with pytest.raises(InvalidStatePatch, match="preparation"):
        apply_state_patch(
            _state(),
            StatePatch(
                operation_id="prepare:skip-running",
                expected_revision=0,
                updates={
                    "status": WorkflowStatus.RUNNING.value,
                    "preparation": {
                        "status": StageStatus.COMPLETED.value,
                        "normalized_source": _artifact(uri="artifact://normalized/book.epub"),
                    },
                },
            ),
        )


def test_completed_and_skipped_stages_are_terminal_for_normal_patches() -> None:
    running = _translation_running_state()
    for stage_name in ("preparation", "understanding"):
        with pytest.raises(InvalidStatePatch, match=stage_name):
            stage = copy.deepcopy(running[stage_name])
            stage["status"] = StageStatus.RUNNING.value
            apply_state_patch(
                running,
                StatePatch(
                    operation_id=f"{stage_name}:reopen",
                    expected_revision=4,
                    updates={stage_name: stage},
                ),
            )

    changed_preparation = copy.deepcopy(running["preparation"])
    changed_preparation["normalized_source"] = _artifact(uri="artifact://normalized/replaced.epub")
    with pytest.raises(InvalidStatePatch, match="终态阶段 preparation"):
        apply_state_patch(
            running,
            StatePatch(
                operation_id="prepare:replace-terminal-artifact",
                expected_revision=4,
                updates={"preparation": changed_preparation},
            ),
        )


def test_cursor_phase_and_translation_positions_can_only_move_forward() -> None:
    """Batch checkpoints, rather than free cursor writes, own translation progress."""
    state = _translation_running_state(chapter_count=3)
    first_batch = _artifact(
        uri="artifact://batches/0-0-5.json",
        media_type=TRANSLATION_BATCH_ARTIFACT_MEDIA_TYPE,
    )
    positioned = apply_state_patch(
        state,
        StatePatch(
            operation_id="translation:position-chapter-1",
            expected_revision=4,
            updates={
                "cursor": {
                    "phase": WorkflowPhase.TRANSLATE_CHAPTERS.value,
                    "chapter_index": 0,
                    "segment_offset": 5,
                    "review_round": None,
                },
                "translation": {
                    **state["translation"],
                    "batch_artifacts": {"0:0:5": first_batch},
                },
            },
        ),
    )

    backward_cursors = (
        {
            "phase": WorkflowPhase.PREPARE.value,
            "chapter_index": None,
            "segment_offset": None,
            "review_round": None,
        },
        {
            "phase": WorkflowPhase.TRANSLATE_CHAPTERS.value,
            "chapter_index": 0,
            "segment_offset": 4,
            "review_round": None,
        },
        {
            "phase": WorkflowPhase.TRANSLATE_CHAPTERS.value,
            "chapter_index": 1,
            "segment_offset": 0,
            "review_round": None,
        },
    )
    for index, cursor in enumerate(backward_cursors):
        with pytest.raises(InvalidStatePatch, match="cursor|游标"):
            apply_state_patch(
                positioned.state,
                StatePatch(
                    operation_id=f"translation:backward-cursor-{index}",
                    expected_revision=5,
                    updates={"cursor": cursor},
                ),
            )

    next_chapter = apply_state_patch(
        positioned.state,
        StatePatch(
            operation_id="translation:finalize-chapter-0",
            expected_revision=5,
            updates={
                "cursor": {
                    "phase": WorkflowPhase.TRANSLATE_CHAPTERS.value,
                    "chapter_index": 1,
                    "segment_offset": 0,
                    "review_round": None,
                },
                "translation": {
                    **positioned.state["translation"],
                    "completed_chapters": [0],
                    "chapter_artifacts": {
                        "0": _artifact(uri="artifact://chapters/0.json"),
                    },
                },
            },
        ),
    )
    assert next_chapter.state["cursor"]["chapter_index"] == 1
    assert next_chapter.state["cursor"]["segment_offset"] == 0


def test_batch_patch_appends_exactly_one_range_and_cannot_finalize_chapter() -> None:
    """One reducer commit has one crash-recovery meaning: batch or chapter, never both."""
    state = _translation_running_state()
    batch_zero = _artifact(
        uri="artifact://batches/0-0-2.json",
        media_type=TRANSLATION_BATCH_ARTIFACT_MEDIA_TYPE,
    )
    batch_one = _artifact(
        uri="artifact://batches/0-2-4.json",
        media_type=TRANSLATION_BATCH_ARTIFACT_MEDIA_TYPE,
    )
    with pytest.raises(InvalidStatePatch, match="恰好追加一个"):
        apply_state_patch(
            state,
            StatePatch(
                operation_id="translation:add-two-batches",
                expected_revision=4,
                updates={
                    "cursor": {**state["cursor"], "segment_offset": 4},
                    "translation": {
                        **state["translation"],
                        "batch_artifacts": {
                            "0:0:2": batch_zero,
                            "0:2:4": batch_one,
                        },
                    },
                },
            ),
        )

    with pytest.raises(InvalidStatePatch, match="finalize"):
        apply_state_patch(
            state,
            StatePatch(
                operation_id="translation:add-batch-and-finalize",
                expected_revision=4,
                updates={
                    "cursor": {**state["cursor"], "segment_offset": 2},
                    "translation": {
                        **state["translation"],
                        "batch_artifacts": {"0:0:2": batch_zero},
                        "completed_chapters": [0],
                        "chapter_artifacts": {
                            "0": _artifact(uri="artifact://chapters/0.json"),
                        },
                    },
                },
            ),
        )


def test_chapter_finalize_commits_only_the_current_chapter() -> None:
    """A valid prefix cannot hide a node that skipped two finalize boundaries."""
    state = _translation_running_state(chapter_count=3)

    with pytest.raises(InvalidStatePatch, match="每次只能提交当前游标指向的一章"):
        apply_state_patch(
            state,
            StatePatch(
                operation_id="translation:finalize-two-chapters",
                expected_revision=4,
                updates={
                    "cursor": {
                        **state["cursor"],
                        "chapter_index": 2,
                        "segment_offset": 0,
                    },
                    "translation": {
                        **state["translation"],
                        "completed_chapters": [0, 1],
                        "chapter_artifacts": {
                            "0": _artifact(uri="artifact://chapters/0.json"),
                            "1": _artifact(uri="artifact://chapters/1.json"),
                        },
                    },
                },
            ),
        )


def test_finalized_chapter_rejects_late_batch_artifacts() -> None:
    """The final-chapter cursor window cannot reopen an already published chapter."""
    state = _translation_running_state()
    finalized = apply_state_patch(
        state,
        StatePatch(
            operation_id="translation:finalize-only-chapter",
            expected_revision=state["revision"],
            updates={
                "translation": {
                    **state["translation"],
                    "completed_chapters": [0],
                    "chapter_artifacts": {
                        "0": _artifact(uri="artifact://chapters/0.json"),
                    },
                },
            },
        ),
    )
    late_batch = _artifact(
        uri="artifact://batches/0-0-1.json",
        media_type=TRANSLATION_BATCH_ARTIFACT_MEDIA_TYPE,
    )

    with pytest.raises(InvalidStatePatch, match="已 finalize 章节"):
        apply_state_patch(
            finalized.state,
            StatePatch(
                operation_id="translation:late-batch-after-finalize",
                expected_revision=finalized.state["revision"],
                updates={
                    "cursor": {**finalized.state["cursor"], "segment_offset": 1},
                    "translation": {
                        **finalized.state["translation"],
                        "batch_artifacts": {"0:0:1": late_batch},
                    },
                },
            ),
        )


def test_committed_progress_and_counters_can_only_grow() -> None:
    state = _translation_running_state()
    chapter = _artifact(uri="artifact://chapters/0.json")
    progressed = apply_state_patch(
        state,
        StatePatch(
            operation_id="translation:commit-chapter-0",
            expected_revision=4,
            updates={
                "translation": {
                    "status": StageStatus.RUNNING.value,
                    "batch_artifacts": {},
                    "completed_chapters": [0],
                    "chapter_artifacts": {"0": chapter},
                },
                "accounting": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            },
        ),
    )

    with pytest.raises(InvalidStatePatch, match="completed_chapters"):
        apply_state_patch(
            progressed.state,
            StatePatch(
                operation_id="translation:erase-chapter-0",
                expected_revision=5,
                updates={
                    "translation": {
                        "status": StageStatus.RUNNING.value,
                        "batch_artifacts": {},
                        "completed_chapters": [],
                        "chapter_artifacts": {},
                    }
                },
            ),
        )

    with pytest.raises(InvalidStatePatch, match="accounting.prompt_tokens"):
        apply_state_patch(
            progressed.state,
            StatePatch(
                operation_id="accounting:decrease",
                expected_revision=5,
                updates={
                    "accounting": {
                        "prompt_tokens": 9,
                        "completion_tokens": 5,
                        "total_tokens": 14,
                    }
                },
            ),
        )


def test_empty_book_structure_is_frozen_once_document_is_bound() -> None:
    bound = apply_state_patch(
        _state(),
        StatePatch(
            operation_id="book:bind-empty-document",
            expected_revision=0,
            updates={
                "status": WorkflowStatus.RUNNING.value,
                "preparation": {
                    "status": StageStatus.RUNNING.value,
                    "normalized_source": None,
                },
                "book": {
                    "document_artifact": _artifact(uri="artifact://documents/empty.json"),
                    "chapter_count": 0,
                    "source_segment_count": 0,
                },
            },
        ),
    )

    with pytest.raises(InvalidStatePatch, match="chapter_count"):
        apply_state_patch(
            bound.state,
            StatePatch(
                operation_id="book:redefine-empty-document",
                expected_revision=1,
                updates={
                    "book": {
                        **bound.state["book"],
                        "chapter_count": 2,
                        "source_segment_count": 10,
                    }
                },
            ),
        )


def test_glossary_revision_and_review_round_own_their_results() -> None:
    running = _translation_running_state()
    with pytest.raises(InvalidStatePatch, match="glossary.revision"):
        apply_state_patch(
            running,
            StatePatch(
                operation_id="glossary:rewrite-revision-zero",
                expected_revision=4,
                updates={
                    "glossary": {
                        "status": StageStatus.RUNNING.value,
                        "revision": 0,
                        "snapshot": _artifact(uri="artifact://glossary/revision-0.json"),
                    }
                },
            ),
        )

    review = _review_running_state()
    review_started = apply_state_patch(
        review,
        StatePatch(
            operation_id="review:commit-round-1-chunk-0",
            expected_revision=6,
            updates={
                "review": {
                    **review["review"],
                    "chunk_results": {
                        "round-1-chunk-0": _artifact(uri="artifact://review/round-1-chunk-0.json")
                    },
                },
            },
        ),
    )

    with pytest.raises(InvalidStatePatch, match="review.chunk_results"):
        apply_state_patch(
            review_started.state,
            StatePatch(
                operation_id="review:erase-round-1-chunk",
                expected_revision=7,
                updates={
                    "review": {
                        **review_started.state["review"],
                        "chunk_results": {},
                    }
                },
            ),
        )


def test_glossary_snapshot_revisions_are_contiguous_and_content_addressed() -> None:
    """术语快照与标题快照共用连续修订和全新内容身份规则。"""
    running = _translation_running_state()
    first = apply_state_patch(
        running,
        StatePatch(
            operation_id="glossary:commit-revision-1",
            expected_revision=4,
            updates={
                "glossary": {
                    **running["glossary"],
                    "revision": 1,
                    "snapshot": _artifact(
                        uri="artifact://glossary/revision-1.json",
                        sha256="1" * 64,
                    ),
                }
            },
        ),
    )

    # 只换 URI 仍是同一内容；跳过修订号也会破坏恢复顺序。
    invalid_updates = (
        {
            **first.state["glossary"],
            "revision": 2,
            "snapshot": _artifact(
                uri="artifact://glossary/revision-2-alias.json",
                sha256="1" * 64,
            ),
        },
        {
            **first.state["glossary"],
            "revision": 3,
            "snapshot": _artifact(
                uri="artifact://glossary/revision-3.json",
                sha256="3" * 64,
            ),
        },
    )
    for index, updates in enumerate(invalid_updates):
        with pytest.raises(InvalidStatePatch, match="glossary.revision"):
            apply_state_patch(
                first.state,
                StatePatch(
                    operation_id=f"glossary:invalid-revision-{index}",
                    expected_revision=5,
                    updates={"glossary": updates},
                ),
            )

    second = apply_state_patch(
        first.state,
        StatePatch(
            operation_id="glossary:commit-revision-2",
            expected_revision=5,
            updates={
                "glossary": {
                    **first.state["glossary"],
                    "revision": 2,
                    "snapshot": _artifact(
                        uri="artifact://glossary/revision-2.json",
                        sha256="2" * 64,
                    ),
                }
            },
        ),
    )
    assert second.state["glossary"]["revision"] == 2


def test_title_progress_is_versioned_and_cannot_be_skipped() -> None:
    """标题批次以稳定 ID 和 snapshot 修订提交，未完成时不能进入 review。"""
    titles = _title_running_state(chapter_count=2)
    partial = apply_state_patch(
        titles,
        StatePatch(
            operation_id="titles:commit-chapter-0",
            expected_revision=titles["revision"],
            updates={
                "titles": {
                    **titles["titles"],
                    "completed_title_ids": ["chapter-0"],
                    "revision": 1,
                    "snapshot": _artifact(uri="artifact://titles/revision-1.json"),
                }
            },
        ),
    )

    with pytest.raises(InvalidStatePatch, match="同一 titles.revision"):
        apply_state_patch(
            partial.state,
            StatePatch(
                operation_id="titles:rewrite-revision-1",
                expected_revision=partial.state["revision"],
                updates={
                    "titles": {
                        **partial.state["titles"],
                        "snapshot": _artifact(uri="artifact://titles/replaced.json"),
                    }
                },
            ),
        )

    with pytest.raises(InvalidStatePatch, match="titles"):
        apply_state_patch(
            partial.state,
            StatePatch(
                operation_id="review:start-before-titles-complete",
                expected_revision=partial.state["revision"],
                updates={
                    "cursor": {
                        "phase": WorkflowPhase.REVIEW.value,
                        "chapter_index": None,
                        "segment_offset": None,
                        "review_round": 1,
                    },
                    "review": {
                        "status": StageStatus.RUNNING.value,
                        "round": 1,
                        "reviewed_content_digest": "c" * 64,
                        "latest_result": None,
                        "latest_result_round": None,
                        "chunk_results": {},
                    },
                },
            ),
        )


def test_title_progress_and_snapshot_advance_as_one_atomic_unit() -> None:
    """每批标题完成账本都必须由连续修订号和全新快照证明。"""
    titles = _title_running_state(chapter_count=2)
    first = apply_state_patch(
        titles,
        StatePatch(
            operation_id="titles:commit-first",
            expected_revision=titles["revision"],
            updates={
                "titles": {
                    **titles["titles"],
                    "completed_title_ids": ["chapter-0"],
                    "revision": 1,
                    "snapshot": _artifact(uri="artifact://titles/revision-1.json"),
                }
            },
        ),
    )

    # 只改完成集合、只改修订号或复用旧快照都不能代表新批次。
    invalid_title_updates = (
        {
            **first.state["titles"],
            "completed_title_ids": ["chapter-0", "chapter-1"],
        },
        {
            **first.state["titles"],
            "completed_title_ids": ["chapter-0", "chapter-1"],
            "revision": 2,
        },
        {
            **first.state["titles"],
            "revision": 2,
        },
        {
            **first.state["titles"],
            "completed_title_ids": ["chapter-0", "chapter-1"],
            "revision": 2,
            "snapshot": _artifact(uri="artifact://titles/revision-2-same-content.json"),
        },
        {
            **first.state["titles"],
            "completed_title_ids": ["chapter-0", "chapter-1"],
            "revision": 2,
            "snapshot": _artifact(
                uri="artifact://titles/revision-1.json",
                sha256="2" * 64,
            ),
        },
    )
    for index, updates in enumerate(invalid_title_updates):
        with pytest.raises(InvalidStatePatch, match="titles|snapshot|标题进度"):
            apply_state_patch(
                first.state,
                StatePatch(
                    operation_id=f"titles:invalid-atomic-unit-{index}",
                    expected_revision=first.state["revision"],
                    updates={"titles": updates},
                ),
            )

    second = apply_state_patch(
        first.state,
        StatePatch(
            operation_id="titles:commit-second",
            expected_revision=first.state["revision"],
            updates={
                "titles": {
                    **first.state["titles"],
                    "completed_title_ids": ["chapter-0", "chapter-1"],
                    "revision": 2,
                    "snapshot": _artifact(
                        uri="artifact://titles/revision-2.json",
                        sha256="2" * 64,
                    ),
                }
            },
        ),
    )
    assert second.state["titles"]["revision"] == 2
    assert second.state["titles"]["completed_title_ids"] == ["chapter-0", "chapter-1"]


def test_title_revision_cannot_skip_versions_and_completion_can_be_status_only() -> None:
    """标题修订不能跳号；进度已落盘后可单独封闭阶段。"""
    titles = _title_running_state(chapter_count=1)
    with pytest.raises(InvalidStatePatch, match="每次只能增加 1"):
        apply_state_patch(
            titles,
            StatePatch(
                operation_id="titles:skip-revisions",
                expected_revision=5,
                updates={
                    "titles": {
                        **titles["titles"],
                        "completed_title_ids": ["chapter-0"],
                        "revision": 7,
                        "snapshot": _artifact(uri="artifact://titles/revision-7.json"),
                    }
                },
            ),
        )

    committed = apply_state_patch(
        titles,
        StatePatch(
            operation_id="titles:commit-all",
            expected_revision=5,
            updates={
                "titles": {
                    **titles["titles"],
                    "completed_title_ids": ["chapter-0"],
                    "revision": 1,
                    "snapshot": _artifact(uri="artifact://titles/revision-1.json"),
                }
            },
        ),
    )
    completed = apply_state_patch(
        committed.state,
        StatePatch(
            operation_id="titles:mark-completed",
            expected_revision=6,
            updates={
                "cursor": {
                    "phase": WorkflowPhase.REVIEW.value,
                    "chapter_index": None,
                    "segment_offset": None,
                    "review_round": 1,
                },
                "titles": {
                    **committed.state["titles"],
                    "status": StageStatus.COMPLETED.value,
                },
                "review": {
                    "status": StageStatus.RUNNING.value,
                    "round": 1,
                    "reviewed_content_digest": "c" * 64,
                    "latest_result": None,
                    "latest_result_round": None,
                    "chunk_results": {},
                },
            },
        ),
    )
    assert completed.state["titles"]["revision"] == 1
    assert completed.state["titles"]["status"] == StageStatus.COMPLETED.value


def test_empty_title_set_still_publishes_one_versioned_snapshot() -> None:
    """空书也要以 revision 1 快照证明标题阶段确实执行过。"""
    titles = _title_running_state(chapter_count=0)
    completed = apply_state_patch(
        titles,
        StatePatch(
            operation_id="titles:complete-empty-set",
            expected_revision=5,
            updates={
                "cursor": {
                    "phase": WorkflowPhase.REVIEW.value,
                    "chapter_index": None,
                    "segment_offset": None,
                    "review_round": 1,
                },
                "titles": {
                    **titles["titles"],
                    "status": StageStatus.COMPLETED.value,
                    "revision": 1,
                    "snapshot": _artifact(uri="artifact://titles/revision-1.json"),
                },
                "review": {
                    "status": StageStatus.RUNNING.value,
                    "round": 1,
                    "reviewed_content_digest": "c" * 64,
                    "latest_result": None,
                    "latest_result_round": None,
                    "chunk_results": {},
                },
            },
        ),
    )
    assert completed.state["titles"]["completed_title_ids"] == []
    assert completed.state["titles"]["revision"] == 1


def test_review_new_round_can_bind_a_new_content_digest() -> None:
    """同轮摘要不可改写，但修复后的下一轮必须能绑定新的输入身份。"""
    review = _review_running_state()
    first_result = apply_state_patch(
        review,
        StatePatch(
            operation_id="review:finish-round-1",
            expected_revision=6,
            updates={
                "review": {
                    **review["review"],
                    "latest_result": _artifact(uri="artifact://review/round-1.json"),
                    "latest_result_round": 1,
                }
            },
        ),
    )
    second_round = apply_state_patch(
        first_result.state,
        StatePatch(
            operation_id="review:start-round-2",
            expected_revision=7,
            updates={
                "cursor": {
                    **first_result.state["cursor"],
                    "review_round": 2,
                },
                "review": {
                    **first_result.state["review"],
                    "round": 2,
                    "reviewed_content_digest": "d" * 64,
                    "latest_result": None,
                    "latest_result_round": None,
                },
            },
        ),
    )

    assert second_round.state["review"]["round"] == 2
    assert second_round.state["review"]["reviewed_content_digest"] == "d" * 64

    completed = apply_state_patch(
        second_round.state,
        StatePatch(
            operation_id="review:complete-round-2",
            expected_revision=8,
            updates={
                "cursor": {
                    "phase": WorkflowPhase.QUALITY.value,
                    "chapter_index": None,
                    "segment_offset": None,
                    "review_round": None,
                },
                "review": {
                    **second_round.state["review"],
                    "status": StageStatus.COMPLETED.value,
                    "latest_result": _artifact(uri="artifact://review/round-2.json"),
                    "latest_result_round": 2,
                },
            },
        ),
    )
    assert completed.state["review"]["latest_result_round"] == 2


def test_new_review_round_rejects_inherited_or_missing_prior_results() -> None:
    """新轮次必须在上一轮有正式结果后开始，并清空全部旧结果归属。"""
    review = _review_running_state()
    without_prior_result = StatePatch(
        operation_id="review:skip-unfinished-round-1",
        expected_revision=6,
        updates={
            "cursor": {**review["cursor"], "review_round": 2},
            "review": {
                **review["review"],
                "round": 2,
                "reviewed_content_digest": "d" * 64,
            },
        },
    )
    with pytest.raises(InvalidStatePatch, match="上一轮 latest_result"):
        apply_state_patch(review, without_prior_result)

    first_result = apply_state_patch(
        review,
        StatePatch(
            operation_id="review:bind-round-1-result",
            expected_revision=6,
            updates={
                "review": {
                    **review["review"],
                    "latest_result": _artifact(uri="artifact://review/round-1.json"),
                    "latest_result_round": 1,
                }
            },
        ),
    )
    inherited = StatePatch(
        operation_id="review:inherit-round-1-result",
        expected_revision=7,
        updates={
            "cursor": {**first_result.state["cursor"], "review_round": 2},
            "review": {
                **first_result.state["review"],
                "round": 2,
                "reviewed_content_digest": "d" * 64,
                "latest_result_round": 2,
            },
        },
    )
    with pytest.raises(InvalidStatePatch, match="清空上一轮 latest_result"):
        apply_state_patch(first_result.state, inherited)


def test_translation_phase_can_fail_and_resume_from_either_active_stage() -> None:
    """翻译和术语并行期间，失败归属与恢复必须保留真实的活跃阶段。"""
    running = _translation_running_state()
    failed = apply_state_patch(
        running,
        StatePatch(
            operation_id="glossary:failed",
            expected_revision=4,
            updates={
                "status": WorkflowStatus.FAILED.value,
                "failure": {
                    "code": "glossary_error",
                    "message": "temporary glossary failure",
                    "retryable": True,
                    "details": {},
                },
                "glossary": {
                    **running["glossary"],
                    "status": StageStatus.FAILED.value,
                },
            },
        ),
    )
    resumed = apply_state_patch(
        failed.state,
        StatePatch(
            operation_id="glossary:resume",
            expected_revision=5,
            updates={
                "status": WorkflowStatus.RUNNING.value,
                "failure": None,
                "glossary": {
                    **failed.state["glossary"],
                    "status": StageStatus.RUNNING.value,
                },
            },
        ),
    )

    assert resumed.state["translation"]["status"] == StageStatus.RUNNING.value
    assert resumed.state["glossary"]["status"] == StageStatus.RUNNING.value


def test_translation_can_resume_after_the_parallel_glossary_has_completed() -> None:
    """已完成的并行分支保持冻结，失败恢复只重启真正失败的 translation。"""
    running = _translation_running_state()
    glossary_completed = apply_state_patch(
        running,
        StatePatch(
            operation_id="glossary:complete-early",
            expected_revision=4,
            updates={
                "glossary": {
                    "status": StageStatus.COMPLETED.value,
                    "revision": 1,
                    "snapshot": _artifact(uri="artifact://glossary/revision-1.json"),
                }
            },
        ),
    )
    failed = apply_state_patch(
        glossary_completed.state,
        StatePatch(
            operation_id="translation:failed-after-glossary",
            expected_revision=5,
            updates={
                "status": WorkflowStatus.FAILED.value,
                "failure": {
                    "code": "translation_error",
                    "message": "temporary translation failure",
                    "retryable": True,
                    "details": {},
                },
                "translation": {
                    **glossary_completed.state["translation"],
                    "status": StageStatus.FAILED.value,
                },
            },
        ),
    )
    resumed = apply_state_patch(
        failed.state,
        StatePatch(
            operation_id="translation:resume-after-glossary",
            expected_revision=6,
            updates={
                "status": WorkflowStatus.RUNNING.value,
                "failure": None,
                "translation": {
                    **failed.state["translation"],
                    "status": StageStatus.RUNNING.value,
                },
            },
        ),
    )

    assert resumed.state["translation"]["status"] == StageStatus.RUNNING.value
    assert resumed.state["glossary"] == glossary_completed.state["glossary"]


def test_glossary_can_resume_after_parallel_chapter_translation_has_completed() -> None:
    """正文已完成时仍可在原 phase 恢复 glossary，标题阶段不会被提前宣告完成。"""
    running = _translation_running_state()
    translation_completed = apply_state_patch(
        running,
        StatePatch(
            operation_id="translation:complete-before-glossary",
            expected_revision=4,
            updates={
                "translation": {
                    "status": StageStatus.COMPLETED.value,
                    "batch_artifacts": {},
                    "completed_chapters": [0],
                    "chapter_artifacts": {
                        "0": _artifact(uri="artifact://chapters/0.json"),
                    },
                }
            },
        ),
    )
    failed = apply_state_patch(
        translation_completed.state,
        StatePatch(
            operation_id="glossary:fail-after-translation",
            expected_revision=5,
            updates={
                "status": WorkflowStatus.FAILED.value,
                "failure": {
                    "code": "glossary_error",
                    "message": "temporary glossary failure",
                    "retryable": True,
                    "details": {},
                },
                "glossary": {
                    **translation_completed.state["glossary"],
                    "status": StageStatus.FAILED.value,
                },
            },
        ),
    )
    resumed = apply_state_patch(
        failed.state,
        StatePatch(
            operation_id="glossary:resume-after-translation",
            expected_revision=6,
            updates={
                "status": WorkflowStatus.RUNNING.value,
                "failure": None,
                "glossary": {
                    **failed.state["glossary"],
                    "status": StageStatus.RUNNING.value,
                },
            },
        ),
    )

    assert resumed.state["translation"]["status"] == StageStatus.COMPLETED.value
    assert resumed.state["titles"]["status"] == StageStatus.PENDING.value


def test_running_workflow_can_pause_without_rewriting_active_stage() -> None:
    """暂停只冻结调度，不伪造阶段完成或丢失当前恢复位置。"""
    running = _translation_running_state()
    paused = apply_state_patch(
        running,
        StatePatch(
            operation_id="workflow:pause",
            expected_revision=4,
            updates={"status": WorkflowStatus.PAUSED.value},
        ),
    )
    resumed = apply_state_patch(
        paused.state,
        StatePatch(
            operation_id="workflow:resume",
            expected_revision=5,
            updates={"status": WorkflowStatus.RUNNING.value},
        ),
    )

    assert resumed.state["cursor"] == running["cursor"]
    assert resumed.state["translation"] == running["translation"]


def test_paused_and_failed_workflows_cannot_continue_business_progress() -> None:
    """强暂停/失败快照先恢复控制状态，之后的独立补丁才能继续计费或推进。"""
    running = _translation_running_state()
    paused = apply_state_patch(
        running,
        StatePatch(
            operation_id="workflow:pause-before-progress",
            expected_revision=4,
            updates={"status": WorkflowStatus.PAUSED.value},
        ),
    )
    with pytest.raises(InvalidStatePatch, match="不允许工作流状态"):
        apply_state_patch(
            paused.state,
            StatePatch(
                operation_id="translation:progress-while-paused",
                expected_revision=5,
                updates={
                    "accounting": {
                        "prompt_tokens": 1,
                        "completion_tokens": 0,
                        "total_tokens": 1,
                    }
                },
            ),
        )

    failed = apply_state_patch(
        running,
        StatePatch(
            operation_id="translation:fail-before-progress",
            expected_revision=4,
            updates={
                "status": WorkflowStatus.FAILED.value,
                "failure": {
                    "code": "translation_error",
                    "message": "temporary failure",
                    "retryable": True,
                    "details": {},
                },
                "translation": {
                    **running["translation"],
                    "status": StageStatus.FAILED.value,
                },
            },
        ),
    )
    with pytest.raises(InvalidStatePatch, match="不允许工作流状态"):
        apply_state_patch(
            failed.state,
            StatePatch(
                operation_id="translation:progress-while-failed",
                expected_revision=5,
                updates={
                    "accounting": {
                        "prompt_tokens": 1,
                        "completion_tokens": 0,
                        "total_tokens": 1,
                    }
                },
            ),
        )


@pytest.mark.parametrize("started", [False, True], ids=["pending", "running"])
def test_prepare_failure_cannot_smuggle_business_progress(started: bool) -> None:
    """失败补丁只改变控制状态，保留最后一次成功提交的恢复事实。"""
    current = _state()
    if started:
        current = apply_state_patch(current, _running_patch()).state

    expected_revision = current["revision"]
    failure = {
        "code": "prepare_error",
        "message": "source preparation failed",
        "retryable": True,
        "details": {},
    }
    failed_preparation = {
        "status": StageStatus.FAILED.value,
        "normalized_source": None,
    }
    invalid_progress = (
        (
            "book",
            {
                "book": {
                    "document_artifact": _artifact(uri="artifact://documents/partial.json"),
                    "chapter_count": 1,
                    "source_segment_count": 1,
                }
            },
            "book 业务进度",
        ),
        (
            "accounting",
            {
                "accounting": {
                    "prompt_tokens": 1,
                    "completion_tokens": 0,
                    "total_tokens": 1,
                }
            },
            "accounting 业务进度",
        ),
        (
            "preparation-payload",
            {
                "preparation": {
                    "status": StageStatus.FAILED.value,
                    "normalized_source": _artifact(uri="artifact://normalized/partial.epub"),
                }
            },
            "preparation 业务内容",
        ),
        (
            "future-stage-payload",
            {
                "exports": {
                    "status": StageStatus.PENDING.value,
                    "requested_formats": ["epub"],
                    "outputs": {},
                }
            },
            "exports 业务内容",
        ),
    )

    for case, extra_updates, error_pattern in invalid_progress:
        updates = {
            "status": WorkflowStatus.FAILED.value,
            "failure": failure,
            "preparation": failed_preparation,
            **extra_updates,
        }
        with pytest.raises(InvalidStatePatch, match=error_pattern):
            apply_state_patch(
                current,
                StatePatch(
                    operation_id=f"prepare:fail-with-{case}:{current['status']}",
                    expected_revision=expected_revision,
                    updates=updates,
                ),
            )


def test_running_failure_cannot_advance_recovery_cursor() -> None:
    """失败边界不能把未成功提交的翻译批次伪装成恢复游标。"""
    running = _translation_running_state()
    batch = _artifact(
        uri="artifact://batches/0-0-5.json",
        media_type=TRANSLATION_BATCH_ARTIFACT_MEDIA_TYPE,
    )

    with pytest.raises(InvalidStatePatch, match="cursor 业务进度"):
        apply_state_patch(
            running,
            StatePatch(
                operation_id="translation:fail-with-cursor-progress",
                expected_revision=4,
                updates={
                    "status": WorkflowStatus.FAILED.value,
                    "failure": {
                        "code": "translation_error",
                        "message": "batch did not commit successfully",
                        "retryable": True,
                        "details": {},
                    },
                    "cursor": {
                        "phase": WorkflowPhase.TRANSLATE_CHAPTERS.value,
                        "chapter_index": 0,
                        "segment_offset": 5,
                        "review_round": None,
                    },
                    "translation": {
                        **running["translation"],
                        "status": StageStatus.FAILED.value,
                        # 让候选快照自身满足 cursor/batch 交叉约束，确保本测试
                        # 真正命中相邻状态的失败控制边界，而非提前失败于形状校验。
                        "batch_artifacts": {"0:0:5": batch},
                    },
                },
            ),
        )


def test_running_failure_cannot_publish_uncommitted_batch_artifact() -> None:
    """游标不变时也不能通过失败补丁改写已提交 batch 引用。"""
    running = _translation_running_state()
    committed_batch = _artifact(
        uri="artifact://batches/0-0-5.json",
        media_type=TRANSLATION_BATCH_ARTIFACT_MEDIA_TYPE,
    )
    positioned = apply_state_patch(
        running,
        StatePatch(
            operation_id="translation:commit-batch-before-failure",
            expected_revision=4,
            updates={
                "cursor": {**running["cursor"], "segment_offset": 5},
                "translation": {
                    **running["translation"],
                    "batch_artifacts": {"0:0:5": committed_batch},
                },
            },
        ),
    ).state
    rewritten_batch = _artifact(
        uri="artifact://batches/0-0-5-rewritten.json",
        sha256="c" * 64,
        media_type=TRANSLATION_BATCH_ARTIFACT_MEDIA_TYPE,
    )

    with pytest.raises(InvalidStatePatch, match="translation 业务内容"):
        apply_state_patch(
            positioned,
            StatePatch(
                operation_id="translation:fail-with-batch-artifact",
                expected_revision=5,
                updates={
                    "status": WorkflowStatus.FAILED.value,
                    "failure": {
                        "code": "translation_error",
                        "message": "batch did not commit successfully",
                        "retryable": True,
                        "details": {},
                    },
                    "translation": {
                        **positioned["translation"],
                        "status": StageStatus.FAILED.value,
                        # 同键的新引用仍是一个自洽快照，但失败控制操作无权
                        # 改写最后一次成功提交的业务内容。
                        "batch_artifacts": {"0:0:5": rewritten_batch},
                    },
                },
            ),
        )


def test_prepare_failure_accepts_only_failure_summary_and_stage_status() -> None:
    """合法失败保留 prepare 恢复位置，并可发布规范生命周期事件。"""
    running = apply_state_patch(_state(), _running_patch()).state
    failure = {
        "code": "prepare_error",
        "message": "source preparation failed",
        "retryable": True,
        "details": {},
    }

    application = apply_state_patch(
        running,
        StatePatch(
            operation_id="prepare:fail-cleanly",
            expected_revision=1,
            updates={
                "status": WorkflowStatus.FAILED.value,
                "failure": failure,
                "preparation": {
                    "status": StageStatus.FAILED.value,
                    "normalized_source": None,
                },
            },
            events=(
                {
                    "event_id": "prepare-failed-cleanly",
                    "event_type": "workflow.failed",
                    "payload": {"code": failure["code"], "retryable": True},
                },
            ),
        ),
    )

    assert application.state["cursor"] == running["cursor"]
    assert application.state["book"] == running["book"]
    assert application.state["accounting"] == running["accounting"]
    assert application.state["preparation"]["status"] == StageStatus.FAILED.value


def _failure_source_state(status: str):
    """构造 pending/running/paused 三种合法的失败源快照。"""
    state = _state()
    if status == WorkflowStatus.PENDING.value:
        return state
    state = apply_state_patch(state, _running_patch()).state
    if status == WorkflowStatus.PAUSED.value:
        state = apply_state_patch(
            state,
            StatePatch(
                operation_id="workflow:pause-before-failure-effects",
                expected_revision=1,
                updates={"status": WorkflowStatus.PAUSED.value},
            ),
        ).state
    return state


def _clean_failure_patch(state, *, events=(), created_artifacts=()) -> StatePatch:
    """为控制效果矩阵构造只改变失败摘要与阶段状态的补丁。"""
    revision = state["revision"]
    return StatePatch(
        operation_id=f"prepare:fail-effects:{state['status']}:{revision}",
        expected_revision=revision,
        updates={
            "status": WorkflowStatus.FAILED.value,
            "failure": {
                "code": "prepare_error",
                "message": "source preparation failed",
                "retryable": True,
                "details": {},
            },
            "preparation": {
                **state["preparation"],
                "status": StageStatus.FAILED.value,
            },
        },
        events=events,
        created_artifacts=created_artifacts,
    )


@pytest.mark.parametrize(
    "status",
    [
        WorkflowStatus.PENDING.value,
        WorkflowStatus.RUNNING.value,
        WorkflowStatus.PAUSED.value,
    ],
)
def test_failure_control_effects_are_canonical_for_every_source_status(status: str) -> None:
    """每种可失败状态都共享同一个事件和 created-artifact 边界。"""
    state = _failure_source_state(status)
    revision = state["revision"]
    canonical_event = {
        "event_id": f"prepare-failed-effects-{revision}",
        "event_type": "workflow.failed",
        "payload": {"code": "prepare_error", "retryable": True},
    }

    accepted = apply_state_patch(
        state,
        _clean_failure_patch(state, events=(canonical_event,)),
    )
    assert accepted.state["status"] == WorkflowStatus.FAILED.value

    invalid_effects = (
        (
            "artifact",
            (),
            (_artifact(uri="artifact://normalized/orphan.epub"),),
        ),
        (
            "business-event",
            (
                {
                    "event_id": f"prepare-business-event-{revision}",
                    "event_type": "preparation.parsed",
                    "payload": {},
                },
            ),
            (),
        ),
        (
            "wrong-payload",
            (
                {
                    **canonical_event,
                    "event_id": f"prepare-wrong-payload-{revision}",
                    "payload": {"code": "different", "retryable": True},
                },
            ),
            (),
        ),
        (
            "multiple-events",
            (
                canonical_event,
                {
                    **canonical_event,
                    "event_id": f"prepare-failed-effects-extra-{revision}",
                },
            ),
            (),
        ),
    )
    for suffix, events, artifacts in invalid_effects:
        patch = _clean_failure_patch(
            state,
            events=events,
            created_artifacts=artifacts,
        )
        patch = StatePatch(
            operation_id=f"{patch.operation_id}:{suffix}",
            expected_revision=patch.expected_revision,
            updates=patch.updates,
            events=patch.events,
            created_artifacts=patch.created_artifacts,
        )
        with pytest.raises(InvalidStatePatch):
            apply_state_patch(state, patch)


def test_non_retryable_failure_requires_a_separate_override_api() -> None:
    """普通 reducer 尊重 retryable=False；未来人工恢复必须走独立审计命令。"""
    failed = apply_state_patch(
        _state(),
        StatePatch(
            operation_id="prepare:permanent-failure",
            expected_revision=0,
            updates={
                "status": WorkflowStatus.FAILED.value,
                "failure": {
                    "code": "invalid_source",
                    "message": "source cannot be parsed",
                    "retryable": False,
                    "details": {},
                },
                "preparation": {
                    "status": StageStatus.FAILED.value,
                    "normalized_source": None,
                },
            },
        ),
    )
    with pytest.raises(InvalidStatePatch, match="不可重试"):
        apply_state_patch(
            failed.state,
            StatePatch(
                operation_id="prepare:retry-permanent-failure",
                expected_revision=1,
                updates={
                    "status": WorkflowStatus.RUNNING.value,
                    "failure": None,
                    "preparation": {
                        "status": StageStatus.RUNNING.value,
                        "normalized_source": None,
                    },
                },
            ),
        )

    # failure 摘要在 failed 状态内不可改写，否则可以先篡改 retryable 再恢复。
    with pytest.raises(InvalidStatePatch, match="不允许工作流状态"):
        apply_state_patch(
            failed.state,
            StatePatch(
                operation_id="prepare:rewrite-permanent-failure",
                expected_revision=1,
                updates={
                    "failure": {
                        "code": "invalid_source",
                        "message": "source cannot be parsed",
                        "retryable": True,
                        "details": {"override": True},
                    }
                },
            ),
        )


def test_paused_and_failed_states_reject_new_self_loop_effects() -> None:
    """强暂停和强失败不接受新的同状态事件或产物操作。"""
    running = _translation_running_state()
    paused = apply_state_patch(
        running,
        StatePatch(
            operation_id="workflow:pause-for-effect-check",
            expected_revision=4,
            updates={"status": WorkflowStatus.PAUSED.value},
        ),
    )
    failed = apply_state_patch(
        running,
        StatePatch(
            operation_id="workflow:fail-for-effect-check",
            expected_revision=4,
            updates={
                "status": WorkflowStatus.FAILED.value,
                "failure": {
                    "code": "translation_error",
                    "message": "stopped",
                    "retryable": True,
                    "details": {},
                },
                "translation": {
                    **running["translation"],
                    "status": StageStatus.FAILED.value,
                },
            },
        ),
    )

    frozen_states = (("paused", paused.state), ("failed", failed.state))
    for label, frozen in frozen_states:
        with pytest.raises(InvalidStatePatch, match="不允许工作流状态"):
            apply_state_patch(
                frozen,
                StatePatch(
                    operation_id=f"workflow:{label}-event-only",
                    expected_revision=5,
                    updates={"status": frozen["status"]},
                    events=(
                        {
                            "event_id": f"{label}-business-event",
                            "event_type": "translation.output",
                            "payload": {"chapter": 0},
                        },
                    ),
                ),
            )
        with pytest.raises(InvalidStatePatch, match="不允许工作流状态"):
            apply_state_patch(
                frozen,
                StatePatch(
                    operation_id=f"workflow:{label}-artifact-only",
                    expected_revision=5,
                    updates={"status": frozen["status"]},
                    created_artifacts=(_artifact(uri=f"artifact://{label}/unexpected"),),
                ),
            )


def test_control_transitions_reject_business_effects_but_allow_lifecycle_event() -> None:
    """恢复补丁不携带产物，且最多发布一个同义生命周期事件。"""
    running = _translation_running_state()
    paused = apply_state_patch(
        running,
        StatePatch(
            operation_id="workflow:pause-for-resume-check",
            expected_revision=4,
            updates={"status": WorkflowStatus.PAUSED.value},
        ),
    )
    with pytest.raises(InvalidStatePatch, match="created_artifacts"):
        apply_state_patch(
            paused.state,
            StatePatch(
                operation_id="workflow:resume-with-artifact",
                expected_revision=5,
                updates={"status": WorkflowStatus.RUNNING.value},
                created_artifacts=(_artifact(uri="artifact://resume/unexpected"),),
            ),
        )
    with pytest.raises(InvalidStatePatch, match="workflow.resumed"):
        apply_state_patch(
            paused.state,
            StatePatch(
                operation_id="workflow:resume-with-business-event",
                expected_revision=5,
                updates={"status": WorkflowStatus.RUNNING.value},
                events=(
                    {
                        "event_id": "resume-business-event",
                        "event_type": "translation.output",
                        "payload": {},
                    },
                ),
            ),
        )
    with pytest.raises(InvalidStatePatch, match="规范 payload"):
        apply_state_patch(
            paused.state,
            StatePatch(
                operation_id="workflow:resume-with-business-payload",
                expected_revision=5,
                updates={"status": WorkflowStatus.RUNNING.value},
                events=(
                    {
                        "event_id": "resume-business-payload",
                        "event_type": "workflow.resumed",
                        "payload": {"translated_text": "unexpected"},
                    },
                ),
            ),
        )

    resumed = apply_state_patch(
        paused.state,
        StatePatch(
            operation_id="workflow:resume-with-lifecycle-event",
            expected_revision=5,
            updates={"status": WorkflowStatus.RUNNING.value},
            events=(
                {
                    "event_id": "workflow-resumed",
                    "event_type": "workflow.resumed",
                    "payload": {},
                },
            ),
        ),
    )
    assert resumed.events[0]["event_type"] == "workflow.resumed"


def test_duplicate_business_effects_can_replay_after_workflow_is_paused() -> None:
    """旧操作重放早于生命周期检查，因此仍可用于 outbox 补投。"""
    original_patch = _running_patch()
    running = apply_state_patch(_state(), original_patch)
    paused = apply_state_patch(
        running.state,
        StatePatch(
            operation_id="workflow:pause-after-prepare-start",
            expected_revision=1,
            updates={"status": WorkflowStatus.PAUSED.value},
        ),
    )

    replay = apply_state_patch(paused.state, original_patch)

    assert replay.duplicate
    assert replay.state == paused.state
    assert replay.events == running.events
    assert replay.created_artifacts == running.created_artifacts


def test_requested_export_formats_can_be_appended_but_not_removed() -> None:
    requested = apply_state_patch(
        _state(),
        StatePatch(
            operation_id="exports:request-epub",
            expected_revision=0,
            updates={
                "exports": {
                    "status": StageStatus.PENDING.value,
                    "requested_formats": ["epub"],
                    "outputs": {},
                },
            },
        ),
    )

    appended = apply_state_patch(
        requested.state,
        StatePatch(
            operation_id="exports:request-pdf",
            expected_revision=1,
            updates={
                "exports": {
                    "status": StageStatus.PENDING.value,
                    "requested_formats": ["epub", "pdf"],
                    "outputs": {},
                }
            },
        ),
    )
    assert appended.state["exports"]["requested_formats"] == ["epub", "pdf"]

    with pytest.raises(InvalidStatePatch, match="requested_formats"):
        apply_state_patch(
            appended.state,
            StatePatch(
                operation_id="exports:remove-epub",
                expected_revision=2,
                updates={
                    "exports": {
                        "status": StageStatus.PENDING.value,
                        "requested_formats": ["pdf"],
                        "outputs": {},
                    }
                },
            ),
        )


def test_failed_workflow_can_resume_only_when_failure_is_cleared() -> None:
    state = _state()
    failed = apply_state_patch(
        state,
        StatePatch(
            operation_id="prepare:failed",
            expected_revision=0,
            updates={
                "status": WorkflowStatus.FAILED.value,
                "failure": {
                    "code": "network_error",
                    "message": "temporary failure",
                    "retryable": True,
                    "details": {},
                },
                "preparation": {
                    "status": StageStatus.FAILED.value,
                    "normalized_source": None,
                },
            },
        ),
    )

    invalid_resume = StatePatch(
        operation_id="prepare:resume-invalid",
        expected_revision=1,
        updates={"status": WorkflowStatus.RUNNING.value},
    )
    with pytest.raises(InvalidStatePatch, match="只有 failed"):
        apply_state_patch(failed.state, invalid_resume)

    valid_resume = StatePatch(
        operation_id="prepare:resume",
        expected_revision=1,
        updates={
            "status": WorkflowStatus.RUNNING.value,
            "failure": None,
            "preparation": {
                "status": StageStatus.RUNNING.value,
                "normalized_source": None,
            },
        },
    )
    resumed = apply_state_patch(failed.state, valid_resume)
    assert resumed.state["status"] == WorkflowStatus.RUNNING.value
    assert resumed.state["failure"] is None


def test_completed_workflow_is_terminal_for_new_operations() -> None:
    state = _title_running_state()
    completed = apply_state_patch(
        state,
        StatePatch(
            operation_id="workflow:complete",
            expected_revision=5,
            updates=_completed_updates(state),
        ),
    )

    with pytest.raises(InvalidStatePatch, match="终态"):
        apply_state_patch(
            completed.state,
            StatePatch(
                operation_id="workflow:reopen",
                expected_revision=6,
                updates={"status": WorkflowStatus.RUNNING.value},
            ),
        )


def test_empty_book_can_complete_without_inventing_chapter_artifacts() -> None:
    """合法空书也按正常阶段转换闭合，而不是跳过必选 translation。"""
    state = _title_running_state(chapter_count=0)
    completed = apply_state_patch(
        state,
        StatePatch(
            operation_id="workflow:complete-empty-book",
            expected_revision=5,
            updates=_completed_updates(state),
        ),
    )

    assert completed.state["translation"]["completed_chapters"] == []
    assert completed.state["translation"]["chapter_artifacts"] == {}


def test_merge_unique_mapping_is_ordered_and_idempotent() -> None:
    merged = merge_unique_mapping(
        {"chapter-2": {"digest": "b"}},
        {"chapter-1": {"digest": "a"}, "chapter-2": {"digest": "b"}},
    )

    assert list(merged) == ["chapter-1", "chapter-2"]
    assert merged["chapter-2"] == {"digest": "b"}


def test_merge_unique_mapping_rejects_conflicting_parallel_results() -> None:
    with pytest.raises(MergeConflict, match="互相矛盾"):
        merge_unique_mapping(
            {"review-block-1": {"artifact": "a"}},
            {"review-block-1": {"artifact": "b"}},
        )
