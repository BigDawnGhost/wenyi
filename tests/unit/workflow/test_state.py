"""Graph-ready 状态工厂和跨切片不变量测试。"""

from __future__ import annotations

import copy
import json

import pytest

from trans_novel.domain.workflow import StageStatus, WorkflowPhase, WorkflowStatus
from trans_novel.workflow import (
    WORKFLOW_STATE_KEYS,
    new_workflow_state,
    validate_workflow_state,
)

SOURCE_HASH = "a" * 64
PROFILE_HASH = "b" * 64


def _source_artifact() -> dict[str, object]:
    """返回状态工厂使用的最小源文件引用。"""
    return {
        "uri": "artifact://source/book.epub",
        "sha256": SOURCE_HASH,
        "media_type": "application/epub+zip",
        "size_bytes": 123,
    }


def _artifact(uri: str) -> dict[str, object]:
    """为章节和导出切片构造独立 URI 的合法产物引用。"""
    return {**_source_artifact(), "uri": uri}


def _title_ids(chapter_count: int) -> list[str]:
    """用稳定 ID 表示当前测试书籍需要翻译的章节标题。"""
    return [f"chapter-{index}" for index in range(chapter_count)]


def _state():
    """创建每个测试独享的初始状态。"""
    return new_workflow_state(
        source_artifact=_source_artifact(),
        source_format=".EPUB",
        source_lang="JA_jp",
        target_lang="ZH_cn",
        semantic_profile_hash=PROFILE_HASH,
        requested_output_formats=(".EPUB", "pdf", "epub"),
    )


def _enter_translation_phase(state, *, chapter_count: int = 2) -> None:
    """把快照推进到满足全部先决条件的正文翻译阶段。"""
    state["status"] = WorkflowStatus.RUNNING.value
    state["cursor"] = {
        "phase": WorkflowPhase.TRANSLATE_CHAPTERS.value,
        "chapter_index": 0 if chapter_count else None,
        "segment_offset": 0 if chapter_count else None,
        "review_round": None,
    }
    state["book"] = {
        "document_artifact": _artifact("artifact://documents/book.json"),
        "chapter_count": chapter_count,
        "source_segment_count": chapter_count * 10,
    }
    state["preparation"] = {
        "status": StageStatus.COMPLETED.value,
        "normalized_source": _artifact("artifact://normalized/book.epub"),
    }
    state["understanding"]["status"] = StageStatus.SKIPPED.value
    state["translation"]["status"] = StageStatus.RUNNING.value


def _enter_title_phase(state, *, chapter_count: int = 2) -> None:
    """完成正文和术语快照，并绑定标题阶段的稳定输入身份。"""
    _enter_translation_phase(state, chapter_count=chapter_count)
    state["translation"] = {
        "status": StageStatus.COMPLETED.value,
        "completed_chapters": list(range(chapter_count)),
        "chapter_artifacts": {
            str(index): _artifact(f"artifact://chapters/{index}.json")
            for index in range(chapter_count)
        },
    }
    state["glossary"] = {
        "status": StageStatus.COMPLETED.value,
        "revision": 1,
        "snapshot": _artifact("artifact://glossary/revision-1.json"),
    }
    state["titles"] = {
        "status": StageStatus.RUNNING.value,
        "input_digest": "d" * 64,
        "expected_title_ids": _title_ids(chapter_count),
        "completed_title_ids": [],
        "revision": 0,
        "snapshot": None,
    }
    state["cursor"] = {
        "phase": WorkflowPhase.TRANSLATE_TITLES.value,
        "chapter_index": None,
        "segment_offset": None,
        "review_round": None,
    }


