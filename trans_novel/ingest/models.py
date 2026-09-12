"""核心数据结构：Document → Chapter → Segment。

Segment 是最小可对齐 / 可回填的翻译单元（通常一个段落或一个标题）。
EPUB Segment 额外保留原 XHTML 文本槽位，译文由代码按原文槽位长度比例分配，
据此回填内联结构，不要求模型处理槽位标记。

用 pydantic v2 BaseModel 做校验与序列化；to_dict()/from_dict() 包装保留，
供 runstore 断点续跑与既有调用方使用。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from trans_novel.epub.slots import (
    EpubSegmentState,
    normalized_target_text,
    validate_slot_transport,
)

_XML10_FORBIDDEN = frozenset(
    (*range(0x09), 0x0B, 0x0C, *range(0x0E, 0x20), *range(0xD800, 0xE000), 0xFFFE, 0xFFFF)
)
_XML10_DELETE = dict.fromkeys(_XML10_FORBIDDEN)


def sanitize_generated_text(value: str) -> str:
    """Remove only characters forbidden by XML 1.0."""
    return value.translate(_XML10_DELETE)


KIND_TEXT = "text"
KIND_HEADING = "heading"
CHAPTER_SEMANTICS_VERSION = "chapter_semantics_v1"


class ChapterProcessing(BaseModel):
    """持久化的全章语义处理决策。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    action: Literal["translate", "preserve"]
    review_required: bool
    reason: str
    source_sha256: str
    strategy_version: Literal["chapter_semantics_v1"]

    @field_validator("reason", "source_sha256")
    @classmethod
    def _nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value


class Segment(BaseModel):
    """一个可翻译单元。"""

    index: int  # 章内序号（从 0 起）
    source: str  # 原文
    kind: str = KIND_TEXT  # text | heading
    target: str | None = None  # 译文（翻译/润色后填入）
    anchor: str | None = None  # EPUB 资源内稳定的 Segment 定位键
    resource_href: str | None = None  # EPUB：Segment 所属的物理 XHTML 路径
    cont: bool = False  # 超长段被拆分后的续段：回填时并回上一段，不另起段落
    epub_state: EpubSegmentState | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    preserve_source: bool = False

    @model_validator(mode="after")
    def _sanitize_targets(self) -> Segment:
        self.target = sanitize_generated_text(self.target) if self.target is not None else None
        state = self.epub_state
        if state is None:
            return self
        state.slots = [
            slot.model_copy(
                update={
                    "target_value": (
                        sanitize_generated_text(slot.target_value)
                        if slot.target_value is not None
                        else None
                    )
                }
            )
            for slot in state.slots
        ]
        if self.target is not None and all(slot.target_value is not None for slot in state.slots):
            self.target = normalized_target_text(state.slots)
        return self

    def assign_translation(
        self, translation: str | list[dict[str, str]] | list[tuple[str, str]]
    ) -> None:
        """Validate then atomically assign the segment translation."""
        state = self.epub_state
        if state is None:
            if not isinstance(translation, str):
                raise ValueError("non-EPUB segment translation must be a string")
            self.target = sanitize_generated_text(translation)
            return
        parsed = validate_slot_transport(state, translation)
        new_slots = [
            slot.model_copy(update={"target_value": sanitize_generated_text(value)})
            for slot, (_slot_id, value) in zip(state.slots, parsed, strict=True)
        ]
        state.slots = new_slots
        self.target = normalized_target_text(new_slots)

    def reset_translation(self) -> None:
        state = self.epub_state
        if state is None:
            self.target = None
            return
        state.slots = [slot.model_copy(update={"target_value": None}) for slot in state.slots]
        self.target = None

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Segment:
        return cls.model_validate(d)


