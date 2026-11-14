"""Annotation context, continuation reconstruction, alignment and safe fallback.
Map source point/range offsets to translation slices, reconstruct logical paragraphs only
after the final continuation is translated, and cache placements by target_digest. Process
logical paragraphs serially and persist each immediately. Record alignment failures as
events and continue.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...alignment import AlignmentUnit, TextAligner, target_digest, unresolved_alignment
from ...alignment.objects import clean_object_markers, resolve_objects
from ...events import ProgressFn
from ...markup.objects import (
    OBJECT_RELATION,
    annotation_placements,
    object_anchors,
    pending_object,
)
from ...models.document import Chapter, Segment
from ...storage.protocol import Storage

if TYPE_CHECKING:
    from ..run.runtime import PipelineRuntime


class AnnotationService:
    """Domain service for annotation alignment and continuation reconstruction."""

    def __init__(self, runtime: PipelineRuntime, *, aligner: TextAligner | None = None):
        self._runtime = runtime
        self._aligner = aligner

    @staticmethod
    def annotation_metadata(segment: Segment) -> dict[str, Any] | None:
        """Select the format-owned metadata consumed by the shared point/range aligner."""
        for key in ("epub_annotations", "pdf_annotations"):
            metadata = segment.meta.get(key)
            if isinstance(metadata, dict):
                return metadata
        return None

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
        """Assign note/object references to actual translation slices by source offsets.
        Only the first slice holds EPUB layout metadata. Use its original offsets and
        cumulative slice boundaries to assign point annotations to one slice and ranges to
        every intersecting slice. Inject each destination only once per slice.
        """
        assigned: list[list[dict[str, str]]] = [[] for _ in segments]
        raw_contexts = registry.get("contexts") if isinstance(registry, dict) else None
        if not isinstance(raw_contexts, dict):
            raw_contexts = {}

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

            metadata = AnnotationService.annotation_metadata(logical_segments[0])
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
                if not isinstance(raw_item, dict) or raw_item.get("relation") not in {
                    "noteref",
                    OBJECT_RELATION,
                }:
                    continue
                target_key = raw_item.get("target_key")
                if not isinstance(target_key, str) or not target_key:
                    continue
                record = raw_contexts.get(target_key, {})
                if not isinstance(record, dict):
                    record = {}
                raw_blocks = record.get("source_blocks")
                blocks = (
                    [block for block in raw_blocks if isinstance(block, str) and block.strip()]
                    if isinstance(raw_blocks, list)
                    else []
                )
                if not blocks and raw_item.get("protected") and raw_item.get("object_source"):
                    blocks = [raw_item["object_source"]]
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
                    reference = dict(note)
                    if raw_item.get("relation") == OBJECT_RELATION:
                        reference.update(
                            marker=raw_item["marker"],
                            source_offset=str(start - boundaries[piece_index][0]),
                        )
                    assigned[logical_start + piece_index].append(reference)

            position = logical_end
        return assigned

    def align_segment_annotation(
        self,
        ci: int,
        chapter: Chapter,
        start_position: int,
        store: Storage,
        *,
        retry_fallbacks: bool = False,
    ) -> None:
        """Align format-owned annotations for one fully translated logical paragraph.
        Only the first slice has an anchor and parsing metadata, so wait for all
        continuations before merging source/target. Align against formal target text; export
        punctuation normalization remaps placements only on a copy.
        Persist valid placements, note fallbacks and pending objects immediately. Skip model
        calls when annotations are absent or translation is incomplete.
        Repair mode retries only cached fallback items for the unchanged logical target.
        """
        segments = chapter.text_segments
        if not 0 <= start_position < len(segments):
            return
        while start_position > 0 and segments[start_position].cont:
            start_position -= 1
        segment = segments[start_position]
        metadata = self.annotation_metadata(segment)
        if not isinstance(metadata, dict):
            return
        raw_items = metadata.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            return

        logical_segments = self._logical_segments(chapter, start_position)
        if any(item.target is None for item in logical_segments):
            return

        source = "".join(item.source for item in logical_segments)
        targets = tuple(item.target or "" for item in logical_segments)
        objects = [
            item
            for item in raw_items
            if isinstance(item, dict) and item.get("relation") == OBJECT_RELATION
        ]
        resolution = resolve_objects(targets, object_anchors(objects), source)
        target = "".join(resolution.targets)
        copied = {row["id"]: row for row in resolution.placements}
        strict_pdf = "pdf_annotations" in segment.meta and any(
            item.get("protected") for item in objects
        )
        if resolution.damaged:
            # No immutable-target locator can repair broken transport. Preserve the complete
            # response and stop here, including on resume, rather than paying for unusable offsets.
            retained = {
                row["id"]: row
                for row in metadata.get("placements", [])
                if isinstance(row, dict) and metadata.get("target_digest") == target_digest(target)
            }
            retained.update({item["id"]: pending_object(item, damaged=True) for item in objects})
            placements = [retained[item["id"]] for item in raw_items if item["id"] in retained]
            if (
                metadata.get("target_digest") != target_digest(target)
                or metadata.get("placements") != placements
            ):
                metadata.update(target_digest=target_digest(target), placements=placements)
                store.save_chapter(chapter)
                store.log_event(
                    "annotation_alignment_skipped",
                    chapter=ci,
                    segment=segment.index,
                    reason="damaged_object_marker",
                    pending_objects=[item["id"] for item in objects],
                )
            return
        if strict_pdf and len(copied) != len(objects):
            metadata.update(
                target_digest=target_digest(target),
                placements=[copied.get(item["id"], pending_object(item)) for item in objects],
            )
            store.save_chapter(chapter)
            return
        if objects and not strict_pdf:
            before_polish = clean_object_markers(
                [item.target_before_polish or "" for item in logical_segments], source
            )
            for item, cleaned, before in zip(logical_segments, resolution.targets, before_polish):
                item.target = cleaned
                if item.target_before_polish is not None:
                    item.target_before_polish = before
        expected_ids = {
            str(item.get("id")) for item in raw_items if isinstance(item, dict) and item.get("id")
        }
        placements = metadata.get("placements")
        placement_ids = {
            str(item.get("id"))
            for item in placements or []
            if isinstance(item, dict) and item.get("id")
        }
        digest_current = metadata.get("target_digest") == target_digest(target)
        cache_current = digest_current and expected_ids and placement_ids == expected_ids
        if cache_current and not retry_fallbacks and not copied:
            return

        items = tuple(dict(item) for item in raw_items if isinstance(item, dict))
        if retry_fallbacks:
            # Never bless stale offsets or repeat an already successful paid placement.
            if not cache_current or not isinstance(placements, list):
                return
            fallback_ids = {
                str(item.get("id"))
                for item in placements
                if isinstance(item, dict) and item.get("status") in {"fallback", "pending"}
            }
            items = tuple(item for item in items if str(item.get("id")) in fallback_ids)
        retained = {
            str(item["id"]): dict(item)
            for item in placements or []
            if isinstance(item, dict) and digest_current
        }
        retained.update(copied)
        items = tuple(
            item
            for item in items
            if item["id"] not in copied and (retry_fallbacks or item["id"] not in retained)
        )
        if not items:
            if copied:
                metadata["target_digest"] = target_digest(target)
                metadata["placements"] = [
                    retained[str(item["id"])] for item in raw_items if str(item["id"]) in retained
                ]
                store.save_chapter(chapter)
            return
        anchor = segment.anchor or f"segment-{segment.index}"
        if objects:
            registry = store.load_annotation_contexts() or {}
            contexts = registry.get("contexts", {})
            for item in items:
                reference = contexts.get(item.get("target_key"), {})
                if item.get("relation") == OBJECT_RELATION and reference.get("source_blocks"):
                    item["object_source"] = "\n".join(reference["source_blocks"])
        align_items = []
        for item in items:
            request = {key: item[key] for key in ("id", "mode", "source_start", "source_end")}
            if item.get("relation") == OBJECT_RELATION:
                request["object_reference"] = {
                    "kind": item.get("object_tag", ""),
                    "source": item.get("object_source", ""),
                }
            align_items.append(request)
        unit = AlignmentUnit(
            unit_id=f"ch{ci}:{anchor}",
            source=source,
            target=target,
            items=tuple(align_items),
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
            if not objects:
                return
            result = unresolved_alignment(unit)
        else:
            try:
                aligner = self._aligner or self._runtime.annotation_aligner
                result = aligner.align_unit(unit)
            except Exception as error:  # noqa: BLE001 - Preserve completed text and pending objects.
                store.log_event(
                    "annotation_alignment_failed",
                    chapter=ci,
                    segment=segment.index,
                    anchor=segment.anchor,
                    unit_id=unit.unit_id,
                    error=type(error).__name__,
                    detail=str(error),
                )
                if not objects:
                    return
                result = unresolved_alignment(unit)

        metadata["target_digest"] = result.target_digest
        retained.update({row["id"]: row for row in annotation_placements(result, items, target)})
        metadata["placements"] = [
            retained[str(item["id"])] for item in raw_items if str(item["id"]) in retained
        ]
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
            used_fallback=result.has_unresolved,
            retry_fallbacks=retry_fallbacks,
            pending_objects=[
                item["id"] for item in metadata["placements"] if item.get("status") == "pending"
            ],
        )

    def repair_fallbacks(self, store: Storage, *, progress: ProgressFn | None = None) -> None:
        """Make one separate, resumable pass over fallback annotations, including done chapters.

        The caller holds the book lock. Each repaired paragraph is persisted before
        proceeding; remaining fallbacks wait for a later run rather than looping here.
        """
        if not self._runtime.config.pipeline.annotation_alignment:
            return
        chapter_indices = sorted(
            chapter["index"]
            for chapter in store.load_manifest().get("chapters", [])
            if isinstance(chapter.get("index"), int)
        )
        label = "Repairing fallback annotations"
        if progress:
            progress(0, len(chapter_indices), label)
        for done, ci in enumerate(chapter_indices, 1):
            chapter = store.load_chapter(ci)
            for position, segment in enumerate(chapter.text_segments):
                metadata = self.annotation_metadata(segment)
                if segment.cont or not isinstance(metadata, dict):
                    continue
                if "pdf_annotations" in segment.meta:
                    # PDF export cannot invent formula geometry or use stale positions.
                    # Refresh missing/stale placements on resume, never during read-only export.
                    target = segment.target or ""
                    if target and metadata.get("target_digest") != target_digest(target):
                        self.align_segment_annotation(ci, chapter, position, store)
                    # Unchanged PDF failures are best-effort omissions, not another paid
                    # repair pass. Other annotations in the same paragraph remain usable.
                    continue
                if any(
                    isinstance(item, dict) and item.get("relation") == OBJECT_RELATION
                    for item in metadata.get("items", [])
                ):
                    target = "".join(
                        item.target or "" for item in self._logical_segments(chapter, position)
                    )
                    expected = {item["id"] for item in metadata.get("items", [])}
                    available = {item["id"] for item in metadata.get("placements", [])}
                    if (
                        metadata.get("target_digest") != target_digest(target)
                        or not expected <= available
                    ):
                        self.align_segment_annotation(ci, chapter, position, store)
                        continue
                placements = metadata.get("placements")
                if not isinstance(placements, list) or not any(
                    isinstance(item, dict) and item.get("status") in {"fallback", "pending"}
                    for item in placements
                ):
                    continue
                self.align_segment_annotation(ci, chapter, position, store, retry_fallbacks=True)
            if progress:
                progress(done, len(chapter_indices), label)

    @staticmethod
    def _logical_segments(chapter: Chapter, position: int) -> list[Segment]:
        segments = chapter.text_segments
        end = position + 1
        while end < len(segments) and segments[end].cont:
            end += 1
        return segments[position:end]

    def align_annotations_after_batch(
        self,
        ci: int,
        chapter: Chapter,
        start: int,
        count: int,
        store: Storage,
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