def _enter_review_phase(state, *, chapter_count: int = 2) -> None:
    """把快照推进到正文、术语和标题均已正式提交的审校阶段。"""
    _enter_title_phase(state, chapter_count=chapter_count)
    state["titles"] = {
        "status": StageStatus.COMPLETED.value,
        "input_digest": "d" * 64,
        "expected_title_ids": _title_ids(chapter_count),
        "completed_title_ids": _title_ids(chapter_count),
        "revision": 1,
        "snapshot": _artifact("artifact://titles/revision-1.json"),
    }
    state["review"] = {
        "status": StageStatus.RUNNING.value,
        "round": 1,
        "reviewed_content_digest": "c" * 64,
        "latest_result": None,
        "latest_result_round": None,
        "chunk_results": {},
    }
    state["cursor"] = {
        "phase": WorkflowPhase.REVIEW.value,
        "chapter_index": None,
        "segment_offset": None,
        "review_round": 1,
    }


def _enter_quality_phase(state, *, chapter_count: int = 2) -> None:
    """把快照推进到审校已完成、质量评估尚未开始的阶段边界。"""
    _enter_review_phase(state, chapter_count=chapter_count)
    state["review"] = {
        **state["review"],
        "status": StageStatus.COMPLETED.value,
        "latest_result": _artifact("artifact://review/round-1.json"),
        "latest_result_round": 1,
    }
    state["cursor"] = {
        "phase": WorkflowPhase.QUALITY.value,
        "chapter_index": None,
        "segment_offset": None,
        "review_round": None,
    }


def _enter_export_phase(state, *, chapter_count: int = 2) -> None:
    """把快照推进到可选检查已终止、导出尚未开始的阶段边界。"""
    _enter_title_phase(state, chapter_count=chapter_count)
    state["titles"] = {
        "status": StageStatus.COMPLETED.value,
        "input_digest": "d" * 64,
        "expected_title_ids": _title_ids(chapter_count),
        "completed_title_ids": _title_ids(chapter_count),
        "revision": 1,
        "snapshot": _artifact("artifact://titles/revision-1.json"),
    }
    state["review"]["status"] = StageStatus.SKIPPED.value
    state["quality"]["status"] = StageStatus.SKIPPED.value
    state["cursor"] = {
        "phase": WorkflowPhase.EXPORT.value,
        "chapter_index": None,
        "segment_offset": None,
        "review_round": None,
    }


def test_factory_initializes_the_complete_json_schema() -> None:
    state = _state()

    assert set(state) == WORKFLOW_STATE_KEYS
    assert state["revision"] == 0
    assert state["status"] == WorkflowStatus.PENDING.value
    assert state["request"]["source_format"] == "epub"
    assert state["request"]["source_lang"] == "ja-jp"
    assert state["request"]["target_lang"] == "zh-cn"
    assert state["exports"]["requested_formats"] == ["epub", "pdf"]
    assert state["claimed_event_ids"] == {}
    assert json.loads(json.dumps(state)) == state


def test_factory_does_not_share_nested_mutable_defaults() -> None:
    first = _state()
    second = _state()

    first["translation"]["completed_chapters"].append(1)
    first["exports"]["outputs"]["epub"] = _source_artifact()

    assert second["translation"]["completed_chapters"] == []
    assert second["exports"]["outputs"] == {}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_format", ""),
        ("source_lang", ""),
        ("target_lang", ""),
        ("semantic_profile_hash", "bad"),
    ],
)
def test_factory_rejects_incomplete_request_identity(field: str, value: str) -> None:
    kwargs = {
        "source_artifact": _source_artifact(),
        "source_format": "epub",
        "source_lang": "ja",
        "target_lang": "zh",
        "semantic_profile_hash": PROFILE_HASH,
    }
    kwargs[field] = value

    with pytest.raises(ValueError):
        new_workflow_state(**kwargs)


@pytest.mark.parametrize(
    "source_lang",
    ["auto", "unknown", "und", "mixed", "uncertain", "多语言", "未知", "??"],
)
def test_factory_requires_a_resolved_source_language(source_lang: str) -> None:
    """持久化身份只能使用检测完成后的语言，不能写入配置哨兵值。"""
    with pytest.raises(ValueError, match="检测完成"):
        new_workflow_state(
            source_artifact=_source_artifact(),
            source_format="epub",
            source_lang=source_lang,
            target_lang="zh",
            semantic_profile_hash=PROFILE_HASH,
        )


