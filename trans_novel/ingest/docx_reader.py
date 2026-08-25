"""Word .docx 读取：段落 + 简易表格 → Document。

标题样式（Heading / 标题 / outlineLvl）映射为 heading segments，并按一级标题切章。
表格按文档顺序抽出，单元格合并为一段，meta 记录行列供写出时重建。
"""

from __future__ import annotations

import os
import re
from typing import Any

from docx import Document as open_docx
from docx.document import Document as DocxDocument
from docx.oxml.ns import qn
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph as DocxParagraph

from .models import KIND_HEADING, KIND_TEXT, Chapter, Document, Segment

_HEADING_NAME = re.compile(
    r"^(?:Heading|标题|標題)\s*([1-9])\s*$",
    re.IGNORECASE,
)


def _iter_body_blocks(doc: DocxDocument):
    """按 body 顺序产出段落与表格。"""
    for child in doc.element.body:
        if child.tag == qn("w:p"):
            yield DocxParagraph(child, doc)
        elif child.tag == qn("w:tbl"):
            yield DocxTable(child, doc)


def _outline_level(paragraph: DocxParagraph) -> int | None:
    """从段落样式名或 outlineLvl 解析标题级别 1–9。"""
    style = paragraph.style
    if style is not None and style.name:
        match = _HEADING_NAME.match(style.name.strip())
        if match:
            return int(match.group(1))
    try:
        p_pr = paragraph._p.pPr  # noqa: SLF001 - python-docx 无稳定公开 API
    except AttributeError:
        p_pr = None
    if p_pr is not None:
        outline = p_pr.find(qn("w:outlineLvl"))
        if outline is not None:
            raw = outline.get(qn("w:val"))
            if raw is not None:
                try:
                    level = int(raw) + 1  # OOXML outlineLvl 0 = Heading 1
                except ValueError:
                    level = 0
                if 1 <= level <= 9:
                    return level
    return None


def _cell_text(cell) -> str:
    """合并单元格内段落文本。"""
    parts = [p.text.strip() for p in cell.paragraphs if p.text and p.text.strip()]
    return "\n".join(parts)


def read_docx(path: str, source_lang: str, target_lang: str) -> Document:
    """读取 .docx，识别标题切章，抽出段落与简易表格。"""
    try:
        docx = open_docx(path)
    except Exception as error:  # noqa: BLE001 - 统一为可读的输入错误
        raise ValueError(f"无法读取 Word 文档：{error}") from error

    book_title = os.path.splitext(os.path.basename(path))[0]
    blocks: list[dict[str, Any]] = []
    table_id = 0

    for block in _iter_body_blocks(docx):
        if isinstance(block, DocxParagraph):
            text = (block.text or "").strip()
            if not text:
                continue
            level = _outline_level(block)
            if level is not None:
                blocks.append({"kind": "heading", "text": text, "level": level})
            else:
                blocks.append({"kind": "text", "text": text})
            continue

        if isinstance(block, DocxTable):
            rows = list(block.rows)
            if not rows:
                continue
            cols = max((len(row.cells) for row in rows), default=0)
            if cols == 0:
                continue
            cells: list[dict[str, Any]] = []
            for r_idx, row in enumerate(rows):
                row_cells = list(row.cells)
                for c_idx in range(cols):
                    cell = row_cells[c_idx] if c_idx < len(row_cells) else None
                    text = _cell_text(cell) if cell is not None else ""
                    cells.append(
                        {
                            "text": text or "",
                            "row": r_idx,
                            "col": c_idx,
                        }
                    )
            blocks.append(
                {
                    "kind": "table",
                    "table_id": table_id,
                    "rows": len(rows),
                    "cols": cols,
                    "cells": cells,
                }
            )
            table_id += 1

    if not blocks:
        raise ValueError("Word 文档中未解析到可翻译段落或表格")

    # 按一级标题切章；无一级标题则整篇一章
    chapter_specs: list[tuple[str | None, int, list[dict[str, Any]]]] = []
    current_title: str | None = None
    current_level = 1
    current_body: list[dict[str, Any]] = []
    for item in blocks:
        if item["kind"] == "heading" and item["level"] == 1:
            if current_title is not None or current_body:
                chapter_specs.append((current_title, current_level, current_body))
            current_title = item["text"]
            current_level = 1
            current_body = [item]
        else:
            current_body.append(item)
    if current_title is not None or current_body:
        chapter_specs.append((current_title, current_level, current_body))

    chapters: list[Chapter] = []
    for ci, (explicit_title, level, body) in enumerate(chapter_specs):
        title = explicit_title or book_title
        segments: list[Segment] = []
        idx = 0
        for item in body:
            if item["kind"] == "heading":
                segments.append(
                    Segment(
                        index=idx,
                        source=item["text"],
                        kind=KIND_HEADING,
                        meta={"heading_level": int(item["level"])},
                    )
                )
                idx += 1
            elif item["kind"] == "text":
                segments.append(Segment(index=idx, source=item["text"], kind=KIND_TEXT))
                idx += 1
            elif item["kind"] == "table":
                for cell in item["cells"]:
                    segments.append(
                        Segment(
                            index=idx,
                            source=cell["text"],
                            kind=KIND_TEXT,
                            meta={
                                "table_id": item["table_id"],
                                "row": cell["row"],
                                "col": cell["col"],
                                "rows": item["rows"],
                                "cols": item["cols"],
                            },
                        )
                    )
                    idx += 1
        chapters.append(
            Chapter(
                index=ci,
                title=title,
                segments=segments,
                meta={"heading_level": level, "explicit_title": bool(explicit_title)},
            )
        )

    return Document(
        title=book_title,
        source_lang=source_lang,
        target_lang=target_lang,
        fmt="docx",
        source_path=os.path.abspath(path),
        chapters=chapters,
    )
