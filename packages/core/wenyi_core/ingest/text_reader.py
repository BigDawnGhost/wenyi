"""Plain-text and Markdown reader.
Recognize Markdown ATX headings first, then common Japanese chapter/prologue/epilogue
labels. Without headings, use one chapter. Blank lines separate paragraphs while single
internal newlines remain. Reconstruct output as headings and blank-line-separated
paragraphs.
"""

from __future__ import annotations

import os
import re

from .models import KIND_HEADING, KIND_TEXT, Chapter, Document, Segment

# Markdown headings.
_MD_HEADING = re.compile(r"^(#{1,3})\s+(.*\S)\s*$")
# Japanese chapter labels at the start of a line.
_JA_CHAPTER = re.compile(
    r"^\s*(?:"
    r"第[0-9０-９一二三四五六七八九十百千]+[章話节節回部巻]"
    r"|序章|終章|序幕|終幕|プロローグ|エピローグ|あとがき|まえがき"
    r")"
)


def _is_chapter_heading(line: str) -> tuple[str, int] | None:
    """Return heading text and Markdown level; treat Japanese chapter markers as level one."""
    m = _MD_HEADING.match(line)
    if m:
        return m.group(2).strip(), len(m.group(1))
    if _JA_CHAPTER.match(line):
        return line.strip(), 1
    return None


def _split_paragraphs(block: str) -> list[str]:
    """Split paragraphs at blank lines."""
    parts = re.split(r"\n\s*\n", block)
    return [p.strip("\n") for p in parts if p.strip()]


def read_text(path: str, source_lang: str, target_lang: str) -> Document:
    """Read UTF-8 text or Markdown, identify headings and construct a Document."""
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    lines = content.splitlines()
    book_title = os.path.splitext(os.path.basename(path))[0]

    # explicit_title distinguishes actual headings from untitled content before the first heading.
    # level preserves Markdown heading hierarchy for HTML/Markdown output.
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
        # Keep titles as heading segments so they can be translated and backfilled.
        if explicit_title:
            segments.append(Segment(index=idx, source=explicit_title, kind=KIND_HEADING))
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