@pytest.mark.parametrize("formats", ["epub", b"epub"])
def test_factory_rejects_a_single_string_as_the_format_sequence(formats: object) -> None:
    with pytest.raises(ValueError, match="单个字符串"):
        new_workflow_state(
            source_artifact=_source_artifact(),
            source_format="epub",
            source_lang="ja",
            target_lang="zh",
            semantic_profile_hash=PROFILE_HASH,
            requested_output_formats=formats,
        )


def test_validator_detects_request_and_workflow_identity_drift() -> None:
    state = _state()
    state["request"]["target_lang"] = "en"

    with pytest.raises(ValueError, match="workflow_id"):
        validate_workflow_state(state)


def test_validator_rejects_unknown_top_level_fields() -> None:
    state = _state()
    state["runtime_client"] = object()

    with pytest.raises(ValueError, match="extra"):
        validate_workflow_state(state)


def test_validator_requires_sorted_unique_completed_chapters() -> None:
    state = _state()
    state["translation"]["completed_chapters"] = [2, 1, 2]

    with pytest.raises(ValueError, match="升序且不重复"):
        validate_workflow_state(state)


def test_validator_requires_accounting_total_to_match_components() -> None:
    state = _state()
    state["accounting"] = {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 14,
    }

    with pytest.raises(ValueError, match="prompt_tokens"):
        validate_workflow_state(state)


def test_failed_state_requires_failure_and_only_failed_state_may_have_one() -> None:
    missing_failure = _state()
    missing_failure["status"] = WorkflowStatus.FAILED.value
    with pytest.raises(ValueError, match="必须包含 failure"):
        validate_workflow_state(missing_failure)

    unexpected_failure = _state()
    unexpected_failure["failure"] = {
        "code": "network_error",
        "message": "temporary failure",
        "retryable": True,
        "details": {},
    }
    with pytest.raises(ValueError, match="只有 failed"):
        validate_workflow_state(unexpected_failure)


def test_pending_workflow_cannot_hide_running_or_skipped_stages() -> None:
    running_stage = _state()
    running_stage["preparation"]["status"] = StageStatus.RUNNING.value
    with pytest.raises(ValueError, match="pending 工作流"):
        validate_workflow_state(running_stage)

    skipped_with_result = _state()
    skipped_with_result["understanding"] = {
        "status": StageStatus.SKIPPED.value,
        "analysis": _artifact("artifact://analysis/book.json"),
        "book_synopsis": None,
        "chapter_synopses": None,
    }
    with pytest.raises(ValueError, match="skipped understanding"):
        validate_workflow_state(skipped_with_result)


def test_pending_workflow_cannot_contain_accounting_activity() -> None:
    """pending 表示尚未执行，因此不能预先累计任何模型用量。"""
    state = _state()
    state["accounting"] = {
        "prompt_tokens": 1,
        "completion_tokens": 0,
        "total_tokens": 1,
    }

    with pytest.raises(ValueError, match="accounting 必须全部为零"):
        validate_workflow_state(state)


def test_revision_and_operation_ledger_must_advance_together() -> None:
    """CAS revision 与幂等操作数量是一份提交历史的两个投影。"""
    revision_ahead = _state()
    revision_ahead["revision"] = 1
    with pytest.raises(ValueError, match="applied_operations"):
        validate_workflow_state(revision_ahead)

    ledger_ahead = _state()
    ledger_ahead["applied_operations"] = {"prepare:start": "c" * 64}
    with pytest.raises(ValueError, match="applied_operations"):
        validate_workflow_state(ledger_ahead)


