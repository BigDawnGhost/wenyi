"""Align mixed DOCX character styles after translation using immutable-text markers.
Request one placement per style span. Inherit bold, italic, color and size from source
items, never from model output. Uniform paragraphs bypass this path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..agents.annotation_aligner import AnnotationUnit, target_digest
from ..ingest.models import Chapter
from .annotations import AnnotationService
from .runstore import RunStore

if TYPE_CHECKING:
    from .runtime import PipelineRuntime

# Source-item fields inherited during export, excluding the source font.
_INHERIT_KEYS = ("bold", "italic", "underline", "color", "size_pt")


def _style_fields(item: dict[str, Any]) -> dict[str, Any]:
    """Extract exportable character properties from source metadata, not model output."""
    out: dict[str, Any] = {}
    for key in _INHERIT_KEYS:
        if key in item:
            out[key] = item[key]
    return out


def _needs_alignment(item: dict[str, Any]) -> bool:
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
        **_style_fields(item),
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
                **_style_fields(item),
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


class DocxStyleService:
    """Align only mixed docx_styles.items; uniform docx_style needs no model call."""

    def __init__(self, runtime: PipelineRuntime):
        self._runtime = runtime

    def align_segment_styles(
        self,
        ci: int,
        chapter: Chapter,
        start_position: int,
        store: RunStore,
    ) -> None:
        """Align mixed styles for one logical paragraph including continuations and persist
        metadata.
        """
        segments = chapter.text_segments
        if not 0 <= start_position < len(segments):
            return
        while start_position > 0 and segments[start_position].cont:
            start_position -= 1
        segment = segments[start_position]
        metadata = segment.meta.get("docx_styles")
        if not isinstance(metadata, dict):
            return
        if segment.meta.get("docx_style"):
            return
        raw_items = metadata.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            return
        items = [
            dict(item) for item in raw_items if isinstance(item, dict) and _needs_alignment(item)
        ]
        if not items:
            return

        logical_segments = [segment]
        cursor = start_position + 1
        while cursor < len(segments) and segments[cursor].cont:
            logical_segments.append(segments[cursor])
            cursor += 1
        if any(not (item.target and item.target.strip()) for item in logical_segments):
            return

        source = "".join(item.source for item in logical_segments)
        target = "".join(item.target or "" for item in logical_segments)
        expected_ids = {str(item.get("id")) for item in items if isinstance(item.get("id"), str)}
        placements = metadata.get("placements")
        placement_ids = {
            str(item.get("id"))
            for item in placements or []
            if isinstance(item, dict) and item.get("id")
        }
        if (
            metadata.get("target_digest") == target_digest(target)
            and expected_ids
            and placement_ids == expected_ids
        ):
            return

        # The model locates positions only; AnnotationAligner splits multiple spans into individual requests.
        align_items = []
        for item in items:
            item_id = item.get("id")
            start = item.get("source_start")
            end = item.get("source_end")
            if not isinstance(item_id, str) or not item_id:
                continue
            if not isinstance(start, int) or not isinstance(end, int):
                continue
            align_items.append(
                {
                    "id": item_id,
                    "mode": "range",
                    "source_start": start,
                    "source_end": end,
                }
            )
        if not align_items:
            return

        unit = AnnotationUnit(
            unit_id=f"docx-style:ch{ci}:{segment.anchor or segment.index}",
            source=source,
            target=target,
            items=tuple(align_items),
        )

        try:
            result = self._runtime.annotation_aligner.align_unit(unit)
            raw_placements = [dict(row) for row in result.placements]
            # Keep successful model positions with source styles and proportionally map only failed spans.
            merged, any_fallback = merge_align_results(source, target, items, raw_placements)
            used_fallback = any_fallback
        except Exception as error:  # noqa: BLE001 - Styling failures must not block translation.
            merged = proportional_range_placements(source, target, items)
            used_fallback = True
            store.log_event(
                "docx_style_alignment_failed",
                chapter=ci,
                segment=segment.index,
                error=type(error).__name__,
                detail=str(error),
            )

        metadata["target_digest"] = target_digest(target)
        metadata["placements"] = merged
        store.save_chapter(chapter)
        store.log_event(
            "docx_style_alignment_completed",
            chapter=ci,
            segment=segment.index,
            spans=len(items),
            used_fallback=used_fallback,
        )

    def align_styles_after_batch(
        self,
        ci: int,
        chapter: Chapter,
        start: int,
        count: int,
        store: RunStore,
    ) -> None:
        """Process completed mixed-style logical paragraphs touched by this batch."""
        segments = chapter.text_segments
        for logical_start in AnnotationService.completed_logical_starts_in_range(
            segments, start, count
        ):
            segment = segments[logical_start]
            styles = segment.meta.get("docx_styles")
            if isinstance(styles, dict) and styles.get("items"):
                self.align_segment_styles(ci, chapter, logical_start, store)
