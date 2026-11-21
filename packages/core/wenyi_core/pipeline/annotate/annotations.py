"""Annotation context, continuation reconstruction, alignment and safe fallback.
Map source point/range offsets to translation slices, reconstruct logical paragraphs only
after the final continuation is translated, and cache placements by target_digest. Process
logical paragraphs serially and persist each immediately. Record alignment failures as
events and continue.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ...alignment import AlignmentUnit, TextAligner, target_digest, unresolved_alignment
from ...alignment.object_repair import repair_object_markers
from ...alignment.objects import clean_object_markers, resolve_objects
from ...events import ProgressFn
from ...markup.contexts import annotation_metadata, reference_source
from ...markup.objects import (
    OBJECT_RELATION,
    annotation_placements,
    object_anchors,
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

    def align_segment_annotation(
        self,
        ci: int,
        chapter: Chapter,
        start_position: int,
        store: Storage,
        *,
        segments: list[Segment] | None = None,
        retry_fallbacks: bool = False,
    ) -> None:
        """Align format-owned annotations for one fully translated logical paragraph.
        Only the first slice has an anchor and parsing metadata, so wait for all
        continuations before merging source/target. Align against formal target text,
        using marker-free offsets for protected objects.
        Persist valid placements, note fallbacks and pending objects immediately. Skip model
        calls when annotations are absent or translation is incomplete.
        Repair mode retries only cached fallback items for the unchanged logical target.
        """
        if segments is None:
            segments = chapter.text_segments
        if not 0 <= start_position < len(segments):
            return
        while start_position > 0 and segments[start_position].cont:
            start_position -= 1
        segment = segments[start_position]
        metadata = annotation_metadata(segment)
        if not isinstance(metadata, dict):
            return
        raw_items = metadata.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            return

        logical_segments = self._logical_segments(segments, start_position)
        if any(item.target is None for item in logical_segments):
            return

        source = "".join(item.source for item in logical_segments)
        targets = tuple(item.target or "" for item in logical_segments)
        objects = [
            item
            for item in raw_items
            if isinstance(item, dict) and item.get("relation") == OBJECT_RELATION
        ]
        object_ids = {item["id"] for item in objects}
        resolution = resolve_objects(targets, object_anchors(objects), source)
        target = "".join(resolution.targets)
        copied = {row["id"]: row for row in resolution.placements}
        keep_pdf_markers = "pdf_annotations" in segment.meta and any(
            item.get("protected") for item in objects
        )
        if objects and not keep_pdf_markers:
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
        if cache_current and not retry_fallbacks and not copied and not objects:
            return

        items = tuple(dict(item) for item in raw_items if isinstance(item, dict))
        if retry_fallbacks:
            # Never bless stale offsets or repeat an already successful paid placement.
            if not objects and (not cache_current or not isinstance(placements, list)):
                return
            fallback_ids = {
                str(item.get("id"))
                for item in placements or []
                if isinstance(item, dict) and item.get("status") in {"fallback", "pending"}
            }
            items = tuple(
                item
                for item in items
                if not cache_current or item["id"] in fallback_ids or item["id"] in object_ids
            )
        retained = {
            str(item["id"]): dict(item)
            for item in placements or []
            if isinstance(item, dict) and digest_current
        }
        retained.update(copied)
        global_errors = [error for error in resolution.errors if "id" not in error]
        if global_errors and metadata.get("object_marker_errors") != global_errors:
            metadata["object_marker_errors"] = global_errors
            store.log_event(
                "object_marker_validation_failed",
                chapter=ci,
                segment=segment.index,
                errors=global_errors,
            )
        attempts = metadata.get("object_repairs", {})
        fingerprints = {
            item["id"]: target_digest(json.dumps([source, target, item], sort_keys=True))
            for item in objects
        }
        items = tuple(
            item
            for item in items
            if item["id"] not in copied
            and (
                (
                    retained.get(item["id"], {}).get("status") != "aligned"
                    and attempts.get(item["id"], {}).get("input_digest") != fingerprints[item["id"]]
                )
                if item["id"] in object_ids
                else (
                    item["id"] not in retained
                    or (retry_fallbacks and retained[item["id"]].get("status") != "aligned")
                )
            )
        )
        if not items:
            if copied or global_errors:
                metadata["target_digest"] = target_digest(target)
                metadata["placements"] = [
                    retained[str(item["id"])] for item in raw_items if str(item["id"]) in retained
                ]
                store.save_chapter(chapter)
            return
        anchor = segment.anchor or f"segment-{segment.index}"
        registry = store.load_annotation_contexts() if objects else None
        align_items = []
        for item in items:
            request = {key: item[key] for key in ("id", "mode", "source_start", "source_end")}
            if item.get("relation") == OBJECT_RELATION:
                request["object_reference"] = {
                    "kind": item.get("object_tag", item.get("kind", "")),
                    "source": reference_source(item, registry),
                }
                request["validation_errors"] = [
                    error for error in resolution.errors if error.get("id") in (None, item["id"])
                ] or [{"code": "unresolved_placement", "id": item["id"]}]
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
                result = (
                    repair_object_markers(unit, aligner) if objects else aligner.align_unit(unit)
                )
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
        if objects:
            # A completed failed attempt is resumable too: do not charge repeatedly for
            # unchanged input. A changed target or object definition permits a new attempt.
            for request in align_items:
                if request["id"] not in fingerprints:
                    continue
                attempts[request["id"]] = {
                    "input_digest": fingerprints[request["id"]],
                    "errors": request["validation_errors"],
                    "status": retained[request["id"]]["status"],
                    "reason": (
                        "disabled"
                        if not self._runtime.config.pipeline.annotation_alignment
                        else "aligned"
                        if retained[request["id"]]["status"] == "aligned"
                        else next(
                            (
                                row.get("error", "repair_unresolved")
                                for row in result.placements
                                if row["id"] == request["id"]
                            ),
                            "repair_unresolved",
                        )
                    ),
                }
            if self._runtime.config.pipeline.annotation_alignment:
                metadata["object_repairs"] = attempts
            # Preserve deterministic order for coincident objects, including repaired ones.
            for order, item in enumerate(raw_items):
                row = retained.get(item["id"])
                if item["id"] in object_ids and row and row.get("status") == "aligned":
                    row.setdefault("target_order", len(raw_items) + order)
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
            object_repairs=attempts if objects else {},
        )

    def repair_fallbacks(
        self, store: Storage, *, progress: ProgressFn | None = None, objects_only: bool = False
    ) -> None:
        """Make one separate, resumable pass over fallback annotations, including done chapters.

        The caller holds the book lock. Each repaired paragraph is persisted before
        proceeding. Ordinary note fallbacks wait for a later run; completed object
        attempts require changed input. Neither path loops over failures here.
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
            segments = chapter.text_segments
            for position, segment in enumerate(segments):
                metadata = annotation_metadata(segment)
                if segment.cont or not isinstance(metadata, dict):
                    continue
                if any(
                    isinstance(item, dict) and item.get("relation") == OBJECT_RELATION
                    for item in metadata.get("items", [])
                ):
                    self.align_segment_annotation(
                        ci, chapter, position, store, segments=segments, retry_fallbacks=True
                    )
                    continue
                if objects_only:
                    continue
                if "pdf_annotations" in segment.meta:
                    # PDF export cannot invent formula geometry or use stale positions.
                    # Refresh missing/stale placements on resume, never during read-only export.
                    target = segment.target or ""
                    if target and metadata.get("target_digest") != target_digest(target):
                        self.align_segment_annotation(
                            ci, chapter, position, store, segments=segments
                        )
                    # Unchanged PDF failures are best-effort omissions, not another paid
                    # repair pass. Other annotations in the same paragraph remain usable.
                    continue
                placements = metadata.get("placements")
                if not isinstance(placements, list) or not any(
                    isinstance(item, dict) and item.get("status") in {"fallback", "pending"}
                    for item in placements
                ):
                    continue
                self.align_segment_annotation(
                    ci, chapter, position, store, segments=segments, retry_fallbacks=True
                )
            if progress:
                progress(done, len(chapter_indices), label)

    @staticmethod
    def _logical_segments(segments: list[Segment], position: int) -> list[Segment]:
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
        *,
        segments: list[Segment] | None = None,
    ) -> None:
        """Process completed annotated paragraphs in source order.

        If supplied, segments must be the nonempty view of this chapter instance.
        """
        if segments is None:
            segments = chapter.text_segments
        for logical_start in self.completed_logical_starts_in_range(
            segments,
            start,
            count,
        ):
            self.align_segment_annotation(ci, chapter, logical_start, store, segments=segments)
