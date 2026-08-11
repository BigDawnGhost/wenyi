"""工作流初始状态工厂。

工厂只做输入规范化和独立默认值装配；所有完整状态不变量统一交给
``validation`` 模块，避免创建路径和恢复路径出现两套规则。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ..domain.workflow import (
    StageStatus,
    WorkflowPhase,
    WorkflowStatus,
    build_workflow_id,
    normalize_language_code,
    validate_artifact_ref,
    validate_sha256,
)
from .state import WORKFLOW_SCHEMA_VERSION, WorkflowState
from .validation import validate_workflow_state


def new_workflow_state(
    *,
    source_artifact: Mapping[str, object],
    source_format: str,
    source_lang: str,
    target_lang: str,
    semantic_profile_hash: str,
    requested_output_formats: Sequence[str] = (),
) -> WorkflowState:
    """从已解析的稳定请求身份创建没有共享可变默认值的初始状态。"""
    if isinstance(requested_output_formats, (str, bytes)):
        raise ValueError("requested_output_formats 必须是字符串序列，不能是单个字符串")

    # 不可变身份输入先统一规范化；任何空值都在创建状态前失败。
    artifact = validate_artifact_ref(source_artifact)
    normalized_source_format = (
        _require_utf8_text(
            source_format,
            field="source_format",
        )
        .lower()
        .lstrip(".")
    )
    normalized_source_lang = normalize_language_code(source_lang, field="source_lang")
    normalized_target_lang = normalize_language_code(target_lang, field="target_lang")
    profile_hash = validate_sha256(
        semantic_profile_hash,
        field="semantic_profile_hash",
    )
    if not normalized_source_format:
        raise ValueError("source_format 不能为空")
    if not normalized_source_lang:
        raise ValueError("source_lang 不能为空")
    if not normalized_target_lang:
        raise ValueError("target_lang 不能为空")

    # 输出意图不参与翻译身份；当前执行关闭前可追加，关闭后由独立 export job 承接。
    output_formats: set[str] = set()
    for value in requested_output_formats:
        normalized = (
            _require_utf8_text(
                value,
                field="requested_output_formats[]",
            )
            .lower()
            .lstrip(".")
        )
        output_formats.add(normalized)

    # 每个嵌套容器都在本次调用中创建，防止不同工作流共享可变默认值。
    state: WorkflowState = {
        "schema_version": WORKFLOW_SCHEMA_VERSION,
        "revision": 0,
        "workflow_id": build_workflow_id(
            artifact["sha256"],
            normalized_source_lang,
            normalized_target_lang,
            profile_hash,
        ),
        "status": WorkflowStatus.PENDING.value,
        "request": {
            "source_sha256": artifact["sha256"],
            "source_format": normalized_source_format,
            "source_lang": normalized_source_lang,
            "target_lang": normalized_target_lang,
            "semantic_profile_hash": profile_hash,
            "source_artifact": artifact,
        },
        "cursor": {
            "phase": WorkflowPhase.PREPARE.value,
            "chapter_index": None,
            "segment_offset": None,
            "review_round": None,
        },
        "book": {
            "document_artifact": None,
            "chapter_count": 0,
            "source_segment_count": 0,
        },
        "preparation": {
            "status": StageStatus.PENDING.value,
            "normalized_source": None,
        },
        "understanding": {
            "status": StageStatus.PENDING.value,
            "analysis": None,
            "book_synopsis": None,
            "chapter_synopses": None,
        },
        "translation": {
            "status": StageStatus.PENDING.value,
            "completed_chapters": [],
            "chapter_artifacts": {},
        },
        "glossary": {
            "status": StageStatus.PENDING.value,
            "revision": 0,
            "snapshot": None,
        },
        "titles": {
            "status": StageStatus.PENDING.value,
            "input_digest": None,
            "expected_title_ids": [],
            "completed_title_ids": [],
            "revision": 0,
            "snapshot": None,
        },
        "review": {
            "status": StageStatus.PENDING.value,
            "round": 0,
            "reviewed_content_digest": None,
            "latest_result": None,
            "latest_result_round": None,
            "chunk_results": {},
        },
        "quality": {
            "status": StageStatus.PENDING.value,
            "report": None,
        },
        "exports": {
            "status": StageStatus.PENDING.value,
            "requested_formats": sorted(output_formats),
            "outputs": {},
        },
        "accounting": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        },
        "failure": None,
        "applied_operations": {},
        "claimed_event_ids": {},
    }
    validate_workflow_state(state)
    return state


def _require_utf8_text(value: object, *, field: str) -> str:
    """规范化前先拒绝空值、错误运行时类型和无法持久化的字符串。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 不能为空且必须是字符串")
    stripped = value.strip()
    try:
        stripped.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{field} 必须可写入 UTF-8") from error
    return stripped


__all__ = ["new_workflow_state"]