@pytest.mark.parametrize(
    "phase_builder",
    [
        "prepare",
        "understand",
        "translate_chapters",
        "translate_titles",
        "review",
        "quality",
        "export",
    ],
)
def test_each_runtime_phase_has_a_self_consistent_stage_snapshot(phase_builder: str) -> None:
    """为每个可恢复 phase 固定一份满足先决、当前和未来阶段的合法快照。"""
    state = _state()
    state["status"] = WorkflowStatus.RUNNING.value

    # prepare 内部尚未产出文档；离开该阶段后统一绑定可恢复的 document artifact。
    if phase_builder == "prepare":
        pass
    elif phase_builder == "understand":
        state["preparation"] = {
            "status": StageStatus.COMPLETED.value,
            "normalized_source": _artifact("artifact://normalized/book.epub"),
        }
        state["book"]["document_artifact"] = _artifact("artifact://documents/book.json")
        state["cursor"]["phase"] = WorkflowPhase.UNDERSTAND.value
    elif phase_builder == "translate_titles":
        _enter_title_phase(state)
    elif phase_builder == "translate_chapters":
        _enter_translation_phase(state)
    elif phase_builder == "review":
        _enter_review_phase(state)
    elif phase_builder == "quality":
        _enter_quality_phase(state)
    else:
        _enter_export_phase(state)

    validate_workflow_state(state)


@pytest.mark.parametrize(
    ("case", "mutate"),
    [
        (
            "jump-to-export",
            lambda state: state.update(
                {
                    "status": WorkflowStatus.RUNNING.value,
                    "cursor": {
                        "phase": WorkflowPhase.EXPORT.value,
                        "chapter_index": None,
                        "segment_offset": None,
                        "review_round": None,
                    },
                    "book": {
                        "document_artifact": _artifact("artifact://documents/book.json"),
                        "chapter_count": 0,
                        "source_segment_count": 0,
                    },
                }
            ),
        ),
        (
            "understand-before-prepare-completes",
            lambda state: state.update(
                {
                    "status": WorkflowStatus.RUNNING.value,
                    "cursor": {
                        "phase": WorkflowPhase.UNDERSTAND.value,
                        "chapter_index": None,
                        "segment_offset": None,
                        "review_round": None,
                    },
                    "book": {
                        "document_artifact": _artifact("artifact://documents/book.json"),
                        "chapter_count": 0,
                        "source_segment_count": 0,
                    },
                    "preparation": {
                        "status": StageStatus.RUNNING.value,
                        "normalized_source": None,
                    },
                }
            ),
        ),
        (
            "future-review-runs-during-prepare",
            lambda state: state.update(
                {
                    "status": WorkflowStatus.RUNNING.value,
                    "review": {
                        "status": StageStatus.RUNNING.value,
                        "round": 1,
                        "reviewed_content_digest": "c" * 64,
                        "latest_result": None,
                        "latest_result_round": None,
                        "chunk_results": {},
                    },
                }
            ),
        ),
        (
            "review-before-required-stages-complete",
            lambda state: state.update(
                {
                    "status": WorkflowStatus.RUNNING.value,
                    "cursor": {
                        "phase": WorkflowPhase.REVIEW.value,
                        "chapter_index": None,
                        "segment_offset": None,
                        "review_round": 1,
                    },
                    "book": {
                        "document_artifact": _artifact("artifact://documents/book.json"),
                        "chapter_count": 0,
                        "source_segment_count": 0,
                    },
                    "review": {
                        "status": StageStatus.RUNNING.value,
                        "round": 1,
                        "reviewed_content_digest": "c" * 64,
                        "latest_result": None,
                        "latest_result_round": None,
                        "chunk_results": {},
                    },
                }
            ),
        ),
    ],
)
def test_phase_matrix_rejects_missing_predecessors_and_running_future_stages(
    case: str,
    mutate,
) -> None:
    """游标不能绕过必选阶段，也不能让无关的未来阶段提前运行。"""
    state = _state()
    mutate(state)

    with pytest.raises(ValueError, match="阶段先决条件"):
        validate_workflow_state(state)


