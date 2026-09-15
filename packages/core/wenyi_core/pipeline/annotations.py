"""EPUB annotation context, continuation reconstruction, alignment and safe fallback.
Map source point/range offsets to translation slices, reconstruct logical paragraphs only
after the final continuation is translated, and cache placements by target_digest. Process
logical paragraphs serially and persist each immediately. Record alignment failures as
events and continue.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..agents.annotation_aligner import AnnotationUnit, target_digest
from ..ingest.models import Chapter, Segment
from .runstore import RunStore

if TYPE_CHECKING:
    from .runtime import PipelineRuntime


class AnnotationService:
    """Domain service for annotation alignment and continuation reconstruction."""

    def __init__(self, runtime: PipelineRuntime):
        self._runtime = runtime

    @staticmethod
    def completed_logical_starts_in_range(
        segments: list[Segment],
        start: int,
        count: int,
    ) -> list[int]:
        """Return ordered, unique logical-paragraph starts whose final slice is in this batch.
        A batch may begin at a continuation. Walk back to the original first slice so the
        complete source/target can be merged and aligned once the final continuation
        finishes. Return only groups whose final slice belongs to the current range,
        preventing duplicate work across batches.
        """
        if count <= 0 or not segments:
            return []
        lower = max(0, start)
        upper = min(len(segments), lower + count)
        starts: list[int] = []
        position = lower
        while position < upper:
            logical_start = position
            while logical_start > 0 and segments[logical_start].cont:
                logical_start -= 1
            logical_end = logical_start
            while logical_end + 1 < len(segments) and segments[logical_end + 1].cont:
                logical_end += 1
            if lower <= logical_end < upper:
                starts.append(logical_start)
            position = max(position + 1, logical_end + 1)
        return starts

    @staticmethod
    def annotation_contexts_for_segments(
        segments: list[Segment],
        registry: dict[str, Any] | None,
    ) -> list[list[dict[str, str]]]:
        """Assign book-level annotation source text to actual translation slices by source
        offsets.
        Only the first slice holds EPUB layout metadata. Use its original offsets and
        cumulative slice boundaries to assign point annotations to one slice and ranges to
        every intersecting slice. Inject each destination only once per slice.
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
            logical_end = logical_start + 1
            while logical_end < len(segments) and segments[logical_end].cont:
                logical_end += 1
            logical_segments = segments[logical_start:logical_end]

            boundaries: list[tuple[int, int]] = []
            cursor = 0
            for segment in logical_segments:
                end = cursor + len(segment.source)
                boundaries.append((cursor, end))
                cursor = end

            metadata = logical_segments[0].meta.get("epub_annotations")
            raw_items = metadata.get("items") if isinstance(metadata, dict) else None
            items = raw_items if isinstance(raw_items, list) else []
            source_length = metadata.get("source_length") if isinstance(metadata, dict) else None
            if items and (
                not isinstance(source_length, int)
                or isinstance(source_length, bool)
                or source_length != cursor
            ):
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
                note = {
                    "target_key": target_key,
                    "source": "\n\n".join(blocks),
                }

                start = raw_item.get("source_start")
                end = raw_item.get("source_end")
                if (
                    not isinstance(start, int)
                    or isinstance(start, bool)
                    or not isinstance(end, int)
                    or isinstance(end, bool)
                    or not 0 <= start <= end <= cursor
                ):
                    continue

                piece_indices: list[int]
                if raw_item.get("mode") == "range" and start < end:
                    piece_indices = [
                        index
                        for index, (piece_start, piece_end) in enumerate(boundaries)
                        if start < piece_end and end > piece_start
                    ]
                else:
                    # Assign boundary points to the preceding slice; position zero belongs to the first slice.
                    piece_index = 0
                    if start > 0:
                        piece_index = next(
                            (
                                index
                                for index, (_piece_start, piece_end) in enumerate(boundaries)
                                if start <= piece_end
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

    def align_segment_annotation(
        self,
        ci: int,
        chapter: Chapter,
        start_position: int,
        store: RunStore,
    ) -> None:
        """Align EPUB links for one fully translated logical paragraph.
        Only the first slice has an anchor and parsing metadata, so wait for all
        continuations before merging source/target. Align against formal target text; export
        punctuation normalization remaps placements only on a copy.
        Persist both valid placements and deterministic fallbacks immediately. Skip model
        calls when annotations are absent or translation is incomplete.
        """
        segments = chapter.text_segments
        if not 0 <= start_position < len(segments):
            return
        while start_position > 0 and segments[start_position].cont:
            start_position -= 1
        segment = segments[start_position]
        metadata = segment.meta.get("epub_annotations")
        if not isinstance(metadata, dict):
            return
        raw_items = metadata.get("items")
        if not isinstance(raw_items, list) or not raw_items:
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
            return

        items = tuple(dict(item) for item in raw_items if isinstance(item, dict))
        if not items:
            return
        anchor = segment.anchor or f"segment-{segment.index}"
        unit = AnnotationUnit(
            unit_id=f"ch{ci}:{anchor}",
            source=source,
            target=target,
            items=items,
        )
        if not self._runtime.config.pipeline.annotation_alignment:
            store.log_event(
                "annotation_alignment_skipped",
                chapter=ci,
                segment=segment.index,
                anchor=segment.anchor,
                unit_id=unit.unit_id,
                reason="disabled",
            )
            return

        try:
            result = self._runtime.annotation_aligner.align_unit(unit)
        except Exception as error:  # noqa: BLE001 - The writer safely degrades individual alignment failures.
            store.log_event(
                "annotation_alignment_failed",
                chapter=ci,
                segment=segment.index,
                anchor=segment.anchor,
                unit_id=unit.unit_id,
                error=type(error).__name__,
                detail=str(error),
            )
            return

        metadata["target_digest"] = result.target_digest
        metadata["placements"] = [dict(item) for item in result.placements]
        # Persist each logical paragraph atomically so interruptions do not repeat paid alignment calls
        # and users can inspect exports before the complete book finishes translating.
        store.save_chapter(chapter)
        store.log_event(
            "annotation_alignment_completed",
            chapter=ci,
            segment=segment.index,
            anchor=segment.anchor,
            unit_id=unit.unit_id,
            annotations=len(items),
            used_fallback=result.used_fallback,
        )

    def align_annotations_after_batch(
        self,
        ci: int,
        chapter: Chapter,
        start: int,
        count: int,
        store: RunStore,
    ) -> None:
        """Process complete annotated paragraphs touched by this batch serially in source
        order.
        """
        segments = chapter.text_segments
        for logical_start in self.completed_logical_starts_in_range(
            segments,
            start,
            count,
        ):
            self.align_segment_annotation(ci, chapter, logical_start, store)
