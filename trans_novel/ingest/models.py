"""核心数据结构：Document → Chapter → Segment。

Segment 是最小可对齐 / 可回填的翻译单元（通常一个段落或一个标题）。
翻译时多个 Segment 组成一个 batch 一起发给模型，模型必须返回等长的译文数组，
据此做句段对齐校验、防止整段漏译。

用 pydantic v2 BaseModel 做校验与序列化；to_dict()/from_dict() 包装保留，
供 runstore 断点续跑与既有调用方使用。
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

# Segment 类型
KIND_TEXT = "text"
KIND_HEADING = "heading"


class Segment(BaseModel):
    """一个可翻译单元。"""

    index: int                              # 章内序号（从 0 起）
    source: str                             # 原文
    kind: str = KIND_TEXT                   # text | heading
    target: Optional[str] = None            # 译文（翻译/润色后填入）
    anchor: Optional[str] = None            # 回填定位标记（EPUB 用占位符 id）
    resource_href: Optional[str] = None     # EPUB: Segment 所属的物理 XHTML 路径
    cont: bool = False                      # 超长段被拆分后的续段：回填时并回上一段，不另起段落
    meta: dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化为可写入章节状态文件的普通字典。"""
        return self.model_dump()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Segment":
        """校验普通字典并恢复一个 Segment。"""
        return cls.model_validate(d)


class Chapter(BaseModel):
    """一章：有序的 Segment 列表 + 回填所需的结构信息。"""

    index: int                              # 全书章序号（从 0 起）
    title: str = ""
    segments: list[Segment] = Field(default_factory=list)
    href: Optional[str] = None              # EPUB: 逻辑章起始物理资源路径（兼容展示）
    template: Optional[str] = None          # HTML/旧 EPUB: 带占位符的回填模板
    meta: dict[str, Any] = Field(default_factory=dict)

    @property
    def text_segments(self) -> list[Segment]:
        """需要送翻译的非空 Segment。"""
        return [s for s in self.segments if s.source.strip()]

    def to_dict(self) -> dict[str, Any]:
        """序列化章节及其全部段落为普通字典。"""
        return self.model_dump()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Chapter":
        """校验普通字典并恢复一个 Chapter。"""
        return cls.model_validate(d)


class Document(BaseModel):
    """整本书。"""

    title: str = ""
    source_lang: str
    target_lang: str
    fmt: str                                # epub | text
    source_path: str = ""
    chapters: list[Chapter] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """序列化整本文档及章节数据为普通字典。"""
        return self.model_dump()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Document":
        """校验普通字典并恢复一个 Document。"""
        return cls.model_validate(d)