def test_completed_state_requires_complete_cursor_and_terminal_stages() -> None:
    state = _state()
    state["status"] = WorkflowStatus.COMPLETED.value

    with pytest.raises(ValueError, match="cursor.phase"):
        validate_workflow_state(state)

    state["cursor"]["phase"] = WorkflowPhase.COMPLETE.value
    state["book"]["document_artifact"] = _artifact("artifact://documents/book.json")
    with pytest.raises(ValueError, match="必选阶段 preparation"):
        validate_workflow_state(state)

    # 必选阶段标完成、可选阶段标跳过，且导出产物齐全后才是自洽完成态。
    for name in ("preparation", "translation", "glossary", "titles", "exports"):
        state[name]["status"] = StageStatus.COMPLETED.value
    for name in ("understanding", "review", "quality"):
        state[name]["status"] = StageStatus.SKIPPED.value
    state["exports"]["outputs"] = {
        "epub": _artifact("artifact://exports/book.epub"),
        "pdf": _artifact("artifact://exports/book.pdf"),
    }
    state["preparation"]["normalized_source"] = _artifact("artifact://normalized/book.epub")
    state["book"]["document_artifact"] = _artifact("artifact://documents/book.json")
    state["glossary"]["revision"] = 1
    state["glossary"]["snapshot"] = _artifact("artifact://glossary/revision-1.json")
    state["titles"] = {
        "status": StageStatus.COMPLETED.value,
        "input_digest": "d" * 64,
        "expected_title_ids": [],
        "completed_title_ids": [],
        "revision": 1,
        "snapshot": _artifact("artifact://titles/revision-1.json"),
    }
    validate_workflow_state(state)


def test_required_stages_cannot_be_skipped() -> None:
    state = _state()
    state["translation"]["status"] = StageStatus.SKIPPED.value

    with pytest.raises(ValueError, match="必选阶段 translation"):
        validate_workflow_state(state)


def test_translation_progress_must_match_book_and_artifact_boundaries() -> None:
    state = _state()
    _enter_translation_phase(state)
    state["translation"]["completed_chapters"] = [0]

    with pytest.raises(ValueError, match="chapter_artifacts"):
        validate_workflow_state(state)

    state["translation"]["chapter_artifacts"] = {"0": _artifact("artifact://chapters/0.json")}
    validate_workflow_state(state)

    state["translation"]["completed_chapters"] = [2]
    state["translation"]["chapter_artifacts"] = {"2": _artifact("artifact://chapters/2.json")}
    with pytest.raises(ValueError, match="chapter_count"):
        validate_workflow_state(state)


@pytest.mark.parametrize("chapter_key", ["01", "١", "-1", "+1"])
def test_chapter_artifact_keys_use_canonical_ascii_decimal(chapter_key: str) -> None:
    state = _state()
    state["book"] = {
        "document_artifact": _artifact("artifact://documents/book.json"),
        "chapter_count": 2,
        "source_segment_count": 20,
    }
    state["translation"]["completed_chapters"] = [1]
    state["translation"]["chapter_artifacts"] = {
        chapter_key: _artifact("artifact://chapters/1.json")
    }

    with pytest.raises(ValueError, match="ASCII"):
        validate_workflow_state(state)


def test_completed_translation_requires_every_chapter_artifact() -> None:
    state = _state()
    _enter_review_phase(state)
    state["translation"] = {
        "status": StageStatus.COMPLETED.value,
        "completed_chapters": [0],
        "chapter_artifacts": {"0": _artifact("artifact://chapters/0.json")},
    }

    with pytest.raises(ValueError, match="全部章节"):
        validate_workflow_state(state)


