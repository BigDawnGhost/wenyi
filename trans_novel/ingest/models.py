"""核心数据结构：Document → Chapter → Segment。

Segment 是最小可对齐 / 可回填的翻译单元（通常一个段落或一个标题）。
翻译时多个 Segment 组成一个 batch 一起发给模型，模型必须返回等长的译文数组，
据此做句段对齐校验、防止整段漏译。

为零三方依赖，全部用标准库 dataclass，并提供 JSON 序列化以支持断点续跑。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional

# Segment 类型
KIND_TEXT = "text"
KIND_HEADING = "heading"


@dataclass
class Segment:
    """一个可翻译单元。"""

    index: int                      # 章内序号（从 0 起）
    source: str                     # 原文
    kind: str = KIND_TEXT           # text | heading
    target: Optional[str] = None    # 译文（翻译/润色后填入）
    anchor: Optional[str] = None    # 回填定位标记（EPUB 用占位符 id）
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Segment":
        return cls(
            index=d["index"],
            source=d["source"],
            kind=d.get("kind", KIND_TEXT),
            target=d.get("target"),
            anchor=d.get("anchor"),
            meta=d.get("meta", {}) or {},
        )


@dataclass
class Chapter:
    """一章：有序的 Segment 列表 + 回填所需的结构信息。"""

    index: int                          # 全书章序号（从 0 起）
    title: str
    segments: list[Segment] = field(default_factory=list)
    href: Optional[str] = None          # EPUB spine item 内部路径
    template: Optional[str] = None      # EPUB: 带占位符的 HTML，用于回填
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def text_segments(self) -> list[Segment]:
        """需要送翻译的非空 Segment。"""
        return [s for s in self.segments if s.source.strip()]

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "title": self.title,
            "segments": [s.to_dict() for s in self.segments],
            "href": self.href,
            "template": self.template,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Chapter":
        return cls(
            index=d["index"],
            title=d.get("title", ""),
            segments=[Segment.from_dict(s) for s in d.get("segments", [])],
            href=d.get("href"),
            template=d.get("template"),
            meta=d.get("meta", {}) or {},
        )


@dataclass
class Document:
    """整本书。"""

    title: str
    source_lang: str
    target_lang: str
    fmt: str                                # epub | text
    source_path: str
    chapters: list[Chapter] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "source_lang": self.source_lang,
            "target_lang": self.target_lang,
            "fmt": self.fmt,
            "source_path": self.source_path,
            "chapters": [c.to_dict() for c in self.chapters],
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Document":
        return cls(
            title=d.get("title", ""),
            source_lang=d["source_lang"],
            target_lang=d["target_lang"],
            fmt=d["fmt"],
            source_path=d.get("source_path", ""),
            chapters=[Chapter.from_dict(c) for c in d.get("chapters", [])],
            meta=d.get("meta", {}) or {},
        )
