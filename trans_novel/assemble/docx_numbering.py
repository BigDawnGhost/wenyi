"""Resolve Word numbering identities and restart list instances."""

from __future__ import annotations

from docx.document import Document as DocxDocument
from docx.oxml import OxmlElement
from docx.oxml.ns import qn


def _list_style_name(list_fmt: str | None, ilvl: int) -> str:
    """Map to built-in python-docx list style names."""
    level = max(0, min(2, int(ilvl)))
    bullet = (list_fmt or "").lower() in {"bullet", "none"}
    if bullet:
        return "List Bullet" if level == 0 else f"List Bullet {level + 1}"
    return "List Number" if level == 0 else f"List Number {level + 1}"


def _abstract_num_id_for_style(doc: DocxDocument, style_name: str) -> int | None:
    """Find abstractNumId through numPr on the paragraph style."""
    try:
        style = doc.styles[style_name]
    except KeyError:
        return None
    try:
        num_pr = style.element.pPr.numPr  # noqa: SLF001
        num_id = int(num_pr.numId.val)
    except (AttributeError, TypeError, ValueError):
        return None
    try:
        numbering = doc.part.numbering_part._element  # noqa: SLF001
    except (AttributeError, ValueError, KeyError):
        return None
    for num in numbering.findall(qn("w:num")):
        if num.get(qn("w:numId")) == str(num_id):
            abs_el = num.find(qn("w:abstractNumId"))
            if abs_el is not None:
                try:
                    return int(abs_el.get(qn("w:val")))
                except (TypeError, ValueError):
                    return None
    return None


def _next_num_id(numbering_root) -> int:
    used = []
    for num in numbering_root.findall(qn("w:num")):
        raw = num.get(qn("w:numId"))
        if raw is not None:
            try:
                used.append(int(raw))
            except ValueError:
                continue
    return (max(used) + 1) if used else 1


def _restart_list_numbering(doc: DocxDocument, paragraph, *, style_name: str, ilvl: int) -> None:
    """Create a numId with startOverride for the first item so each list restarts at 1."""
    abstract_id = _abstract_num_id_for_style(doc, style_name)
    if abstract_id is None:
        return
    try:
        numbering = doc.part.numbering_part._element  # noqa: SLF001
    except (AttributeError, ValueError, KeyError):
        return
    new_id = _next_num_id(numbering)
    num = OxmlElement("w:num")
    num.set(qn("w:numId"), str(new_id))
    abs_ref = OxmlElement("w:abstractNumId")
    abs_ref.set(qn("w:val"), str(abstract_id))
    num.append(abs_ref)
    override = OxmlElement("w:lvlOverride")
    override.set(qn("w:ilvl"), str(max(0, ilvl)))
    start = OxmlElement("w:startOverride")
    start.set(qn("w:val"), "1")
    override.append(start)
    num.append(override)
    numbering.append(num)

    p_pr = paragraph._p.get_or_add_pPr()  # noqa: SLF001
    num_pr = p_pr.get_or_add_numPr()
    num_pr.get_or_add_ilvl().val = max(0, ilvl)
    num_pr.get_or_add_numId().val = new_id