def test_completed_optional_stages_require_their_formal_artifacts() -> None:
    """completed 表示产物已经提交；未执行的可选阶段必须使用 skipped。"""
    understanding = _state()
    _enter_translation_phase(understanding)
    understanding["understanding"] = {
        "status": StageStatus.COMPLETED.value,
        "analysis": None,
        "book_synopsis": None,
        "chapter_synopses": None,
    }
    with pytest.raises(ValueError, match="completed understanding"):
        validate_workflow_state(understanding)

    review = _state()
    _enter_quality_phase(review)
    review["review"]["latest_result"] = None
    review["review"]["latest_result_round"] = None
    with pytest.raises(ValueError, match="completed review"):
        validate_workflow_state(review)

    quality = _state()
    _enter_export_phase(quality)
    quality["quality"]["status"] = StageStatus.COMPLETED.value
    with pytest.raises(ValueError, match="completed quality"):
        validate_workflow_state(quality)


def test_title_completion_requires_the_full_input_set_and_a_snapshot() -> None:
    """标题阶段不能靠移动 cursor 完成，必须提交每个稳定标题 ID 的证据。"""
    state = _state()
    _enter_title_phase(state)
    state["titles"]["status"] = StageStatus.COMPLETED.value
    state["titles"]["revision"] = 1
    state["titles"]["snapshot"] = _artifact("artifact://titles/revision-1.json")

    with pytest.raises(ValueError, match="完成全部 expected_title_ids"):
        validate_workflow_state(state)

    state["titles"]["completed_title_ids"] = _title_ids(2)
    state["cursor"] = {
        "phase": WorkflowPhase.REVIEW.value,
        "chapter_index": None,
        "segment_offset": None,
        "review_round": 1,
    }
    state["review"] = {
        "status": StageStatus.RUNNING.value,
        "round": 1,
        "reviewed_content_digest": "c" * 64,
        "latest_result": None,
        "latest_result_round": None,
        "chunk_results": {},
    }
    validate_workflow_state(state)


def test_review_result_is_owned_by_the_current_round() -> None:
    """孤立快照也必须能证明 latest_result 属于当前审校轮次。"""
    state = _state()
    _enter_quality_phase(state)
    state["review"]["latest_result_round"] = 2

    with pytest.raises(ValueError, match="等于当前 review.round"):
        validate_workflow_state(state)


def test_started_review_requires_a_nonzero_round_and_content_digest() -> None:
    """round=0 只代表审校尚未开始，不能与 running/completed 并存。"""
    state = _state()
    state["review"]["status"] = StageStatus.RUNNING.value

    with pytest.raises(ValueError, match="非零 round"):
        validate_workflow_state(state)


def test_glossary_revision_and_snapshot_are_an_atomic_pair() -> None:
    """运行中或失败的术语状态也不能保存无法解释的半份修订。"""
    missing_snapshot = _state()
    missing_snapshot["glossary"] = {
        "status": StageStatus.RUNNING.value,
        "revision": 1,
        "snapshot": None,
    }
    with pytest.raises(ValueError, match="同时存在"):
        validate_workflow_state(missing_snapshot)

    orphan_snapshot = _state()
    orphan_snapshot["glossary"] = {
        "status": StageStatus.RUNNING.value,
        "revision": 0,
        "snapshot": _artifact("artifact://glossary/orphan.json"),
    }
    with pytest.raises(ValueError, match="同时存在"):
        validate_workflow_state(orphan_snapshot)


def test_book_segment_counts_cannot_exist_without_a_document_artifact() -> None:
    """书籍结构必须作为同一份已发布 document artifact 的不可变元数据。"""
    state = _state()
    state["status"] = WorkflowStatus.RUNNING.value
    state["book"]["source_segment_count"] = 1

    with pytest.raises(ValueError, match="document_artifact"):
        validate_workflow_state(state)


