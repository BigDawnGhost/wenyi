"""Legacy runtime adapter for EPUB annotation coordination.

The framework-neutral position algorithms live in
``trans_novel.application.annotations``.  This module is the explicit side-
effect boundary for the current pipeline: it may mutate chapter targets, call
the annotation aligner, append recovery events, and persist a chapter through
``RunStore``.
"""

from __future__ import annotations

from collections.abc import Callable

from ..agents.annotation_aligner import AnnotationAlignment, AnnotationUnit, target_digest
from ..application.annotations import (
    annotation_contexts_for_segments,
    completed_logical_starts_in_range,
    logical_end_after,
    logical_start_at,
    plan_context_chapter_prefix_update,
)
from ..ingest.models import Chapter, Segment
from ..postprocess.punct import normalize_zh_segments
from .context import RollingContext
from .runstore import RunStore

AlignUnit = Callable[[AnnotationUnit], AnnotationAlignment]
AlignSegment = Callable[[int, Chapter, int, RunStore], None]
CompletedStarts = Callable[[list[Segment], int, int], list[int]]
FeatureSwitch = Callable[[], bool]


def sync_legacy_context_chapter_prefix(
    context: RollingContext,
    segments: list[Segment],
    end: int,
) -> None:
    """Apply the finalized chapter prefix to the retained context tail.

    Alignment can normalize an earlier piece only when its final continuation
    is completed.  The pure planner identifies the exact retained suffix; this
    adapter performs the in-memory mutation on the legacy ``RollingContext``.
    """
    update = plan_context_chapter_prefix_update(
        segments,
        end,
        recent_target_count=len(context.recent_targets),
    )
    if update is not None:
        context.recent_targets[-update.retained :] = update.targets


def align_legacy_segment_annotation(
    chapter_index: int,
    chapter: Chapter,
    start_position: int,
    store: RunStore,
    *,
    punctuation_enabled: FeatureSwitch,
    alignment_enabled: FeatureSwitch,
    align_unit: AlignUnit,
) -> None:
    """Align and persist one fully translated logical EPUB source paragraph.

    ``start_position`` is a chapter-local physical segment position.  It may
    point at a ``cont`` piece, so the pure index helper first resolves the
    logical head.  Preconditions that do not alter state return before any LLM
    or storage call.  Once punctuation changes a target, every exit path saves
    it unless a successful alignment immediately persists the same chapter.
    """
    segments = chapter.text_segments
    logical_start = logical_start_at(segments, start_position)
    if logical_start is None:
        return

    segment = segments[logical_start]
    metadata = segment.meta.get("epub_annotations")
    if not isinstance(metadata, dict):
        return
    raw_items = metadata.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        return

    # Metadata belongs to the head; source and target cover every following
    # continuation through the exclusive logical end.
    logical_end = logical_end_after(segments, logical_start)
    logical_segments = segments[logical_start:logical_end]
    if any(not (item.target and item.target.strip()) for item in logical_segments):
        return

    target_changed = False
    if punctuation_enabled():
        targets = [item.target or "" for item in logical_segments]
        normalized = normalize_zh_segments(
            targets,
            [item.cont for item in logical_segments],
        )
        target_changed = normalized != targets
        for item, value in zip(logical_segments, normalized):
            item.target = value

    source = "".join(item.source for item in logical_segments)
    target = "".join(item.target or "" for item in logical_segments)

    # A complete id set plus an exact target digest makes alignment idempotent
    # on resume.  Punctuation-only changes still cross the persistence boundary.
    expected_ids = {
        str(item.get("id")) for item in raw_items if isinstance(item, dict) and item.get("id")
    }
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
        if target_changed:
            store.save_chapter(chapter)
        return

    items = tuple(dict(item) for item in raw_items if isinstance(item, dict))
    if not items:
        if target_changed:
            store.save_chapter(chapter)
        return

    anchor = segment.anchor or f"segment-{segment.index}"
    unit = AnnotationUnit(
        unit_id=f"ch{chapter_index}:{anchor}",
        source=source,
        target=target,
        items=items,
    )
    if not alignment_enabled():
        # Preserve historical observability order: record the policy decision,
        # then persist only if normalization changed a completed target.
        store.log_event(
            "annotation_alignment_skipped",
            chapter=chapter_index,
            segment=segment.index,
            anchor=segment.anchor,
            unit_id=unit.unit_id,
            reason="disabled",
        )
        if target_changed:
            store.save_chapter(chapter)
        return

    try:
        result = align_unit(unit)
    except Exception as error:  # noqa: BLE001 - one unit must degrade without losing text
        # A normalized target is durable before the failure event is appended,
        # exactly matching the legacy crash/retry boundary.
        if target_changed:
            store.save_chapter(chapter)
        store.log_event(
            "annotation_alignment_failed",
            chapter=chapter_index,
            segment=segment.index,
            anchor=segment.anchor,
            unit_id=unit.unit_id,
            error=type(error).__name__,
            detail=str(error),
        )
        return

    metadata["target_digest"] = result.target_digest
    metadata["placements"] = [dict(item) for item in result.placements]

    # Save before the completion event.  A crash between these operations is
    # safe: the digest/id idempotency check prevents a second model call.
    store.save_chapter(chapter)
    store.log_event(
        "annotation_alignment_completed",
        chapter=chapter_index,
        segment=segment.index,
        anchor=segment.anchor,
        unit_id=unit.unit_id,
        annotations=len(items),
        used_fallback=result.used_fallback,
    )


def align_legacy_annotations_after_batch(
    chapter_index: int,
    chapter: Chapter,
    start: int,
    count: int,
    store: RunStore,
    *,
    align_segment: AlignSegment,
    completed_starts: CompletedStarts = completed_logical_starts_in_range,
) -> None:
    """Align eligible logical paragraphs serially in source order.

    ``start`` and ``count`` describe the physical batch as
    ``[start, start + count)``.  Serial callback execution preserves LLM call,
    chapter-save, and event ordering for batches containing multiple notes.
    """
    segments = chapter.text_segments
    for logical_start in completed_starts(segments, start, count):
        align_segment(chapter_index, chapter, logical_start, store)


__all__ = [
    "align_legacy_annotations_after_batch",
    "align_legacy_segment_annotation",
    "annotation_contexts_for_segments",
    "completed_logical_starts_in_range",
    "sync_legacy_context_chapter_prefix",
]
