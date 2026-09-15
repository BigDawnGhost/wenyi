"""Apply Word run, paragraph, heading and target-font policies."""

from __future__ import annotations

import hashlib
from typing import Any

from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_COLOR_INDEX
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

from wenyi_core.document_styles.docx import (
    proportional_range_placements,
)

_ZH_FONT = "宋体"


_ALIGN_MAP = {
    "left": WD_ALIGN_PARAGRAPH.LEFT,
    "center": WD_ALIGN_PARAGRAPH.CENTER,
    "right": WD_ALIGN_PARAGRAPH.RIGHT,
    "both": WD_ALIGN_PARAGRAPH.JUSTIFY,
    "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
    "distribute": WD_ALIGN_PARAGRAPH.DISTRIBUTE,
}


def _set_outline_level(paragraph, level: int) -> None:
    """Ensure the paragraph has outlineLvl for the Word navigation pane."""
    level = max(1, min(9, level))
    p_pr = paragraph._p.get_or_add_pPr()  # noqa: SLF001
    outline = p_pr.find(qn("w:outlineLvl"))
    if outline is None:
        outline = p_pr.makeelement(qn("w:outlineLvl"), {})
        p_pr.append(outline)
    outline.set(qn("w:val"), str(level - 1))


def _set_run_font(run, font_name: str) -> None:
    """Set ascii/hAnsi/eastAsia fonts on a run, not just the Western font name."""
    name = font_name.strip()
    if not name:
        return
    run.font.name = name
    r_pr = run._r.get_or_add_rPr()  # noqa: SLF001
    r_fonts = r_pr.find(qn("w:rFonts"))
    if r_fonts is None:
        r_fonts = OxmlElement("w:rFonts")
        r_pr.insert(0, r_fonts)
    for attr in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
        r_fonts.set(qn(attr), name)


def _target_output_font(target_lang: str | None) -> str | None:
    """Return the target font: SimSun for Chinese, otherwise leave the font unspecified."""
    normalized = (target_lang or "zh").strip().lower().replace("_", "-")
    if normalized == "zh" or normalized.startswith("zh-"):
        return _ZH_FONT
    return None


def _font_for_text(
    output_font: str | None,
    *,
    source: str,
    output_text: str,
    is_source_side: bool = False,
) -> str | None:
    """Use the target font only for translations, not source fallbacks or bilingual source
    text.
    """
    if is_source_side or not output_font:
        return None
    if not output_text.strip() or output_text == source:
        return None
    return output_font


def _apply_run_style(
    run,
    style: dict[str, Any] | None,
    *,
    output_font: str | None = None,
) -> None:
    """Apply metadata character styles to a run, using output_font instead of the source font."""
    if style:
        if "bold" in style:
            run.bold = bool(style["bold"])
        if "italic" in style:
            run.italic = bool(style["italic"])
        if style.get("underline"):
            run.underline = True
        size_pt = style.get("size_pt")
        if isinstance(size_pt, (int, float)) and size_pt > 0:
            run.font.size = Pt(float(size_pt))
        color = style.get("color")
        if isinstance(color, str) and len(color) >= 6:
            try:
                run.font.color.rgb = RGBColor.from_string(color[-6:])
            except (ValueError, TypeError):
                pass
    if output_font:
        _set_run_font(run, output_font)


def _set_run_color_value(run, value: str) -> None:
    """Set explicit color and remove themeColor to override default blue heading themes."""
    r_pr = run._r.get_or_add_rPr()  # noqa: SLF001
    for child in list(r_pr):
        if child.tag == qn("w:color"):
            r_pr.remove(child)
    color = OxmlElement("w:color")
    color.set(qn("w:val"), value)
    r_pr.append(color)


def _neutralize_heading_theme_color(
    paragraph,
    style: dict[str, Any] | None,
) -> None:
    """Use black when no explicit heading color is supplied instead of the default accent blue."""
    explicit = None
    if isinstance(style, dict):
        color = style.get("color")
        if isinstance(color, str) and len(color) >= 6:
            explicit = color[-6:]
    value = explicit or "000000"
    for run in paragraph.runs:
        if run.text:
            _set_run_color_value(run, value)


