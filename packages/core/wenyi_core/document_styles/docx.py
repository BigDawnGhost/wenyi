"""Pure DOCX source style policy, range placement and visible list-prefix recognition."""

from __future__ import annotations

import re
from typing import Any

_INHERIT_KEYS = ("bold", "italic", "underline", "color", "size_pt")


def style_fields(item: dict[str, Any]) -> dict[str, Any]:
    """Extract exportable character properties from source metadata, not model output."""
    out: dict[str, Any] = {}
    for key in _INHERIT_KEYS:
        if key in item:
            out[key] = item[key]
    return out


def needs_alignment(item: dict[str, Any]) -> bool:
    """Request alignment only for spans with bold, italic, underline or color styling."""
    return any(key in item for key in ("bold", "italic", "underline", "color"))


def proportional_range_placement(
    source: str,
    target: str,
    item: dict[str, Any],
) -> dict[str, Any] | None:
    """Build a proportional fallback placement for one span."""
    item_id = item.get("id")
    start = item.get("source_start")
    end = item.get("source_end")
    if not isinstance(item_id, str) or not item_id:
        return None
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    source_length = len(source)
    target_length = len(target)
    if source_length <= 0:
        t_start = t_end = 0
    else:
        t_start = min(
            target_length,
            (start * target_length + source_length // 2) // source_length,
        )
        t_end = min(
            target_length,
            (end * target_length + source_length // 2) // source_length,
        )
        if t_end < t_start:
            t_end = t_start
    return {
        "id": item_id,
        "mode": "range",
        "target_start": t_start,
        "target_end": t_end,
        "status": "fallback",
        "method": "proportional_source_range",
        **style_fields(item),
    }


def proportional_range_placements(
    source: str,
    target: str,
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Map a source range proportionally to target as a style fallback, avoiding zero-width end
    markers.
    """
    out: list[dict[str, Any]] = []
    for item in items:
        row = proportional_range_placement(source, target, item)
        if row is not None:
            out.append(row)
    return out


def _placement_usable(row: dict[str, Any]) -> bool:
    """Check for successful LLM alignment rather than a zero-width paragraph-end placeholder."""
    if row.get("status") == "fallback":
        return False
    if row.get("method") == "paragraph_end":
        return False
    start = row.get("target_start")
    end = row.get("target_end")
    return isinstance(start, int) and isinstance(end, int) and end >= start


def merge_align_results(
    source: str,
    target: str,
    items: list[dict[str, Any]],
    placements: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], bool]:
    """Merge by span: keep successful positions with source styles and proportionally map
    failed spans. Return placements and whether any fallback occurred.
    """
    by_id = {str(item.get("id")): item for item in items if isinstance(item.get("id"), str)}
    place_by_id = {
        str(row.get("id")): dict(row) for row in placements if isinstance(row.get("id"), str)
    }
    merged: list[dict[str, Any]] = []
    any_fallback = False
    for item in items:
        item_id = item.get("id")
        if not isinstance(item_id, str):
            continue
        row = place_by_id.get(item_id)
        if row is not None and _placement_usable(row):
            out = {
                "id": item_id,
                "mode": "range",
                "target_start": row["target_start"],
                "target_end": row["target_end"],
                "status": row.get("status", "aligned"),
                "method": row.get("method", "llm_markers"),
                **style_fields(item),
            }
            merged.append(out)
            continue
        fallback = proportional_range_placement(source, target, item)
        if fallback is not None:
            any_fallback = True
            merged.append(fallback)
    # Match by_id and ignore unknown IDs returned by alignment.
    _ = by_id
    return merged, any_fallback


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


def text_has_visible_list_prefix(text: str) -> bool:
    """Detect visible numbering already present in body text, such as 1. Title in a TOC."""
    return bool(_VISIBLE_LIST_PREFIX.match((text or "").lstrip()))