class Chapter(BaseModel):
    """一章：有序的 Segment 列表 + 回填所需的结构信息。"""

    index: int  # 全书章序号（从 0 起）
    title: str = ""
    segments: list[Segment] = Field(default_factory=list)
    href: str | None = None  # EPUB spine item 内部路径
    meta: dict[str, Any] = Field(default_factory=dict)
    processing: ChapterProcessing | None = None

    @model_validator(mode="after")
    def _sync_processing(self) -> Chapter:
        preserve = self.processing is not None and self.processing.action == "preserve"
        for segment in self.segments:
            segment.preserve_source = preserve
        return self

    @property
    def preserve_source(self) -> bool:
        return self.processing is not None and self.processing.action == "preserve"

    def set_processing(self, processing: ChapterProcessing) -> None:
        self.processing = processing
        self._sync_processing()

    @property
    def text_segments(self) -> list[Segment]:
        """需要送翻译的非空 Segment。"""
        return [s for s in self.segments if s.source.strip()]

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Chapter:
        return cls.model_validate(d)


def chapter_source_digest(chapter: Chapter) -> str:
    """计算会影响章节处理决策的全部源证据摘要。"""
    raw_hints = chapter.meta.get("semantic_hints", [])
    hints = raw_hints if isinstance(raw_hints, list) else []
    payload = {
        "title": chapter.title,
        "segments": [
            {"index": segment.index, "source": segment.source} for segment in chapter.segments
        ],
        "semantic_hints": sorted({hint for hint in hints if isinstance(hint, str)}),
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def preserved_toc_entry_ids(
    chapters: Sequence[Chapter], toc_entries: Sequence[Mapping[str, Any]]
) -> set[str]:
    """返回能够确定落在原文保留章节内的全部目录项 ID。"""
    ranges: list[tuple[int, int, bool]] = []
    anchors: dict[tuple[str, str], set[bool]] = {}
    selected: set[str] = set()
    position = 0
    for chapter in chapters:
        end = position + len(chapter.segments)
        ranges.append((position, end, chapter.preserve_source))
        toc_entry_id = chapter.meta.get("toc_entry_id")
        if chapter.preserve_source and isinstance(toc_entry_id, str):
            selected.add(toc_entry_id)
        for segment in chapter.segments:
            if segment.resource_href and segment.anchor:
                anchors.setdefault((segment.resource_href, segment.anchor), set()).add(
                    chapter.preserve_source
                )
        position = end

    def destination(entry: Mapping[str, Any]) -> bool | None:
        if entry.get("external"):
            return None
        boundary = entry.get("boundary_position")
        if type(boundary) is int and 0 <= boundary < position:
            return next(preserve for start, end, preserve in ranges if start <= boundary < end)
        resource = entry.get("resource_href")
        anchor = entry.get("segment_anchor")
        if isinstance(resource, str) and isinstance(anchor, str):
            decisions = anchors.get((resource, anchor), set())
            if len(decisions) == 1:
                return next(iter(decisions))
        return None

    entries = [entry for entry in toc_entries if isinstance(entry, Mapping)]
    children: dict[tuple[object, int], list[Mapping[str, Any]]] = {}
    for entry in entries:
        parent = entry.get("parent_index")
        toc_path = entry.get("toc_path")
        if type(parent) is int:
            children.setdefault((toc_path, parent), []).append(entry)

    def descendants(entry: Mapping[str, Any]) -> list[bool]:
        node_index = entry.get("node_index")
        if type(node_index) is not int:
            return []
        values: list[bool] = []
        for child in children.get((entry.get("toc_path"), node_index), []):
            resolved = destination(child)
            if resolved is not None and child.get("raw_href"):
                values.append(resolved)
            values.extend(descendants(child))
        return values

    preserved: set[str] = set()
    for entry in entries:
        entry_id = entry.get("entry_id")
        if not isinstance(entry_id, str) or entry.get("external"):
            continue
        if entry.get("raw_href"):
            resolved = destination(entry)
            if resolved is True or (
                resolved is None
                and entry_id in selected
                and "boundary_position" not in entry
                and "segment_anchor" not in entry
            ):
                preserved.add(entry_id)
            continue
        resolved_descendants = descendants(entry)
        if (resolved_descendants and all(resolved_descendants)) or (
            not resolved_descendants
            and entry_id in selected
            and "boundary_position" not in entry
            and "segment_anchor" not in entry
        ):
            preserved.add(entry_id)
    return preserved


class Document(BaseModel):
    """整本书。"""

    title: str = ""
    source_lang: str
    target_lang: str
    fmt: str  # epub | text
    source_path: str = ""
    chapters: list[Chapter] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Document:
        return cls.model_validate(d)
