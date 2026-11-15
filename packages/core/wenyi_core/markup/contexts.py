"""Pure projection of format-owned annotations into read-only model references.

Adapters provide ``annotations`` metadata with source_length and ordered items.
Existing EPUB/PDF keys remain readable without migrating saved projects. An object
item uses relation=inline_object, target_key, marker and clean-source point offsets;
its content comes from registry source_blocks or an embedded object_source.
Consumers receive target_key/source and, for objects, marker/source_offset. They
never need to inspect the originating format or its restoration data.
"""

from __future__ import annotations

from typing import Any

from ..models.document import Segment
from .objects import OBJECT_RELATION


def annotation_metadata(segment: Segment) -> dict[str, Any] | None:
    """Select canonical metadata, with legacy names confined to this compatibility boundary."""
    for key in ("annotations", "epub_annotations", "pdf_annotations"):
        metadata = segment.meta.get(key)
        if isinstance(metadata, dict):
            return metadata
    return None


def reference_source(item: dict[str, Any], registry: dict[str, Any] | None) -> str:
    """Prefer registered contents, then embedded object text, then an opaque description."""
    contexts = registry.get("contexts") if isinstance(registry, dict) else None
    key = item.get("target_key")
    record = contexts.get(key) if isinstance(contexts, dict) and isinstance(key, str) else None
    raw_blocks = record.get("source_blocks") if isinstance(record, dict) else None
    blocks = (
        [block for block in raw_blocks if isinstance(block, str) and block.strip()]
        if isinstance(raw_blocks, list)
        else []
    )
    if blocks:
        return "\n\n".join(blocks)
    if item.get("relation") == OBJECT_RELATION:
        source = item.get("object_source")
        if isinstance(source, str) and source.strip():
            return source
        return "Original immutable object"
    return ""


def annotation_contexts_for_segments(
    segments: list[Segment],
    registry: dict[str, Any] | None = None,
) -> list[list[dict[str, str]]]:
    """Assign references to slices using clean-source offsets in their logical paragraph.

    Only the first slice owns metadata. Boundary points belong to the preceding
    slice (zero belongs to the first); ranges cover every intersecting slice.
    Preserve source order and deduplicate destinations within each slice.
    """
    assigned: list[list[dict[str, str]]] = [[] for _ in segments]
    position = 0
    while position < len(segments):
        logical_start = position
        logical_end = logical_start + 1
        while logical_end < len(segments) and segments[logical_end].cont:
            logical_end += 1
        logical_segments = segments[logical_start:logical_end]
        position = logical_end

        boundaries: list[tuple[int, int]] = []
        cursor = 0
        for segment in logical_segments:
            end = cursor + len(segment.source)
            boundaries.append((cursor, end))
            cursor = end

        metadata = annotation_metadata(logical_segments[0])
        raw_items = metadata.get("items") if metadata is not None else None
        items = raw_items if isinstance(raw_items, list) else []
        source_length = metadata.get("source_length") if metadata is not None else None
        if items and (type(source_length) is not int or source_length != cursor):
            continue
        seen_by_piece: list[set[str]] = [set() for _ in logical_segments]

        for item in items:
            if not isinstance(item, dict) or item.get("relation") not in {
                "noteref",
                OBJECT_RELATION,
            }:
                continue
            target_key = item.get("target_key")
            if not isinstance(target_key, str) or not target_key:
                continue
            source = reference_source(item, registry)
            if not source:
                continue
            start, end = item.get("source_start"), item.get("source_end")
            if type(start) is not int or type(end) is not int or not 0 <= start <= end <= cursor:
                continue
            is_object = item.get("relation") == OBJECT_RELATION
            marker = item.get("marker")
            if is_object and (not isinstance(marker, str) or not marker):
                continue

            if item.get("mode") == "range" and start < end:
                piece_indices = [
                    index
                    for index, (piece_start, piece_end) in enumerate(boundaries)
                    if start < piece_end and end > piece_start
                ]
            else:
                piece_indices = [
                    next(
                        index
                        for index, (_piece_start, piece_end) in enumerate(boundaries)
                        if start <= piece_end
                    )
                ]
            for piece_index in piece_indices:
                if target_key in seen_by_piece[piece_index]:
                    continue
                seen_by_piece[piece_index].add(target_key)
                reference = {"target_key": target_key, "source": source}
                if is_object and isinstance(marker, str):
                    reference.update(
                        marker=marker,
                        source_offset=str(start - boundaries[piece_index][0]),
                    )
                assigned[logical_start + piece_index].append(reference)
    return assigned


def object_contexts_for_segments(
    segments: list[Segment],
    registry: dict[str, Any] | None = None,
) -> list[list[dict[str, str]]]:
    """Select immutable objects without exposing ordinary notes as protected objects."""
    return [
        [reference for reference in references if "marker" in reference]
        for references in annotation_contexts_for_segments(segments, registry)
    ]
