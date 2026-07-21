"""Pydantic 入参出参 DTO（OpenAPI 单一事实来源 → 前端 TS 类型）。"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


# ── 项目 ─────────────────────────────────────────────────────────────────
class ProjectCreate(BaseModel):
    name: str
    source_lang: str = "auto"
    target_lang: str = "zh"
    strategy: dict[str, Any] = Field(
        default_factory=lambda: {"template": "标准翻译"})


class Project(BaseModel):
    id: str
    name: str
    title: Optional[str] = None
    fmt: Optional[str] = None
    source_lang: Optional[str] = None
    target_lang: Optional[str] = None
    status: str = "created"
    strategy: Optional[dict[str, Any]] = None
    created_at: Optional[str] = None


class ProjectDetail(Project):
    book_title: Optional[str] = None
    chapter_count: int = 0
    total_word_count: int = 0
    done_chapters: int = 0


class UploadPreview(BaseModel):
    title: str
    fmt: str
    chapter_count: int
    total_word_count: int
    source_lang: Optional[str] = None
    chapters: list[dict[str, Any]] = []


class StartTranslation(BaseModel):
    do_qa: Optional[bool] = None
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


class SegmentOut(BaseModel):
    index: int
    source: str
    target: Optional[str] = None
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
    type: str = "术语"
    gender: str = ""
    aliases: list[str] = []
    first_chapter: Optional[int] = None
    note: str = ""
    confidence: str = "medium"
    locked: bool = False
    status: str = "ok"


class TermIn(BaseModel):
    source: str
    target: str
    reading: str = ""
    type: str = "术语"
    gender: str = ""
    aliases: list[str] = []
    note: str = ""
    confidence: str = "medium"


class ConflictOut(BaseModel):
    id: int
    source: str
    existing_target: Optional[str]
    proposed_target: Optional[str]
    chapter: Optional[int]
    note: Optional[str] = None


class ResolveConflict(BaseModel):
    decision: str  # current | proposed | custom
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
class ExportRequest(BaseModel):
    format: str = "epub"            # epub | txt
    bilingual: bool = False
    order: str = "target_first"
    about_page: bool = True


class ExportOut(BaseModel):
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
class AnalysisUpdate(BaseModel):
    analysis: dict[str, Any]


# ── 通用 ─────────────────────────────────────────────────────────────────
class Message(BaseModel):
    message: str
    detail: Any = None


class JobEnqueued(BaseModel):
    job_id: str
    project_id: str
    kind: str
