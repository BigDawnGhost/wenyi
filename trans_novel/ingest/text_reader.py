"""纯文本 / Markdown 读取器。

章节识别优先级：
1. Markdown ATX 标题行（# / ##）；
2. 日文常见章节标记行（第〇章 / 第〇話 / 序章 / 終章 / プロローグ …）；
3. 都没有则整篇作为一章。

段落 = 以空行分隔的文本块；块内单换行保留。
回填时按 "标题 + 段落（空行分隔）" 重建。
"""

from __future__ import annotations

import os
import re

from .models import Chapter, Document, Segment, KIND_HEADING, KIND_TEXT

# Markdown 标题
_MD_HEADING = re.compile(r"^(#{1,3})\s+(.*\S)\s*$")
# 日文章节标记（行首）
_JA_CHAPTER = re.compile(
    r"^\s*(?:"
    r"第[0-9０-９一二三四五六七八九十百千]+[章話节節回部巻]"
    r"|序章|終章|序幕|終幕|プロローグ|エピローグ|あとがき|まえがき"
    r")"
)


def _is_chapter_heading(line: str) -> tuple[str, int] | None:
    """返回标题文本与 Markdown 级别；日文章节标记视为一级标题。"""
    m = _MD_HEADING.match(line)
    if m:
        return m.group(2).strip(), len(m.group(1))
    if _JA_CHAPTER.match(line):
        return line.strip(), 1
    return None


def _split_paragraphs(block: str) -> list[str]:
    """按空行切段。"""
    parts = re.split(r"\n\s*\n", block)
    return [p.strip("\n") for p in parts if p.strip()]


def read_text(path: str, source_lang: str, target_lang: str) -> Document:
    """读取 UTF-8 文本或 Markdown，识别章节标题并构造 Document。"""
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    lines = content.splitlines()
    book_title = os.path.splitext(os.path.basename(path))[0]

    # explicit_title 用于区分“真实标题”与首个标题前的无标题前言块；
    # level 保留 Markdown 标题层级，供 HTML / Markdown 输出使用。
    chapters_raw: list[tuple[str | None, int, list[str]]] = []
    current_title: str | None = None
    current_level = 1
    current_body: list[str] = []
    for line in lines:
        heading_info = _is_chapter_heading(line)
        if heading_info is not None:
            if current_title is not None or current_body:
                chapters_raw.append((current_title, current_level, current_body))
            current_title, current_level = heading_info
            current_body = []
        else:
            current_body.append(line)
    if current_title is not None or current_body:
        chapters_raw.append((current_title, current_level, current_body))

    chapters: list[Chapter] = []
    for ci, (explicit_title, level, body_lines) in enumerate(chapters_raw):
        title = explicit_title or book_title
        segments: list[Segment] = []
        idx = 0
        # 标题作为 heading segment（便于翻译并回填）
        if explicit_title:
            segments.append(
                Segment(index=idx, source=explicit_title, kind=KIND_HEADING)
            )
            idx += 1
        body = "\n".join(body_lines)
        for para in _split_paragraphs(body):
            segments.append(Segment(index=idx, source=para, kind=KIND_TEXT))
            idx += 1
        chapters.append(
            Chapter(
                index=ci,
                title=title,
                segments=segments,
                meta={"heading_level": level},
            )
        )

    return Document(
        title=book_title,
        source_lang=source_lang,
        target_lang=target_lang,
        fmt="text",
        source_path=os.path.abspath(path),
        chapters=chapters,
    )
