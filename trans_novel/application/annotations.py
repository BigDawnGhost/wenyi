"""Framework-neutral algorithms for EPUB annotation-aware translation.

The functions in this module only inspect in-memory segment data and return
deterministic values.  They intentionally know nothing about ``RunStore``, LLM
agents, configuration models, or the filesystem; the legacy pipeline adapter
owns those side effects.

All segment positions are zero-based.  Ranges use a closed start and an open
end, matching normal Python slicing semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, Sequence


class AnnotationSegment(Protocol):
    """Minimal segment shape required by the annotation algorithms."""

    source: str
    target: str | None
    cont: bool
    meta: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ContextTailUpdate:
    """A planned replacement for the retained tail of rolling context."""

    retained: int
    targets: tuple[str, ...]


def plan_context_chapter_prefix_update(
    segments: Sequence[AnnotationSegment],
    end: int,
    *,
    recent_target_count: int,
) -> ContextTailUpdate | None:
    """Plan how a completed chapter prefix refreshes rolling-context targets.

    ``end`` is the exclusive chapter-local segment position reached by the
    current batch.  A logical source paragraph can span several batches; once
    its final continuation is translated, annotation alignment may normalize
    targets that were already copied into rolling context.  This plan replaces
    only the retained tail, preserving older cross-chapter context entries.
    """
    bounded_end = max(0, min(end, len(segments)))
    prefix = segments[:bounded_end]
    if not prefix or any(not (segment.target and segment.target.strip()) for segment in prefix):
        return None

    targets = tuple(segment.target or "" for segment in prefix)
    retained = min(len(targets), max(0, recent_target_count))
    if retained == 0:
        return None
    return ContextTailUpdate(retained=retained, targets=targets[-retained:])


def completed_logical_starts_in_range(
    segments: Sequence[AnnotationSegment],
    start: int,
    count: int,
) -> list[int]:
    """Return logical starts whose final piece lies in the current batch.

    A long source paragraph is represented by one head segment followed by
    zero or more ``cont`` segments.  A resumed batch may start in the middle of
    that group, so every inspected position first walks backward to the head.
    The group is returned exactly once, and only when its final piece lies in
    ``[start, start + count)``.  This prevents premature or duplicate alignment
    across batch boundaries.
    """
    if count <= 0 or not segments:
        return []

    lower = max(0, start)
    upper = min(len(segments), lower + count)
    starts: list[int] = []
    position = lower
    while position < upper:
        # Find the inclusive head of the logical paragraph containing position.
        logical_start = position
        while logical_start > 0 and segments[logical_start].cont:
            logical_start -= 1

        # Find the inclusive tail; alignment becomes eligible only at this piece.
        logical_end = logical_start
        while logical_end + 1 < len(segments) and segments[logical_end + 1].cont:
            logical_end += 1

        if lower <= logical_end < upper:
            starts.append(logical_start)
        position = max(position + 1, logical_end + 1)
    return starts


def logical_start_at(
    segments: Sequence[AnnotationSegment],
    position: int,
) -> int | None:
    """Return the head position for a segment, or ``None`` when out of range."""
    if not 0 <= position < len(segments):
        return None
    while position > 0 and segments[position].cont:
        position -= 1
    return position


def logical_end_after(
    segments: Sequence[AnnotationSegment],
    logical_start: int,
) -> int:
    """Return the exclusive end of the continuation group at ``logical_start``."""
    logical_end = logical_start + 1
    while logical_end < len(segments) and segments[logical_end].cont:
        logical_end += 1
    return logical_end


def annotation_contexts_for_segments(
    segments: Sequence[AnnotationSegment],
    registry: dict[str, Any] | None,
) -> list[list[dict[str, str]]]:
    """Assign book-level EPUB note source text to translation pieces.

    Parser metadata lives on the head of a logical paragraph.  Offsets inside
    that metadata are relative to the concatenated source text, while the
    translator consumes the physical split pieces.  Point annotations go to
    the piece ending at that boundary (position zero belongs to the first
    piece); non-empty ranges go to every piece whose half-open source interval
    intersects the range.  One target is injected at most once per piece.
    """
    assigned: list[list[dict[str, str]]] = [[] for _ in segments]
    if not isinstance(registry, dict):
        return assigned
    raw_contexts = registry.get("contexts")
    if not isinstance(raw_contexts, dict):
        return assigned

    position = 0
    while position < len(segments):
        logical_start = position
        logical_end = logical_end_after(segments, logical_start)
        logical_segments = segments[logical_start:logical_end]

        # Boundaries are relative to the concatenated logical source paragraph.
        boundaries: list[tuple[int, int]] = []
        source_cursor = 0
        for segment in logical_segments:
            piece_end = source_cursor + len(segment.source)
            boundaries.append((source_cursor, piece_end))
            source_cursor = piece_end

        metadata = logical_segments[0].meta.get("epub_annotations")
        raw_items = metadata.get("items") if isinstance(metadata, dict) else None
        items = raw_items if isinstance(raw_items, list) else []
        source_length = metadata.get("source_length") if isinstance(metadata, dict) else None
        if items and (
            not isinstance(source_length, int)
            or isinstance(source_length, bool)
            or source_length != source_cursor
        ):
            # Stale parser offsets are unsafe to associate with current pieces.
            position = logical_end
            continue

        seen_by_piece: list[set[str]] = [set() for _ in logical_segments]
        for raw_item in items:
            if not isinstance(raw_item, dict) or raw_item.get("relation") != "noteref":
                continue
            target_key = raw_item.get("target_key")
            if not isinstance(target_key, str) or not target_key:
                continue

            record = raw_contexts.get(target_key)
            if not isinstance(record, dict):
                continue
            raw_blocks = record.get("source_blocks")
            blocks = (
                [block for block in raw_blocks if isinstance(block, str) and block.strip()]
                if isinstance(raw_blocks, list)
                else []
            )
            if not blocks:
                continue
            note = {"target_key": target_key, "source": "\n\n".join(blocks)}

            source_start = raw_item.get("source_start")
            source_end = raw_item.get("source_end")
            if (
                not isinstance(source_start, int)
                or isinstance(source_start, bool)
                or not isinstance(source_end, int)
                or isinstance(source_end, bool)
                or not 0 <= source_start <= source_end <= source_cursor
            ):
                continue

            if raw_item.get("mode") == "range" and source_start < source_end:
                # Two half-open ranges intersect when both opposite bounds cross.
                piece_indices = [
                    index
                    for index, (piece_start, piece_end) in enumerate(boundaries)
                    if source_start < piece_end and source_end > piece_start
                ]
            else:
                # Points on a piece boundary belong to the preceding piece.
                piece_index = 0
                if source_start > 0:
                    piece_index = next(
                        (
                            index
                            for index, (_piece_start, piece_end) in enumerate(boundaries)
                            if source_start <= piece_end
                        ),
                        len(boundaries) - 1,
                    )
                piece_indices = [piece_index]

            for piece_index in piece_indices:
                if target_key in seen_by_piece[piece_index]:
                    continue
                seen_by_piece[piece_index].add(target_key)
                assigned[logical_start + piece_index].append(note)

        position = logical_end
    return assigned


__all__ = [
    "AnnotationSegment",
    "ContextTailUpdate",
    "annotation_contexts_for_segments",
    "completed_logical_starts_in_range",
    "logical_end_after",
    "logical_start_at",
    "plan_context_chapter_prefix_update",
]
