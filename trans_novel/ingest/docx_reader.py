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
# 只有这些差异才值得混排对齐；font/size 单独变化不拆 span（中文导出也不保留西文字体）
_ALIGN_STYLE_KEYS = ("bold", "italic", "underline", "color")

# 正文已自带可见序号时（如目录「1. Title」），不再套 Word 自动编号，避免双重序号
_VISIBLE_LIST_PREFIX = re.compile(
    r"^(?:"
    r"\d+\."  # 1.
    r"|[A-Za-z]\."  # A.
    r"|[ivxlcdm]+\."  # i. / iv.
    r"|[•·‣▪◦‣]\s*"  # bullets
    r"|（?\d+）"  # （1）
    r"|\(\d+\)"  # (1)
    r")\s+",
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


def _text_has_visible_list_prefix(text: str) -> bool:
    """正文是否已含可见序号（目录常见「1. Title」）。"""
    return bool(_VISIBLE_LIST_PREFIX.match((text or "").lstrip()))


def _list_meta(paragraph: DocxParagraph, numbering_root) -> dict[str, Any] | None:
    """读取 Word 自动编号：list_num_id / list_ilvl / list_fmt（decimal|bullet|…）。

    编号可能写在段落 ``w:numPr``，也可能只挂在段落样式上（如 List Number）。
    ``numId<=0`` 视为无效；正文已自带「1.」等前缀时不返回列表 meta，避免导出双重序号。
    """
    num_pr = None
    try:
        p_pr = paragraph._p.pPr  # noqa: SLF001
    except AttributeError:
        p_pr = None
    if p_pr is not None:
        num_pr = p_pr.find(qn("w:numPr"))
    if num_pr is None and paragraph.style is not None:
        try:
            style_p_pr = paragraph.style.element.pPr
            if style_p_pr is not None:
                num_pr = style_p_pr.find(qn("w:numPr"))
        except AttributeError:
            num_pr = None
    if num_pr is None:
        return None
    num_id_el = num_pr.find(qn("w:numId"))
    if num_id_el is None:
        return None
    raw_num_id = num_id_el.get(qn("w:val"))
    if raw_num_id is None:
        return None
    try:
        num_id = int(raw_num_id)
    except ValueError:
        return None
    if num_id <= 0:
        return None
    ilvl_el = num_pr.find(qn("w:ilvl"))
    try:
        ilvl = int(ilvl_el.get(qn("w:val"))) if ilvl_el is not None else 0
    except (TypeError, ValueError):
        ilvl = 0
    ilvl = max(0, min(8, ilvl))
    fmt = "decimal"
    if numbering_root is not None:
        abstract_id = None
        for num in numbering_root.findall(qn("w:num")):
            if num.get(qn("w:numId")) == str(num_id):
                abs_el = num.find(qn("w:abstractNumId"))
                if abs_el is not None:
                    abstract_id = abs_el.get(qn("w:val"))
                break
        if abstract_id is not None:
            for abs_num in numbering_root.findall(qn("w:abstractNum")):
                if abs_num.get(qn("w:abstractNumId")) != abstract_id:
                    continue
                for lvl in abs_num.findall(qn("w:lvl")):
                    if lvl.get(qn("w:ilvl")) != str(ilvl):
                        continue
                    fmt_el = lvl.find(qn("w:numFmt"))
                    if fmt_el is not None:
                        raw_fmt = fmt_el.get(qn("w:val"))
                        if isinstance(raw_fmt, str) and raw_fmt:
                            fmt = raw_fmt
                    break
                break
    return {"list_num_id": num_id, "list_ilvl": ilvl, "list_fmt": fmt}


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


def _align_style_fingerprint(style: dict[str, Any]) -> tuple[tuple[str, Any], ...]:
    """仅含需定位的可见样式，用于判断是否真·混排。"""
    return tuple((key, style[key]) for key in _ALIGN_STYLE_KEYS if key in style)


def _meaningful_align_style(style: dict[str, Any]) -> dict[str, Any]:
    """对齐/写出时继承的样式；可带 size_pt，但不含原文 font。"""
    out: dict[str, Any] = {}
    for key in _ALIGN_STYLE_KEYS:
        if key in style:
            out[key] = style[key]
    if "size_pt" in style:
        out["size_pt"] = style["size_pt"]
    return out


def _paragraph_text_and_style_meta(
    paragraph: DocxParagraph,
    numbering_root=None,
) -> tuple[str, dict[str, Any]]:
    """合并段落 runs 为纯文本，并生成 align / list / docx_style / docx_styles meta。"""
    align = _paragraph_align(paragraph)
    list_meta = _list_meta(paragraph, numbering_root)
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
        # 目录等正文已写「1. …」时，忽略自动编号，防止 List Number 再叠一层
        if list_meta and not _text_has_visible_list_prefix(text):
            meta = {**meta, **list_meta}
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

    # 合并相邻 run：仅按「需对齐的可见样式」判断，避免 font/size 把整段拆碎
    merged: list[dict[str, Any]] = []
    for span in spans:
        if (
            merged
            and merged[-1]["end"] == span["start"]
            and _align_style_fingerprint(merged[-1]["style"])
            == _align_style_fingerprint(span["style"])
        ):
            merged[-1]["end"] = span["end"]
            # 保留一份 size 供写出继承
            if "size_pt" in span["style"] and "size_pt" not in merged[-1]["style"]:
                merged[-1]["style"]["size_pt"] = span["style"]["size_pt"]
        else:
            merged.append(
                {"start": span["start"], "end": span["end"], "style": dict(span["style"])}
            )

    align_fps = {_align_style_fingerprint(item["style"]) for item in merged}
    if len(align_fps) <= 1:
        # 无 bold/italic/color 混排：整段同质（可带统一 size）
        style = _meaningful_align_style(merged[0]["style"])
        return text, _with_para_props({"docx_style": style} if style else {})

    items: list[dict[str, Any]] = []
    for index, span in enumerate(merged):
        meaningful = _meaningful_align_style(span["style"])
        # 普通正文（无加粗/斜体/颜色）不进对齐列表，写出时当默认 run
        if not any(key in meaningful for key in _ALIGN_STYLE_KEYS):
            continue
        items.append(
            {
                "id": f"s{index}",
                "mode": "range",
                "source_start": int(span["start"]),
                "source_end": int(span["end"]),
                **meaningful,
            }
        )
    if not items:
        return text, _with_para_props({})
    if len(items) == 1 and items[0]["source_start"] == 0 and items[0]["source_end"] == len(text):
        style = {key: items[0][key] for key in (*_ALIGN_STYLE_KEYS, "size_pt") if key in items[0]}
        return text, _with_para_props({"docx_style": style})
    return text, _with_para_props({"docx_styles": {"items": items}})


def _cell_text_and_style(cell, numbering_root=None) -> tuple[str, dict[str, Any]]:
    """合并单元格段落；样式取自首个非空段落。"""
    texts: list[str] = []
    style_meta: dict[str, Any] = {}
    for paragraph in cell.paragraphs:
        text, meta = _paragraph_text_and_style_meta(paragraph, numbering_root)
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

    numbering_root = None
    try:
        numbering_root = docx.part.numbering_part._element  # noqa: SLF001
    except (AttributeError, ValueError, KeyError):
        numbering_root = None

    book_title = os.path.splitext(os.path.basename(path))[0]
    blocks: list[dict[str, Any]] = []
    table_id = 0

    for block in _iter_body_blocks(docx):
        if isinstance(block, DocxParagraph):
            text, style_meta = _paragraph_text_and_style_meta(block, numbering_root)
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
                        text, style_meta = _cell_text_and_style(cell, numbering_root)
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
