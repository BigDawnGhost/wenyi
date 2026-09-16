"""Align mixed DOCX styles through model-backed placement and persist target-bound metadata."""

from __future__ import annotations

from typing import TYPE_CHECKING

from wenyi_core.agents.annotation_aligner import AnnotationUnit, target_digest
from wenyi_core.document_styles.docx import (
    merge_align_results,
    needs_alignment,
    proportional_range_placements,
)
from wenyi_core.ingest.models import Chapter
from wenyi_core.pipeline.annotations import AnnotationService
from wenyi_core.storage.protocol import Storage

if TYPE_CHECKING:
    from .runtime import PipelineRuntime


class DocxStyleService:
    """Align only mixed docx_styles.items; uniform docx_style needs no model call."""

    def __init__(self, runtime: PipelineRuntime):
        self._runtime = runtime

    def align_segment_styles(
        self,
        ci: int,
        chapter: Chapter,
        start_position: int,
        store: Storage,
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
            dict(item) for item in raw_items if isinstance(item, dict) and needs_alignment(item)
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
        store: Storage,
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
