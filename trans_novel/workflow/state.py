"""Graph-ready 工作流状态的纯类型定义。

本模块不创建、不校验也不持久化状态，只固定各切片的名称和字段。大型正文、
分析和导出结果必须使用 ``ArtifactRef``，运行时对象不得进入这些结构。
"""

from __future__ import annotations

from typing import TypedDict

from ..domain.workflow import ArtifactRef, FailureInfo

WORKFLOW_SCHEMA_VERSION = 1

# 阶段集合是验证器与 reducer 共享的唯一真相，避免两处规则随演进分叉。
WORKFLOW_STAGE_NAMES = (
    "preparation",
    "understanding",
    "translation",
    "glossary",
    "titles",
    "review",
    "quality",
    "exports",
)
REQUIRED_STAGE_NAMES = ("preparation", "translation", "glossary", "titles")
OPTIONAL_STAGE_NAMES = ("understanding", "review", "quality")


class WorkflowRequestState(TypedDict):
    """创建工作流后不可修改的语义请求身份。"""

    source_sha256: str
    source_format: str
    source_lang: str
    target_lang: str
    semantic_profile_hash: str
    source_artifact: ArtifactRef


class WorkflowCursorState(TypedDict):
    """恢复执行所需的最小位置，不保存运行时对象。"""

    phase: str
    chapter_index: int | None
    segment_offset: int | None
    review_round: int | None


class BookState(TypedDict):
    """书籍结构摘要和规范化文档引用。"""

    document_artifact: ArtifactRef | None
    chapter_count: int
    source_segment_count: int


class PreparationState(TypedDict):
    """输入规范化阶段状态。"""

    status: str
    normalized_source: ArtifactRef | None


class UnderstandingState(TypedDict):
    """全书风格、概览和逐章梗概产物。"""

    status: str
    analysis: ArtifactRef | None
    book_synopsis: ArtifactRef | None
    chapter_synopses: ArtifactRef | None


class TranslationState(TypedDict):
    """顺序翻译游标之外的章节完成集合。"""

    status: str
    completed_chapters: list[int]
    chapter_artifacts: dict[str, ArtifactRef]


class GlossaryState(TypedDict):
    """术语库的逻辑修订号和只读快照引用。"""

    status: str
    revision: int
    snapshot: ArtifactRef | None


class TitleTranslationState(TypedDict):
    """正文完成后的标题输入身份、批次进度和最新不可变快照。"""

    status: str
    input_digest: str | None
    expected_title_ids: list[str]
    completed_title_ids: list[str]
    revision: int
    snapshot: ArtifactRef | None


class ReviewState(TypedDict):
    """审校轮次、输入摘要和可并行块结果。"""

    status: str
    round: int
    reviewed_content_digest: str | None
    latest_result: ArtifactRef | None
    latest_result_round: int | None
    chunk_results: dict[str, ArtifactRef]


class QualityState(TypedDict):
    """最终质量报告产物；不恢复已删除的旧 QA 命令。"""

    status: str
    report: ArtifactRef | None


class ExportState(TypedDict):
    """当前执行实例的导出意图，以及按格式索引的不可变导出产物。"""

    status: str
    requested_formats: list[str]
    outputs: dict[str, ArtifactRef]


class AccountingState(TypedDict):
    """工作流级累计 token 计数。"""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class WorkflowState(TypedDict):
    """未来图节点与当前顺序执行器共用的完整状态。"""

    schema_version: int
    revision: int
    workflow_id: str
    status: str
    request: WorkflowRequestState
    cursor: WorkflowCursorState
    book: BookState
    preparation: PreparationState
    understanding: UnderstandingState
    translation: TranslationState
    glossary: GlossaryState
    titles: TitleTranslationState
    review: ReviewState
    quality: QualityState
    exports: ExportState
    accounting: AccountingState
    failure: FailureInfo | None
    applied_operations: dict[str, str]
    # 该投影只负责事件所有权冲突；完整待投递载荷由 repository outbox 保存。
    claimed_event_ids: dict[str, str]


WORKFLOW_STATE_KEYS = frozenset(
    {
        "schema_version",
        "revision",
        "workflow_id",
        "status",
        "request",
        "cursor",
        "book",
        "preparation",
        "understanding",
        "translation",
        "glossary",
        "titles",
        "review",
        "quality",
        "exports",
        "accounting",
        "failure",
        "applied_operations",
        "claimed_event_ids",
    }
)

# 普通节点只能替换业务切片；身份、版本和两个幂等账本由 reducer 独占维护。
RESERVED_UPDATE_KEYS = frozenset(
    {
        "schema_version",
        "revision",
        "workflow_id",
        "request",
        "applied_operations",
        "claimed_event_ids",
    }
)
ALLOWED_UPDATE_KEYS = WORKFLOW_STATE_KEYS - RESERVED_UPDATE_KEYS


__all__ = [
    "ALLOWED_UPDATE_KEYS",
    "AccountingState",
    "BookState",
    "ExportState",
    "GlossaryState",
    "PreparationState",
    "QualityState",
    "OPTIONAL_STAGE_NAMES",
    "REQUIRED_STAGE_NAMES",
    "RESERVED_UPDATE_KEYS",
    "ReviewState",
    "TitleTranslationState",
    "TranslationState",
    "UnderstandingState",
    "WORKFLOW_SCHEMA_VERSION",
    "WORKFLOW_STAGE_NAMES",
    "WORKFLOW_STATE_KEYS",
    "WorkflowCursorState",
    "WorkflowRequestState",
    "WorkflowState",
]
