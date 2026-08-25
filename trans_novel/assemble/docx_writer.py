"""从 RunStore 重建 Word .docx：标题导航 + 段落 + 简易表格。"""

from __future__ import annotations

from docx import Document as DocxDocument
from docx.enum.text import WD_COLOR_INDEX
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

from ..ingest.models import KIND_HEADING, Chapter
from ..pipeline.runstore import RunStore
from .writer_common import _bilingual_source, _ch_title, _ordered_pair, _seg_text


def _set_outline_level(paragraph, level: int) -> None:
    """确保段落带 outlineLvl，便于 Word 导航窗格。"""
    level = max(1, min(9, level))
    p_pr = paragraph._p.get_or_add_pPr()  # noqa: SLF001
    outline = p_pr.find(qn("w:outlineLvl"))
    if outline is None:
        outline = p_pr.makeelement(qn("w:outlineLvl"), {})
        p_pr.append(outline)
    outline.set(qn("w:val"), str(level - 1))


def _add_heading(doc: DocxDocument, text: str, level: int) -> None:
    level = max(1, min(9, level))
    style_name = f"Heading {level}"
    try:
        paragraph = doc.add_heading(text, level=level)
    except (KeyError, ValueError):
        paragraph = doc.add_paragraph(text)
        try:
            paragraph.style = style_name
        except (KeyError, ValueError):
            pass
    _set_outline_level(paragraph, level)


def _add_normal(doc: DocxDocument, text: str, *, dim: bool = False) -> None:
    paragraph = doc.add_paragraph()
    run = paragraph.add_run(text)
    if dim:
        run.font.size = Pt(10)
        run.font.color.rgb = RGBColor(0x88, 0x88, 0x88)
        try:
            run.font.highlight_color = WD_COLOR_INDEX.GRAY_25
        except (AttributeError, ValueError):
            pass


def _add_bilingual_paragraphs(
    doc: DocxDocument,
    source: str,
    target: str,
    order: str,
) -> None:
    src = _bilingual_source(source, target)
    if not src:
        _add_normal(doc, target)
        return
    first, second = _ordered_pair(src, target, order)
    # target_first: 译文正常，原文淡化
    if order == "source_first":
        _add_normal(doc, first, dim=False)
        _add_normal(doc, second, dim=True)
    else:
        _add_normal(doc, first, dim=False)
        _add_normal(doc, second, dim=True)


def _flush_table(
    doc: DocxDocument,
    cells: dict[tuple[int, int], tuple[str, str]],
    rows: int,
    cols: int,
    *,
    bilingual: bool,
    order: str,
) -> None:
    table = doc.add_table(rows=rows, cols=cols)
    try:
        table.style = "Table Grid"
    except (KeyError, ValueError):
        pass
    for r in range(rows):
        for c in range(cols):
            target, source = cells.get((r, c), ("", ""))
            cell = table.cell(r, c)
            cell.text = ""
            paragraph = cell.paragraphs[0]
            if bilingual:
                src = _bilingual_source(source, target)
                if src:
                    first, second = _ordered_pair(src, target, order)
                    paragraph.add_run(first)
                    paragraph.add_run("\n")
                    dim_run = paragraph.add_run(second)
                    dim_run.font.size = Pt(9)
                    dim_run.font.color.rgb = RGBColor(0x88, 0x88, 0x88)
                else:
                    paragraph.add_run(target)
            else:
                paragraph.add_run(target)


def _emit_chapter_blocks(
    doc: DocxDocument,
    chapter: Chapter,
    *,
    bilingual: bool,
    order: str,
) -> None:
    """按段顺序写出；连续同 table_id 聚合成一张表；cont 续段并回上一段。"""
    i = 0
    segs = chapter.segments
    while i < len(segs):
        seg = segs[i]
        meta = seg.meta if isinstance(seg.meta, dict) else {}
        table_id = meta.get("table_id")
        if isinstance(table_id, int):
            cells: dict[tuple[int, int], tuple[str, str]] = {}
            rows = int(meta.get("rows") or 1)
            cols = int(meta.get("cols") or 1)
            while i < len(segs):
                cur = segs[i]
                cur_meta = cur.meta if isinstance(cur.meta, dict) else {}
                if cur_meta.get("table_id") != table_id:
                    break
                r = int(cur_meta.get("row") or 0)
                c = int(cur_meta.get("col") or 0)
                rows = max(rows, int(cur_meta.get("rows") or rows))
                cols = max(cols, int(cur_meta.get("cols") or cols))
                cells[(r, c)] = (_seg_text(cur), cur.source)
                i += 1
            _flush_table(
                doc,
                cells,
                rows,
                cols,
                bilingual=bilingual,
                order=order,
            )
            continue

        if not seg.source.strip() and not (seg.target and seg.target.strip()):
            i += 1
            continue

        target_parts = [_seg_text(seg)]
        source_parts = [seg.source]
        kind = seg.kind
        heading_level = 1
        if kind == KIND_HEADING:
            raw_level = meta.get("heading_level", 1)
            heading_level = raw_level if isinstance(raw_level, int) else 1
        i += 1
        while i < len(segs):
            nxt = segs[i]
            nxt_meta = nxt.meta if isinstance(nxt.meta, dict) else {}
            if not nxt.cont or nxt_meta.get("table_id") is not None:
                break
            target_parts.append(_seg_text(nxt))
            source_parts.append(nxt.source)
            i += 1
        target = "".join(target_parts)
        source = "".join(source_parts)
        if kind == KIND_HEADING:
            _add_heading(doc, target, heading_level)
        elif bilingual:
            _add_bilingual_paragraphs(doc, source, target, order)
        else:
            _add_normal(doc, target)


def _assemble_docx(
    store: RunStore,
    out_path: str,
    *,
    bilingual: bool = False,
    order: str = "target_first",
) -> str:
    """按章节重建 .docx；标题带 outline，表格按 meta 重建。"""
    manifest = store.load_manifest()
    doc = DocxDocument()
    # 去掉默认空段
    if doc.paragraphs:
        p0 = doc.paragraphs[0]
        if not p0.text.strip():
            p0.clear()

    first_block = True
    for c in manifest["chapters"]:
        chapter = store.load_chapter(c["index"])
        # 若章内没有作为 heading 的章标题段，补一条 Heading 1（与显式章名一致）
        has_h1 = any(
            s.kind == KIND_HEADING
            and isinstance(s.meta, dict)
            and int(s.meta.get("heading_level") or 1) == 1
            for s in chapter.segments
        )
        title = _ch_title(c)
        if title and not has_h1 and chapter.meta.get("explicit_title"):
            if first_block and doc.paragraphs and not doc.paragraphs[0].text:
                # reuse first empty paragraph as heading
                pass
            _add_heading(doc, title, 1)
        _emit_chapter_blocks(doc, chapter, bilingual=bilingual, order=order)
        first_block = False

    # 删除文档开头残留空段
    body = doc.element.body
    for child in list(body):
        if child.tag == qn("w:p") and not (child.text or "").strip():
            # 仅删除完全无 runs 文本的首部空段
            texts = [node.text for node in child.iter(qn("w:t")) if node.text]
            if not any(texts) and body.index(child) == 0:
                body.remove(child)
            break

    doc.save(out_path)
    return out_path
