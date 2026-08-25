"""Word .docx 读取：段落 + 简易表格 → Document。

标题样式（Heading / 标题 / outlineLvl）映射为 heading segments，并按一级标题切章。
表格按文档顺序抽出，单元格合并为一段，meta 记录行列供写出时重建。

字符样式：
- 整段同质 → ``meta["docx_style"]``（写出整段套用，无需 AI 对齐）
- 段内混排 → ``meta["docx_styles"]["items"]``（源文偏移 + bold/color 等，译后对齐）
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

_STYLE_KEYS = ("bold", "italic", "underline", "color", "size_pt", "font")


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


def _paragraph_align(paragraph: DocxParagraph) -> str | None:
    """读取段落对齐：center / left / right / both / distribute。"""
    alignment = paragraph.alignment
    if alignment is not None:
        mapping = {
            0: "left",
            1: "center",
            2: "right",
            3: "both",
            4: "distribute",
        }
        name = mapping.get(int(alignment))
        if name:
            return name
        # Enum may expose .name
        raw = getattr(alignment, "name", None)
        if isinstance(raw, str) and raw.lower() in mapping.values():
            return raw.lower()
    try:
        p_pr = paragraph._p.pPr  # noqa: SLF001
    except AttributeError:
        p_pr = None
    if p_pr is not None:
        jc = p_pr.find(qn("w:jc"))
        if jc is not None:
            value = jc.get(qn("w:val"))
            if isinstance(value, str) and value:
                # OOXML uses "both" for justify; accept common aliases
                normalized = value.strip().lower()
                if normalized in {"left", "center", "right", "both", "distribute", "justify"}:
                    return "both" if normalized == "justify" else normalized
    return None


def _run_style(run) -> dict[str, Any]:
    """抽取单个 run 的可见字符样式（仅显式设置的字段）。"""
    style: dict[str, Any] = {}
    if run.bold is True:
        style["bold"] = True
    elif run.bold is False:
        style["bold"] = False
    if run.italic is True:
        style["italic"] = True
    elif run.italic is False:
        style["italic"] = False
    if run.underline:
        style["underline"] = True
    size = run.font.size
    if size is not None:
        try:
            style["size_pt"] = float(size.pt)
        except (AttributeError, TypeError, ValueError):
            pass
    color = run.font.color
    if color is not None and color.rgb is not None:
        style["color"] = str(color.rgb)
    else:
        # 主题色/直写 w:color 时 rgb 可能为空，回退读 XML
        try:
            r_pr = run._r.rPr  # noqa: SLF001
        except AttributeError:
            r_pr = None
        if r_pr is not None:
            color_node = r_pr.find(qn("w:color"))
            if color_node is not None:
                value = color_node.get(qn("w:val"))
                if isinstance(value, str) and value and value.lower() not in {"auto", "nil"}:
                    style["color"] = value.upper()
    name = run.font.name
    if isinstance(name, str) and name.strip():
        style["font"] = name.strip()
    return style


def _paragraph_shade(paragraph: DocxParagraph) -> str | None:
    """读取段落底纹填充色（w:shd/@w:fill）。"""
    try:
        p_pr = paragraph._p.pPr  # noqa: SLF001
    except AttributeError:
        return None
    if p_pr is None:
        return None
    shd = p_pr.find(qn("w:shd"))
    if shd is None:
        return None
    fill = shd.get(qn("w:fill"))
    if isinstance(fill, str) and fill and fill.lower() not in {"auto", "nil"}:
        return fill.upper()
    return None


def _style_fingerprint(style: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
    return tuple((key, style[key]) for key in _STYLE_KEYS if key in style)


def _paragraph_text_and_style_meta(paragraph: DocxParagraph) -> tuple[str, dict[str, Any]]:
    """合并段落 runs 为纯文本，并生成 align / docx_style / docx_styles meta。"""
    align = _paragraph_align(paragraph)
    spans: list[dict[str, Any]] = []
    parts: list[str] = []
    offset = 0
    for run in paragraph.runs:
        text = run.text or ""
        if not text:
            continue
        start, end = offset, offset + len(text)
        spans.append({"start": start, "end": end, "style": _run_style(run)})
        parts.append(text)
        offset = end
    text = "".join(parts).strip()
    shade = _paragraph_shade(paragraph)

    def _with_para_props(meta: dict[str, Any]) -> dict[str, Any]:
        if align:
            meta = {**meta, "align": align}
        if shade:
            meta = {**meta, "shade": shade}
        return meta

    if not text:
        return "", _with_para_props({})

    # strip() 可能去掉首尾空白：按 strip 后的文本重算相对偏移
    leading = len("".join(parts)) - len("".join(parts).lstrip())
    stripped = "".join(parts).strip()
    if stripped != "".join(parts):
        new_spans: list[dict[str, Any]] = []
        for span in spans:
            start = max(0, span["start"] - leading)
            end = min(len(stripped), span["end"] - leading)
            if start >= end:
                continue
            new_spans.append({"start": start, "end": end, "style": span["style"]})
        spans = new_spans
        text = stripped

    if not spans:
        return text, _with_para_props({})

    merged: list[dict[str, Any]] = []
    for span in spans:
        if (
            merged
            and merged[-1]["end"] == span["start"]
            and _style_fingerprint(merged[-1]["style"]) == _style_fingerprint(span["style"])
        ):
            merged[-1]["end"] = span["end"]
        else:
            merged.append(
                {"start": span["start"], "end": span["end"], "style": dict(span["style"])}
            )

    fingerprints = {_style_fingerprint(item["style"]) for item in merged}
    if len(fingerprints) <= 1:
        style = merged[0]["style"]
        return text, _with_para_props({"docx_style": style} if style else {})

    items: list[dict[str, Any]] = []
    for index, span in enumerate(merged):
        if not span["style"]:
            continue
        items.append(
            {
                "id": f"s{index}",
                "mode": "range",
                "source_start": int(span["start"]),
                "source_end": int(span["end"]),
                **span["style"],
            }
        )
    if not items:
        return text, _with_para_props({})
    if len(items) == 1 and items[0]["source_start"] == 0 and items[0]["source_end"] == len(text):
        style = {key: items[0][key] for key in _STYLE_KEYS if key in items[0]}
        return text, _with_para_props({"docx_style": style})
    return text, _with_para_props({"docx_styles": {"items": items}})


def _cell_text_and_style(cell) -> tuple[str, dict[str, Any]]:
    """合并单元格段落；样式取自首个非空段落。"""
    texts: list[str] = []
    style_meta: dict[str, Any] = {}
    for paragraph in cell.paragraphs:
        text, meta = _paragraph_text_and_style_meta(paragraph)
        if not text:
            continue
        if not style_meta and meta:
            style_meta = meta
        texts.append(text)
    return "\n".join(texts), style_meta


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
            text, style_meta = _paragraph_text_and_style_meta(block)
            if not text:
                continue
            level = _outline_level(block)
            if level is not None:
                blocks.append(
                    {"kind": "heading", "text": text, "level": level, "style_meta": style_meta}
                )
            else:
                blocks.append({"kind": "text", "text": text, "style_meta": style_meta})
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
                    if cell is None:
                        text, style_meta = "", {}
                    else:
                        text, style_meta = _cell_text_and_style(cell)
                    cells.append(
                        {
                            "text": text or "",
                            "row": r_idx,
                            "col": c_idx,
                            "style_meta": style_meta,
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
            style_meta = item.get("style_meta") or {}
            if item["kind"] == "heading":
                meta = {"heading_level": int(item["level"]), **style_meta}
                segments.append(
                    Segment(
                        index=idx,
                        source=item["text"],
                        kind=KIND_HEADING,
                        meta=meta,
                    )
                )
                idx += 1
            elif item["kind"] == "text":
                segments.append(
                    Segment(index=idx, source=item["text"], kind=KIND_TEXT, meta=dict(style_meta))
                )
                idx += 1
            elif item["kind"] == "table":
                for cell in item["cells"]:
                    meta = {
                        "table_id": item["table_id"],
                        "row": cell["row"],
                        "col": cell["col"],
                        "rows": item["rows"],
                        "cols": item["cols"],
                        **(cell.get("style_meta") or {}),
                    }
                    segments.append(
                        Segment(
                            index=idx,
                            source=cell["text"],
                            kind=KIND_TEXT,
                            meta=meta,
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
