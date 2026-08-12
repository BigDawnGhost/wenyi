"""工作流请求、游标和各业务切片的局部形状校验。

这里的函数只读取一个切片，不比较旧状态，也不判断跨切片生命周期。完整快照
的一致性由 ``validation`` 负责，相邻快照的单调演进由 ``transitions`` 负责。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import Enum

from ..domain.translation_batch import (
    TRANSLATION_BATCH_ARTIFACT_MEDIA_TYPE,
    parse_translation_batch_key,
)
from ..domain.workflow import (
    StageStatus,
    WorkflowPhase,
    build_workflow_id,
    normalize_language_code,
    validate_artifact_ref,
    validate_operation_id,
    validate_sha256,
)

_CHAPTER_KEY_PATTERN = re.compile(r"^(0|[1-9][0-9]*)$")


def validate_request(request: Mapping[str, object], *, workflow_id: object) -> None:
    """校验不可变请求，并重新计算身份防止请求与目录串线。"""
    _require_exact_keys(
        request,
        {
            "source_sha256",
            "source_format",
            "source_lang",
            "target_lang",
            "semantic_profile_hash",
            "source_artifact",
        },
        field="request",
    )
    source_hash = validate_sha256(request["source_sha256"], field="request.source_sha256")
    profile_hash = validate_sha256(
        request["semantic_profile_hash"],
        field="request.semantic_profile_hash",
    )
    source_format = _require_non_empty_string(request["source_format"], field="source_format")
    source_lang = _require_non_empty_string(request["source_lang"], field="source_lang")
    target_lang = _require_non_empty_string(request["target_lang"], field="target_lang")
    if source_format != source_format.lower().lstrip("."):
        raise ValueError("request.source_format 必须是规范化的小写扩展名")
    if source_lang != normalize_language_code(source_lang, field="request.source_lang"):
        raise ValueError("request.source_lang 必须是规范化语言代码")
    if target_lang != normalize_language_code(target_lang, field="request.target_lang"):
        raise ValueError("request.target_lang 必须是规范化语言代码")

    artifact = require_mapping(request["source_artifact"], field="request.source_artifact")
    if validate_artifact_ref(artifact)["sha256"] != source_hash:
        raise ValueError("request.source_artifact 与 source_sha256 不一致")
    expected_id = build_workflow_id(source_hash, source_lang, target_lang, profile_hash)
    if workflow_id != expected_id:
        raise ValueError("workflow_id 与不可变请求身份不一致")


def validate_cursor(cursor: Mapping[str, object]) -> None:
    """校验恢复游标中的阶段与可空整数位置。"""
    _require_exact_keys(
        cursor,
        {"phase", "chapter_index", "segment_offset", "review_round"},
        field="cursor",
    )
    phase = _require_enum_value(cursor["phase"], WorkflowPhase, field="cursor.phase")
    for name in ("chapter_index", "segment_offset", "review_round"):
        value = cursor[name]
        if value is not None:
            _require_non_negative_int(value, field=f"cursor.{name}")

    # 每种 phase 只保留自身恢复所需的坐标，跨阶段必须主动清理旧位置。
    if phase == WorkflowPhase.TRANSLATE_CHAPTERS.value:
        if cursor["chapter_index"] is None and cursor["segment_offset"] is not None:
            raise ValueError(f"{phase} 空章节游标不能包含 cursor.segment_offset")
    elif cursor["chapter_index"] is not None or cursor["segment_offset"] is not None:
        raise ValueError(f"{phase} 游标不能包含章节或片段位置")

    if phase == WorkflowPhase.REVIEW.value:
        if cursor["review_round"] is None:
            raise ValueError("review 游标必须包含 cursor.review_round")
    elif cursor["review_round"] is not None:
        raise ValueError(f"{phase} 游标不能包含 cursor.review_round")


def validate_book(book: Mapping[str, object]) -> None:
    """校验书籍结构计数和可选文档引用。"""
    _require_exact_keys(
        book,
        {"document_artifact", "chapter_count", "source_segment_count"},
        field="book",
    )
    _validate_optional_artifact(book["document_artifact"], field="book.document_artifact")
    _require_non_negative_int(book["chapter_count"], field="book.chapter_count")
    _require_non_negative_int(book["source_segment_count"], field="book.source_segment_count")


def validate_preparation(stage: Mapping[str, object]) -> None:
    """校验输入准备切片。"""
    _require_exact_keys(stage, {"status", "normalized_source"}, field="preparation")
    status = _validate_stage_status(stage, field="preparation")
    _validate_optional_artifact(stage["normalized_source"], field="preparation.normalized_source")
    if status == StageStatus.PENDING.value and stage["normalized_source"] is not None:
        raise ValueError("pending preparation 不能包含 normalized_source")
    if status == StageStatus.COMPLETED.value and stage["normalized_source"] is None:
        raise ValueError("completed preparation 必须包含 normalized_source")


def validate_understanding(stage: Mapping[str, object]) -> None:
    """校验全书理解切片中的三个可选产物。"""
    artifact_fields = ("analysis", "book_synopsis", "chapter_synopses")
    _require_exact_keys(stage, {"status", *artifact_fields}, field="understanding")
    status = _validate_stage_status(stage, field="understanding")
    for name in artifact_fields:
        _validate_optional_artifact(stage[name], field=f"understanding.{name}")
    if status in {StageStatus.PENDING.value, StageStatus.SKIPPED.value} and any(
        stage[name] is not None for name in artifact_fields
    ):
        raise ValueError(f"{status} understanding 不能包含分析产物")
    if status == StageStatus.COMPLETED.value and stage["analysis"] is None:
        raise ValueError("completed understanding 必须包含 analysis")


def validate_translation(stage: Mapping[str, object]) -> None:
    """校验批次与完成章节账本均使用唯一、可排序的稳定键。"""
    _require_exact_keys(
        stage,
        {"status", "batch_artifacts", "completed_chapters", "chapter_artifacts"},
        field="translation",
    )
    status = _validate_stage_status(stage, field="translation")
    completed = stage["completed_chapters"]
    if not isinstance(completed, list):
        raise ValueError("translation.completed_chapters 必须是列表")
    for chapter in completed:
        _require_non_negative_int(chapter, field="translation.completed_chapters[]")
    if completed != sorted(set(completed)):
        raise ValueError("translation.completed_chapters 必须升序且不重复")
    _validate_artifact_mapping(
        stage["chapter_artifacts"],
        field="translation.chapter_artifacts",
        numeric_keys=True,
    )
    batches = _validate_artifact_mapping(
        stage["batch_artifacts"],
        field="translation.batch_artifacts",
    )
    for key, artifact in batches.items():
        parse_translation_batch_key(key)
        normalized = validate_artifact_ref(
            require_mapping(artifact, field=f"translation.batch_artifacts.{key}")
        )
        if normalized["media_type"] != TRANSLATION_BATCH_ARTIFACT_MEDIA_TYPE:
            raise ValueError("translation.batch_artifacts 必须引用 translation batch 专用媒体类型")
    if status == StageStatus.PENDING.value and (batches or completed or stage["chapter_artifacts"]):
        raise ValueError("pending translation 不能包含完成章节或产物")


def validate_glossary(stage: Mapping[str, object]) -> None:
    """校验术语状态和单调修订号。"""
    _require_exact_keys(stage, {"status", "revision", "snapshot"}, field="glossary")
    status = _validate_stage_status(stage, field="glossary")
    revision = _require_non_negative_int(stage["revision"], field="glossary.revision")
    _validate_optional_artifact(stage["snapshot"], field="glossary.snapshot")
    if (revision == 0) != (stage["snapshot"] is None):
        raise ValueError("glossary.revision 与 snapshot 必须同时存在或同时为空")
    if status == StageStatus.PENDING.value and revision != 0:
        raise ValueError("pending glossary 必须保持 revision=0 且没有 snapshot")
    if status == StageStatus.COMPLETED.value and (revision == 0 or stage["snapshot"] is None):
        raise ValueError("completed glossary 必须包含非零 revision 和 snapshot")


def validate_titles(stage: Mapping[str, object]) -> None:
    """校验标题输入集合、可恢复完成集合和版本化快照。"""
    _require_exact_keys(
        stage,
        {
            "status",
            "input_digest",
            "expected_title_ids",
            "completed_title_ids",
            "revision",
            "snapshot",
        },
        field="titles",
    )
    status = _validate_stage_status(stage, field="titles")
    input_digest = stage["input_digest"]
    if input_digest is not None:
        validate_sha256(input_digest, field="titles.input_digest")
    expected = _validate_sorted_unique_strings(
        stage["expected_title_ids"],
        field="titles.expected_title_ids",
    )
    completed = _validate_sorted_unique_strings(
        stage["completed_title_ids"],
        field="titles.completed_title_ids",
    )
    if not set(completed) <= set(expected):
        raise ValueError("titles.completed_title_ids 必须是 expected_title_ids 的子集")

    revision = _require_non_negative_int(stage["revision"], field="titles.revision")
    _validate_optional_artifact(stage["snapshot"], field="titles.snapshot")
    if (revision == 0) != (stage["snapshot"] is None):
        raise ValueError("titles.revision 与 snapshot 必须同时存在或同时为空")

    # pending/skipped 不得提前绑定输入；开始后必须始终保留稳定输入摘要。
    if status in {StageStatus.PENDING.value, StageStatus.SKIPPED.value}:
        if input_digest is not None or expected or completed or revision != 0:
            raise ValueError(f"{status} titles 不能包含输入、进度或快照")
        return
    if input_digest is None:
        raise ValueError(f"{status} titles 必须包含 input_digest")
    if completed and revision == 0:
        raise ValueError("titles 有完成项时必须包含版本化 snapshot")
    if status == StageStatus.COMPLETED.value and (completed != expected or revision == 0):
        raise ValueError("completed titles 必须完成全部 expected_title_ids 并提交 snapshot")


def validate_review(stage: Mapping[str, object]) -> None:
    """校验审校轮次、内容摘要和并行块映射。"""
    _require_exact_keys(
        stage,
        {
            "status",
            "round",
            "reviewed_content_digest",
            "latest_result",
            "latest_result_round",
            "chunk_results",
        },
        field="review",
    )
    status = _validate_stage_status(stage, field="review")
    round_number = _require_non_negative_int(stage["round"], field="review.round")
    digest = stage["reviewed_content_digest"]
    if digest is not None:
        validate_sha256(digest, field="review.reviewed_content_digest")
    _validate_optional_artifact(stage["latest_result"], field="review.latest_result")
    result_round = stage["latest_result_round"]
    if result_round is not None:
        _require_non_negative_int(result_round, field="review.latest_result_round")
    if (stage["latest_result"] is None) != (result_round is None):
        raise ValueError("review.latest_result 与 latest_result_round 必须同时存在或同时为空")
    if result_round is not None and result_round != round_number:
        raise ValueError("review.latest_result_round 必须等于当前 review.round")
    _validate_artifact_mapping(stage["chunk_results"], field="review.chunk_results")
    if status in {StageStatus.PENDING.value, StageStatus.SKIPPED.value} and (
        round_number != 0
        or digest is not None
        or stage["latest_result"] is not None
        or result_round is not None
        or stage["chunk_results"]
    ):
        raise ValueError(f"{status} review 不能包含审校轮次或结果")
    if status in {
        StageStatus.RUNNING.value,
        StageStatus.FAILED.value,
        StageStatus.COMPLETED.value,
    } and (round_number == 0 or digest is None):
        raise ValueError(f"{status} review 必须包含非零 round 和 reviewed_content_digest")
    if status == StageStatus.COMPLETED.value and stage["latest_result"] is None:
        raise ValueError("completed review 必须包含 latest_result")


def validate_quality(stage: Mapping[str, object]) -> None:
    """校验质量报告切片。"""
    _require_exact_keys(stage, {"status", "report"}, field="quality")
    status = _validate_stage_status(stage, field="quality")
    _validate_optional_artifact(stage["report"], field="quality.report")
    if (
        status in {StageStatus.PENDING.value, StageStatus.SKIPPED.value}
        and stage["report"] is not None
    ):
        raise ValueError(f"{status} quality 不能包含 report")
    if status == StageStatus.COMPLETED.value and stage["report"] is None:
        raise ValueError("completed quality 必须包含 report")


def validate_exports(stage: Mapping[str, object]) -> None:
    """校验当前执行的导出意图，并保证完成态已有全部请求产物。"""
    _require_exact_keys(
        stage,
        {"status", "requested_formats", "outputs"},
        field="exports",
    )
    status = _validate_stage_status(stage, field="exports")
    formats = stage["requested_formats"]
    if not isinstance(formats, list):
        raise ValueError("exports.requested_formats 必须是列表")
    normalized_formats = [
        _require_non_empty_string(value, field="exports.requested_formats[]") for value in formats
    ]
    if normalized_formats != sorted(set(normalized_formats)):
        raise ValueError("exports.requested_formats 必须按字典序排列且不重复")
    if any(value != value.lower().lstrip(".") for value in normalized_formats):
        raise ValueError("exports.requested_formats 必须是规范化的小写格式名")

    outputs = _validate_artifact_mapping(stage["outputs"], field="exports.outputs")
    output_formats = set(outputs)
    requested_formats = set(normalized_formats)
    if not output_formats <= requested_formats:
        raise ValueError("exports.outputs 不能包含未请求的格式")
    if status == StageStatus.SKIPPED.value and requested_formats:
        raise ValueError("存在 requested_formats 时 exports 不能标记为 skipped")
    if status in {StageStatus.PENDING.value, StageStatus.SKIPPED.value} and output_formats:
        raise ValueError(f"{status} exports 不能包含输出产物")
    if status == StageStatus.COMPLETED.value and output_formats != requested_formats:
        raise ValueError("completed exports 必须包含全部 requested_formats 的产物")


def validate_accounting(accounting: Mapping[str, object]) -> None:
    """校验 token 计数并保证总数与分项一致。"""
    _require_exact_keys(
        accounting,
        {"prompt_tokens", "completion_tokens", "total_tokens"},
        field="accounting",
    )
    prompt = _require_non_negative_int(
        accounting["prompt_tokens"],
        field="accounting.prompt_tokens",
    )
    completion = _require_non_negative_int(
        accounting["completion_tokens"],
        field="accounting.completion_tokens",
    )
    total = _require_non_negative_int(accounting["total_tokens"], field="accounting.total_tokens")
    if total != prompt + completion:
        raise ValueError("accounting.total_tokens 必须等于 prompt_tokens + completion_tokens")


def validate_applied_operations(operations: Mapping[str, object]) -> None:
    """校验操作幂等账本中的 ID 和规范补丁指纹。"""
    for operation_id, fingerprint in operations.items():
        validate_operation_id(operation_id, field="applied_operations key")
        validate_sha256(fingerprint, field=f"applied_operations.{operation_id}")


def validate_claimed_event_ids(
    claims: Mapping[str, object],
    *,
    operations: Mapping[str, object],
) -> None:
    """校验 event_id 的唯一所有者，并拒绝没有已提交操作的孤儿认领。"""
    for event_id, operation_id in claims.items():
        validate_operation_id(event_id, field="claimed_event_ids key")
        owner = validate_operation_id(operation_id, field=f"claimed_event_ids.{event_id}")
        if owner not in operations:
            raise ValueError(f"claimed_event_ids.{event_id} 指向未提交的 operation_id")


def require_mapping(value: object, *, field: str) -> Mapping[str, object]:
    """把动态输入收窄为字符串键映射。"""
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{field} 必须是字符串键映射")
    return value


def _validate_stage_status(stage: Mapping[str, object], *, field: str) -> str:
    """校验所有阶段切片共用的 status 字段。"""
    return _require_enum_value(stage["status"], StageStatus, field=f"{field}.status")


def _validate_optional_artifact(value: object, *, field: str) -> None:
    """校验可空产物引用。"""
    if value is None:
        return
    validate_artifact_ref(require_mapping(value, field=field))


def _validate_artifact_mapping(
    value: object,
    *,
    field: str,
    numeric_keys: bool = False,
) -> Mapping[str, object]:
    """校验稳定字符串 ID 到产物引用的映射。"""
    mapping = require_mapping(value, field=field)
    for key, artifact in mapping.items():
        _require_non_empty_string(key, field=f"{field} key")
        if numeric_keys and _CHAPTER_KEY_PATTERN.fullmatch(key) is None:
            raise ValueError(f"{field} 的键必须是规范 ASCII 非负章节号")
        validate_artifact_ref(require_mapping(artifact, field=f"{field}.{key}"))
    return mapping


def _validate_sorted_unique_strings(value: object, *, field: str) -> list[str]:
    """校验用于恢复进度的稳定字符串 ID 列表。"""
    if not isinstance(value, list):
        raise ValueError(f"{field} 必须是列表")
    items = [_require_non_empty_string(item, field=f"{field}[]") for item in value]
    if items != sorted(set(items)):
        raise ValueError(f"{field} 必须按字典序排列且不重复")
    return items


def _require_exact_keys(
    value: Mapping[str, object],
    expected: frozenset[str] | set[str],
    *,
    field: str,
) -> None:
    """拒绝字段遗漏和静默扩展，令 schema 变更必须显式版本化。"""
    actual = set(value)
    if actual == set(expected):
        return
    missing = sorted(set(expected) - actual)
    extra = sorted(actual - set(expected))
    raise ValueError(f"{field} 字段不匹配：missing={missing}, extra={extra}")


def _require_non_negative_int(value: object, *, field: str) -> int:
    """拒绝 bool 冒充整数，并返回非负计数。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} 必须是非负整数")
    return value


def _require_non_empty_string(value: object, *, field: str) -> str:
    """返回非空字符串，不自动修改已持久化内容。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 不能为空")
    return value


def _require_enum_value(value: object, enum_type: type[Enum], *, field: str) -> str:
    """校验字符串属于给定字符串枚举。"""
    allowed_values = {item.value for item in enum_type}
    if not isinstance(value, str) or value not in allowed_values:
        allowed = ", ".join(str(item.value) for item in enum_type)
        raise ValueError(f"{field} 必须是：{allowed}")
    return value


__all__ = [
    "require_mapping",
    "validate_accounting",
    "validate_applied_operations",
    "validate_book",
    "validate_claimed_event_ids",
    "validate_cursor",
    "validate_exports",
    "validate_glossary",
    "validate_preparation",
    "validate_quality",
    "validate_request",
    "validate_review",
    "validate_titles",
    "validate_translation",
    "validate_understanding",
]
