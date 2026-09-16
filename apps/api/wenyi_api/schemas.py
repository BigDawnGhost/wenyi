"""Pydantic 入参出参 DTO（OpenAPI 单一事实来源 → 前端 TS 类型）。"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator
from wenyi_core.i18n.languages import require_language


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ── 项目 ─────────────────────────────────────────────────────────────────
class ProjectCreate(RequestModel):
    name: str = Field(min_length=1, max_length=240)
    source_lang: str = "auto"
    target_lang: str = "zh"
    strategy: dict[str, Any] = Field(default_factory=lambda: {"template": "标准翻译"})

    @field_validator("source_lang")
    @classmethod
    def validate_source(cls, value: str) -> str:
        return require_language(value, allow_auto=True)

    @field_validator("target_lang")
    @classmethod
    def validate_target(cls, value: str) -> str:
        return require_language(value)


class Project(BaseModel):
    id: str
    name: str
    title: Optional[str] = None
    fmt: Optional[str] = None
    source_lang: Optional[str] = None
    target_lang: Optional[str] = None
    status: str = "created"
    error: str | None = None
    initialized: bool = False
    strategy: Optional[dict[str, Any]] = None
    created_at: Optional[str] = None


class ProjectDetail(Project):
    book_title: Optional[str] = None
    chapter_count: int = 0
    total_word_count: int = 0
    done_chapters: int = 0
    source_meta: dict[str, Any] = Field(default_factory=dict)


class PreviewChapter(BaseModel):
    index: int
    title: str
    word_count: int


class UploadPreview(BaseModel):
    title: str
    fmt: str
    chapter_count: int
    total_word_count: int
    source_lang: Optional[str] = None
    chapters: list[PreviewChapter] = Field(default_factory=list)


class StartTranslation(RequestModel):
    strategy: Optional[dict[str, Any]] = None


# ── 章节 / 段落 ──────────────────────────────────────────────────────────
class ChapterSummary(BaseModel):
    index: int
    title: str = ""
    title_translated: Optional[str] = None
    status: str = "pending"
    word_count: int = 0
    target_word_count: int = 0
    review_issue_count: int = 0
    review_status: str = "pending"


class SegmentOut(BaseModel):
    index: int
    source: str
    target: Optional[str] = None
    target_before_polish: str | None = None
    anchor: str | None = None
    kind: str = "text"


class ChapterSegments(BaseModel):
    index: int
    title: str = ""
    title_translated: Optional[str] = None
    segments: list[SegmentOut]
    review_issues: list[dict[str, Any]] = []


# ── 术语 ─────────────────────────────────────────────────────────────────
class TermOut(BaseModel):
    source: str
    target: str
    reading: str = ""
    type: str = "term"
    gender: str = ""
    aliases: list[str] = []
    first_chapter: Optional[int] = None
    note: str = ""
    status: str = "ok"


class TermIn(RequestModel):
    source: str
    target: str
    reading: str = ""
    type: str = "term"
    gender: str = ""
    aliases: list[str] = []
    note: str = ""


class ConflictOut(BaseModel):
    id: int
    source: str
    existing_target: Optional[str]
    proposed_target: Optional[str]
    chapter: Optional[int]
    note: Optional[str] = None


class ResolveConflict(RequestModel):
    decision: Literal["current", "proposed", "custom"]
    target: Optional[str] = None


# ── 策略 ─────────────────────────────────────────────────────────────────
class StepDef(BaseModel):
    id: str
    name: str
    category: str
    always_on: bool = False
    locked: bool = False
    group: Optional[str] = None
    depends_on: list[str] = []
    description: Optional[str] = None
    output: Optional[str] = None
    options: Optional[dict[str, Any]] = None


class StrategyTemplateOut(BaseModel):
    name: str
    description: str = ""
    time_factor: int = 1
    recommended: bool = False
    steps: dict[str, Any]


# ── 导出 ─────────────────────────────────────────────────────────────────
class ExportRequest(RequestModel):
    format: Literal["epub", "txt", "html", "markdown", "pdf", "docx", "srt"] | None = None
    bilingual: bool = False
    order: Literal["target_first", "source_first"] = "target_first"
    about_page: bool = True
    preserve_source_style: bool = False
    punctuation_normalize: bool | None = None
    pdf_engine: Literal["weasyprint", "fpdf2"] = "weasyprint"


class ExportOut(BaseModel):
    options: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    id: int
    project_id: str
    format: str
    status: str
    path: Optional[str] = None
    size: Optional[int] = None
    created_at: Optional[str] = None


# ── 事件 ─────────────────────────────────────────────────────────────────
class EventOut(BaseModel):
    id: int
    type: str
    payload: dict[str, Any] = {}
    created_at: Optional[str] = None


# ── 风格 / 概要（编辑）──────────────────────────────────────────────────
class AnalysisUpdate(RequestModel):
    analysis: dict[str, Any]


# ── 通用 ─────────────────────────────────────────────────────────────────
class Message(BaseModel):
    message: str
    detail: Any = None


class JobEnqueued(BaseModel):
    job_id: str
    project_id: str
    kind: str


class AssembleEnqueued(JobEnqueued):
    export_id: int


class GlossaryImport(RequestModel):
    terms: list[TermIn]


class DigestUpdate(RequestModel):
    digest: str


class TargetEdit(RequestModel):
    target: str


class ReviewRunRequest(RequestModel):
    autofix: bool | None = None


class ReviewRun(BaseModel):
    id: str
    review_id: str
    status: str
    created_at: str | None = None
    issues: list[dict[str, Any]] = Field(default_factory=list)
    changes: list[dict[str, Any]] = Field(default_factory=list)
    autofix: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)


class SubtitleCue(BaseModel):
    id: str
    index: int
    timestamp: str
    start: str
    end: str
    source: str
    target: str | None = None
    status: str = "pending"


class SubtitleResult(BaseModel):
    cues: list[SubtitleCue]
    completed: int
    total: int


class LanguageOption(BaseModel):
    code: str
    name: str


class PDFCapabilities(BaseModel):
    backends: list[str]
    engines: list[str]
    export_backends: list[str]


class Capabilities(BaseModel):
    languages: list[LanguageOption]
    input_formats: list[str]
    output_formats: list[str]
    pdf: PDFCapabilities
    providers: list[str]
    operations: list[dict[str, Any]]


class ConfigInput(RequestModel):
    yaml: str = Field(max_length=2_000_000)


class ProjectConfigOut(BaseModel):
    yaml: str
    effective: dict[str, Any]
    routes: list[dict[str, Any]]
    editable: bool


class ModelCheckRequest(RequestModel):
    workflow: Literal["prepare", "translate", "review", "srt"] = "translate"


class ModelCheckResult(BaseModel):
    valid: bool
    operations: list[str]


class ModelMessage(RequestModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ModelCompareRequest(RequestModel):
    operation: str
    models: list[str] = Field(min_length=1, max_length=10)
    messages: list[ModelMessage] = Field(min_length=1)
    json_mode: bool = False


class ProjectStats(BaseModel):
    usage: dict[str, Any] = Field(default_factory=dict)
    timing: dict[str, Any] = Field(default_factory=dict)


class ChapterDigest(BaseModel):
    index: int
    title: str
    digest: str


class AnalysisOut(BaseModel):
    analysis: dict[str, Any]
    chapter_digests: list[ChapterDigest]


class WorkflowStage(BaseModel):
    id: str
    label: str
    enabled: bool = True


class WorkflowOut(BaseModel):
    source: str
    kind: str
    status: str
    run_id: str | None = None
    stages: list[WorkflowStage]
    progress: dict | None = None


class RetranslationInput(RequestModel):
    segment_indices: list[int] = Field(min_length=1, max_length=50)

    @field_validator("segment_indices", mode="before")
    @classmethod
    def valid_indices(cls, value):
        if not isinstance(value, list) or any(type(i) is not int or i < 0 for i in value):
            raise ValueError("段落编号必须为非负整数")
        if len(set(value)) != len(value):
            raise ValueError("段落编号不能重复")
        return value


class RetranslationOut(BaseModel):
    id: str
    chapter_index: int
    segment_indices: list[int]
    status: Literal["queued", "running", "done", "error", "conflict"]
    applied: list[int] = Field(default_factory=list)
    conflicts: list[int] = Field(default_factory=list)
    error: str | None = None
    created_at: str