def _style_slices(
    text: str,
    style: dict[str, Any] | None,
    placements: list[dict[str, Any]] | None,
) -> list[tuple[str, dict[str, Any] | None]]:
    """Split text into fragment/style pairs; return one slice for uniform styling."""
    if not text:
        return []
    if not placements:
        return [(text, style)]
    bounds = [0, len(text)]
    usable: list[dict[str, Any]] = []
    for row in placements:
        start = row.get("target_start")
        end = row.get("target_end")
        if not isinstance(start, int) or not isinstance(end, int):
            continue
        start = max(0, min(len(text), start))
        end = max(start, min(len(text), end))
        if start >= end:
            continue
        usable.append({**row, "target_start": start, "target_end": end})
        bounds.extend((start, end))
    if not usable:
        return [(text, style)]
    cuts = sorted(set(bounds))
    slices: list[tuple[str, dict[str, Any] | None]] = []
    for left, right in zip(cuts, cuts[1:]):
        if left >= right:
            continue
        fragment = text[left:right]
        matched: dict[str, Any] | None = None
        for row in usable:
            if row["target_start"] <= left and right <= row["target_end"]:
                matched = {
                    key: row[key]
                    for key in ("bold", "italic", "underline", "color", "size_pt", "font")
                    if key in row
                }
                break
        slices.append((fragment, matched or style))
    return slices or [(text, style)]


def _apply_paragraph_align(paragraph, align: str | None) -> None:
    """Apply paragraph alignment."""
    if not align:
        return
    value = _ALIGN_MAP.get(str(align).strip().lower())
    if value is not None:
        paragraph.alignment = value


def _apply_paragraph_shade(paragraph, shade: str | None) -> None:
    """Apply paragraph background shading."""
    if not shade:
        return
    p_pr = paragraph._p.get_or_add_pPr()  # noqa: SLF001
    shd = p_pr.find(qn("w:shd"))
    if shd is None:
        shd = p_pr.makeelement(qn("w:shd"), {})
        p_pr.append(shd)
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), str(shade).upper()[-6:])


def _fill_paragraph(
    paragraph,
    text: str,
    *,
    style: dict[str, Any] | None = None,
    placements: list[dict[str, Any]] | None = None,
    align: str | None = None,
    shade: str | None = None,
    output_font: str | None = None,
    dim: bool = False,
) -> None:
    """Clear the paragraph and write styled text slices."""
    paragraph.clear()
    _apply_paragraph_align(paragraph, align)
    _apply_paragraph_shade(paragraph, shade)
    for fragment, frag_style in _style_slices(text, style, placements):
        run = paragraph.add_run(fragment)
        _apply_run_style(run, frag_style, output_font=output_font)
        if dim:
            run.font.size = Pt(10)
            run.font.color.rgb = RGBColor(0x88, 0x88, 0x88)
            try:
                run.font.highlight_color = WD_COLOR_INDEX.GRAY_25
            except (AttributeError, ValueError):
                pass


def _segment_style_payload(
    meta: dict[str, Any],
    *,
    source: str,
    output_text: str,
) -> tuple[dict[str, Any] | None, list | None, str | None, str | None]:
    """Return paragraph style, mixed placements, alignment and shading.
    If mixed-style placements are missing or invalid because alignment, hashes or
    translation are unavailable, map source offsets proportionally to the output text. This
    preserves styling such as bold names when exporting a source fallback.
    """
    align = meta.get("align") if isinstance(meta.get("align"), str) else None
    shade = meta.get("shade") if isinstance(meta.get("shade"), str) else None
    uniform = meta.get("docx_style")
    if isinstance(uniform, dict) and uniform:
        return uniform, None, align, shade
    styles = meta.get("docx_styles")
    if not isinstance(styles, dict):
        return None, None, align, shade
    placements = styles.get("placements")
    output_digest = hashlib.sha256(output_text.encode("utf-8")).hexdigest()
    if isinstance(placements, list) and placements and styles.get("target_digest") == output_digest:
        return None, placements, align, shade
    items = styles.get("items")
    if isinstance(items, list) and items:
        return None, proportional_range_placements(source, output_text, items), align, shade
    return None, None, align, shade