def test_completed_empty_book_still_requires_a_document_artifact() -> None:
    """零章节只影响游标，不取消 preparation 产生正式 Document 的责任。"""
    state = _state()
    _enter_export_phase(state, chapter_count=0)
    state["status"] = WorkflowStatus.COMPLETED.value
    state["cursor"]["phase"] = WorkflowPhase.COMPLETE.value
    state["book"]["document_artifact"] = None
    state["exports"] = {
        "status": StageStatus.COMPLETED.value,
        "requested_formats": ["epub", "pdf"],
        "outputs": {
            "epub": _artifact("artifact://exports/book.epub"),
            "pdf": _artifact("artifact://exports/book.pdf"),
        },
    }

    with pytest.raises(ValueError, match="book.document_artifact"):
        validate_workflow_state(state)


def test_export_intent_is_sorted_unique_and_completed_only_with_outputs() -> None:
    state = _state()
    state["exports"]["requested_formats"] = ["pdf", "epub"]
    with pytest.raises(ValueError, match="字典序"):
        validate_workflow_state(state)

    state["exports"]["requested_formats"] = ["epub", "pdf"]
    state["exports"]["status"] = StageStatus.COMPLETED.value
    with pytest.raises(ValueError, match="全部 requested_formats"):
        validate_workflow_state(state)


def test_cursor_positions_are_bounded_and_structurally_linked() -> None:
    state = _state()
    _enter_translation_phase(state)
    state["cursor"]["chapter_index"] = 2
    with pytest.raises(ValueError, match="chapter_index"):
        validate_workflow_state(state)

    state["cursor"]["phase"] = WorkflowPhase.PREPARE.value
    state["cursor"]["chapter_index"] = None
    state["cursor"]["segment_offset"] = 3
    with pytest.raises(ValueError, match="章节或片段"):
        validate_workflow_state(state)


def test_empty_book_uses_a_translation_cursor_without_a_chapter_index() -> None:
    """零章节文档仍能进入翻译阶段，但游标不能虚构第零章。"""
    state = _state()
    _enter_translation_phase(state, chapter_count=0)
    state["cursor"]["chapter_index"] = None
    state["cursor"]["segment_offset"] = None

    validate_workflow_state(state)

    state["book"]["chapter_count"] = 1
    with pytest.raises(ValueError, match="必须包含 cursor.chapter_index"):
        validate_workflow_state(state)


def test_review_cursor_is_present_only_during_the_matching_review_round() -> None:
    state = _state()
    _enter_review_phase(state)
    state["cursor"]["review_round"] = None

    with pytest.raises(ValueError, match="review_round"):
        validate_workflow_state(state)

    state["cursor"]["review_round"] = 1
    validate_workflow_state(state)

    state["cursor"]["phase"] = WorkflowPhase.EXPORT.value
    with pytest.raises(ValueError, match="review_round"):
        validate_workflow_state(state)


def test_event_claims_reference_valid_committed_operations() -> None:
    state = _state()
    state["applied_operations"] = {"prepare:start": "c" * 64}
    state["claimed_event_ids"] = {"prepare-started": "missing operation"}

    with pytest.raises(ValueError, match="claimed_event_ids"):
        validate_workflow_state(state)

    state["claimed_event_ids"] = {"prepare-started": "prepare:missing"}
    with pytest.raises(ValueError, match="未提交"):
        validate_workflow_state(state)


def test_state_rejects_json_values_that_change_shape_on_round_trip() -> None:
    state = _state()
    state["status"] = WorkflowStatus.FAILED.value
    state["preparation"]["status"] = StageStatus.FAILED.value
    state["failure"] = {
        "code": "unstable_details",
        "message": "tuple would become a list",
        "retryable": False,
        "details": {"coordinates": (1, 2)},
    }

    with pytest.raises(ValueError, match="JSON"):
        validate_workflow_state(state)


def test_validator_does_not_mutate_the_state_it_checks() -> None:
    state = _state()
    before = copy.deepcopy(state)

    validate_workflow_state(state)

    assert state == before
