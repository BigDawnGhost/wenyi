"""Locate EPUB annotation links in an immutable translated text.

The source parser records where a footnote marker or annotated source range
appeared.  Translation changes character offsets, so this agent asks an LLM to
copy a set of synthetic markers into the already-finished target.  The target
itself is treated as immutable: local validation rejects a response if removing
the synthetic markers does not reproduce the target byte-for-byte.

This module deliberately returns deterministic fallback placements instead of
raising.  Link preservation is useful metadata, but a transient alignment error
must neither alter nor discard a completed translation.
"""

from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

from .base import Agent

_MARKER_RE = re.compile(r"⟪([^⟪⟫]+)⟫")

_SYSTEM_PROMPT = """\
You align EPUB annotation markers between a source passage and its completed translation.
The target text is immutable. You may ONLY insert the supplied markers into it:
- never add, remove, replace, reorder, normalize, or re-punctuate any target character;
- preserve every whitespace and line-break character exactly;
- insert every supplied marker exactly once and do not invent markers;
- a range marker :S must occur before its matching :E marker;
- ranges must be nested or disjoint, never crossing.

Return JSON only. Preserve every input unit_id exactly and return one item per input unit:
{"items":[{"unit_id":"the unchanged unit id","marked_target":"the exact target with markers inserted"}]}
"""


class AnnotationAlignmentError(ValueError):
    """The model output cannot safely be mapped back to the immutable target."""


@dataclass(frozen=True)
class AnnotationUnit:
    """One logical EPUB text block and the annotations found in its source."""

    unit_id: str
    source: str
    target: str
    items: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class AnnotationAlignment:
    """Validated target offsets for every annotation in one logical block."""

    unit_id: str
    target_digest: str
    placements: tuple[dict[str, Any], ...]
    used_fallback: bool = False


def target_digest(text: str) -> str:
    """Return the stable digest used to detect stale alignment metadata."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _item_id(item: dict[str, Any]) -> str:
    value = item.get("id")
    if not isinstance(value, str) or not value or any(char in value for char in "⟪⟫"):
        raise AnnotationAlignmentError("invalid_annotation_id")
    return value


def _item_mode(item: dict[str, Any]) -> str:
    mode = item.get("mode")
    if mode not in {"point", "range"}:
        raise AnnotationAlignmentError("invalid_annotation_mode")
    return str(mode)


def _source_offset(item: dict[str, Any], field: str, source_length: int) -> int:
    value = item.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise AnnotationAlignmentError(f"invalid_{field}")
    if not 0 <= value <= source_length:
        raise AnnotationAlignmentError(f"out_of_bounds_{field}")
    return value


def marker_tokens(item: dict[str, Any]) -> tuple[str, ...]:
    """Return the immutable marker token(s) expected for an annotation item."""

    item_id = _item_id(item)
    mode = _item_mode(item)
    if mode == "point":
        return (f"⟪{item_id}⟫",)
    return (f"⟪{item_id}:S⟫", f"⟪{item_id}:E⟫")


def build_marked_source(unit: AnnotationUnit) -> str:
    """Insert synthetic markers at parser-provided source offsets.

    Insertions at the same boundary use a stable nesting order: range endings,
    point markers, and then range starts.  The exact source display is guidance
    for the model; target validation remains the source of truth.
    """

    source_length = len(unit.source)
    insertions: dict[int, list[tuple[int, str]]] = {}
    seen_ids: set[str] = set()
    for ordinal, item in enumerate(unit.items):
        item_id = _item_id(item)
        if item_id in seen_ids:
            raise AnnotationAlignmentError("duplicate_annotation_id")
        seen_ids.add(item_id)
        mode = _item_mode(item)
        start = _source_offset(item, "source_start", source_length)
        if mode == "point":
            end = _source_offset(item, "source_end", source_length)
            if end != start:
                raise AnnotationAlignmentError("point_has_nonzero_range")
            # Keep the original item order stable for coincident point markers.
            insertions.setdefault(start, []).append((10_000 + ordinal, f"⟪{item_id}⟫"))
            continue

        end = _source_offset(item, "source_end", source_length)
        if start > end:
            raise AnnotationAlignmentError("reversed_source_range")
        # At a shared boundary, close inner ranges before opening new ranges.
        insertions.setdefault(start, []).append((20_000 - end, f"⟪{item_id}:S⟫"))
        insertions.setdefault(end, []).append((-20_000 - start, f"⟪{item_id}:E⟫"))

    parts: list[str] = []
    for offset in range(source_length + 1):
        for _, marker in sorted(insertions.get(offset, []), key=lambda value: value[0]):
            parts.append(marker)
        if offset < source_length:
            parts.append(unit.source[offset])
    return "".join(parts)


def _expected_markers(unit: AnnotationUnit) -> tuple[dict[str, str], list[str]]:
    """Map marker payloads to item ids and preserve the expected marker order."""

    payload_to_id: dict[str, str] = {}
    expected: list[str] = []
    seen_ids: set[str] = set()
    for item in unit.items:
        item_id = _item_id(item)
        if item_id in seen_ids:
            raise AnnotationAlignmentError("duplicate_annotation_id")
        seen_ids.add(item_id)
        for marker in marker_tokens(item):
            payload = marker[1:-1]
            if payload in payload_to_id:
                raise AnnotationAlignmentError("duplicate_marker_payload")
            payload_to_id[payload] = item_id
            expected.append(payload)
    return payload_to_id, expected


def validate_marked_target(
    unit: AnnotationUnit,
    marked_target: str,
) -> tuple[dict[str, Any], ...]:
    """Validate model markers and convert them to target character offsets.

    Validation is intentionally strict.  In particular, stripping all markers
    must reproduce ``unit.target`` exactly, including whitespace and Unicode
    punctuation.  Invalid output raises :class:`AnnotationAlignmentError`; the
    public agent method catches it and returns deterministic fallbacks.
    """

    if not isinstance(marked_target, str):
        raise AnnotationAlignmentError("marked_target_not_string")

    _, expected = _expected_markers(unit)
    matches = list(_MARKER_RE.finditer(marked_target))
    actual = [match.group(1) for match in matches]
    if len(actual) != len(expected):
        raise AnnotationAlignmentError("marker_count_mismatch")
    if set(actual) != set(expected):
        unknown = set(actual) - set(expected)
        missing = set(expected) - set(actual)
        if unknown:
            raise AnnotationAlignmentError("unknown_marker")
        if missing:
            raise AnnotationAlignmentError("missing_marker")
        raise AnnotationAlignmentError("duplicate_marker")
    if any(actual.count(marker) != 1 for marker in expected):
        raise AnnotationAlignmentError("duplicate_marker")

    stripped = _MARKER_RE.sub("", marked_target)
    if stripped != unit.target:
        raise AnnotationAlignmentError("target_was_modified")

    offsets: dict[str, int] = {}
    removed_length = 0
    range_stack: list[str] = []
    range_ids = {_item_id(item) for item in unit.items if _item_mode(item) == "range"}
    for match in matches:
        payload = match.group(1)
        offsets[payload] = match.start() - removed_length
        removed_length += len(match.group(0))
        if payload.endswith(":S"):
            item_id = payload[:-2]
            if item_id not in range_ids:
                raise AnnotationAlignmentError("unexpected_range_start")
            range_stack.append(item_id)
        elif payload.endswith(":E"):
            item_id = payload[:-2]
            if not range_stack or range_stack[-1] != item_id:
                raise AnnotationAlignmentError("crossing_or_reversed_range")
            range_stack.pop()
    if range_stack:
        raise AnnotationAlignmentError("unclosed_range")

    placements: list[dict[str, Any]] = []
    for item in unit.items:
        item_id = _item_id(item)
        mode = _item_mode(item)
        if mode == "point":
            position = offsets[item_id]
            start = end = position
        else:
            start = offsets[f"{item_id}:S"]
            end = offsets[f"{item_id}:E"]
            if start > end:
                raise AnnotationAlignmentError("reversed_target_range")
        placements.append(
            {
                "id": item_id,
                "mode": mode,
                "target_start": start,
                "target_end": end,
                "status": "aligned",
                "method": "llm_markers",
            }
        )
    return tuple(placements)


def fallback_alignment(unit: AnnotationUnit) -> AnnotationAlignment:
    """Return deterministic placements that never depend on model output.

    Point annotations use their proportional source offset.  A source range has
    no safely inferable translated span, so it becomes a zero-width placement at
    the paragraph end; the writer can render its original marker as a link.
    """

    source_length = len(unit.source)
    target_length = len(unit.target)
    placements: list[dict[str, Any]] = []
    for item in unit.items:
        try:
            item_id = _item_id(item)
            mode = _item_mode(item)
            source_start = _source_offset(item, "source_start", source_length)
        except AnnotationAlignmentError:
            # Parser-produced metadata should always be valid.  If persisted
            # state was manually corrupted, omit only that unusable placement.
            continue
        if mode == "point":
            if source_length:
                position = min(
                    target_length,
                    (source_start * target_length + source_length // 2) // source_length,
                )
            else:
                position = 0
            method = "proportional_source_offset"
        else:
            position = target_length
            method = "paragraph_end"
        placements.append(
            {
                "id": item_id,
                "mode": mode,
                "target_start": position,
                "target_end": position,
                "status": "fallback",
                "method": method,
            }
        )
    return AnnotationAlignment(
        unit_id=unit.unit_id,
        target_digest=target_digest(unit.target),
        placements=tuple(placements),
        used_fallback=True,
    )


class AnnotationAligner(Agent):
    """Ask the cheap model tier to locate annotations without editing text."""

    @staticmethod
    def _empty_alignment(unit: AnnotationUnit) -> AnnotationAlignment:
        """Return the no-op result for a logical block without annotations."""

        return AnnotationAlignment(
            unit_id=unit.unit_id,
            target_digest=target_digest(unit.target),
            placements=(),
        )

    def align_units(self, units: list[AnnotationUnit]) -> list[AnnotationAlignment]:
        """Align multiple logical blocks, splitting multi-annotation blocks per item.

        A block with N>1 annotations used to go through a single model call
        that had to reproduce the whole immutable target byte-for-byte *and*
        place all N markers correctly at once; any single mistake (even in one
        unrelated marker) invalidated the whole block. That all-or-nothing
        contract is why annotation-dense books (e.g. a heavily footnoted
        classic where nearly every paragraph carries a note) tend to fall back
        to paragraph-end placement far more than the per-annotation model
        error rate would suggest.

        Blocks with more than one annotation are instead split into one
        independent, concurrently issued request per annotation: each request
        only has to place a single marker, so a bad response only costs that
        one note instead of every note sharing its paragraph. Concurrency
        (bounded by ``pipeline.annotation_alignment_concurrency``) keeps
        wall-clock time from growing with the number of notes per block.
        Results preserve input order regardless of which path a block took.
        """

        if not units:
            return []

        results: list[AnnotationAlignment | None] = [None] * len(units)
        direct_indices = [index for index, unit in enumerate(units) if len(unit.items) <= 1]
        split_indices = [index for index, unit in enumerate(units) if len(unit.items) > 1]

        if direct_indices:
            direct_units = [units[index] for index in direct_indices]
            for index, result in zip(direct_indices, self._align_batch(direct_units)):
                results[index] = result

        for index in split_indices:
            results[index] = self._align_split_unit(units[index])

        return [
            result if result is not None else fallback_alignment(units[index])
            for index, result in enumerate(results)
        ]

    def _align_split_unit(self, unit: AnnotationUnit) -> AnnotationAlignment:
        """Align every annotation of one block through its own concurrent request.

        Validates ids up front (same integrity check ``build_marked_source``
        would otherwise raise on) so a corrupt item still degrades the whole
        block deterministically instead of partially. Each well-formed item is
        then aligned independently: a mistake in one annotation's response
        never affects its siblings.
        """

        try:
            _expected_markers(unit)
        except AnnotationAlignmentError:
            return fallback_alignment(unit)

        workers = max(1, self.config.pipeline.annotation_alignment_concurrency)
        placements_by_id: dict[str, dict[str, Any]] = {}
        used_fallback = False
        with ThreadPoolExecutor(max_workers=min(workers, len(unit.items))) as executor:
            futures = {
                executor.submit(self._align_single_item, unit, item): _item_id(item)
                for item in unit.items
            }
            for future in as_completed(futures):
                item_id = futures[future]
                placement, item_used_fallback = future.result()
                placements_by_id[item_id] = placement
                used_fallback = used_fallback or item_used_fallback

        placements = tuple(placements_by_id[_item_id(item)] for item in unit.items)
        return AnnotationAlignment(
            unit_id=unit.unit_id,
            target_digest=target_digest(unit.target),
            placements=placements,
            used_fallback=used_fallback,
        )

    def _align_single_item(
        self, unit: AnnotationUnit, item: dict[str, Any]
    ) -> tuple[dict[str, Any], bool]:
        """Align exactly one annotation against the unchanged immutable target."""

        pseudo_unit = AnnotationUnit(
            unit_id=unit.unit_id,
            source=unit.source,
            target=unit.target,
            items=(item,),
        )
        [alignment] = self._align_batch([pseudo_unit])
        return alignment.placements[0], alignment.used_fallback

    def _align_batch(self, units: list[AnnotationUnit]) -> list[AnnotationAlignment]:
        """Align multiple logical blocks in one model call.

        Results preserve input order.  A missing, duplicate, or invalid response
        item falls back only for its corresponding unit.  Transport, JSON, or
        outer-schema failures fall back every unit that required alignment.
        """

        if not units:
            return []

        unit_ids = [unit.unit_id for unit in units]
        if any(not isinstance(unit_id, str) or not unit_id for unit_id in unit_ids):
            return [fallback_alignment(unit) for unit in units]
        if len(set(unit_ids)) != len(unit_ids):
            # Duplicate ids make response ownership ambiguous; do not call the
            # model and preserve one deterministic result per input unit.
            return [fallback_alignment(unit) for unit in units]

        results: list[AnnotationAlignment | None] = [None] * len(units)
        request_items: list[dict[str, str]] = []
        requested_indices: list[int] = []
        for index, unit in enumerate(units):
            if not unit.items:
                results[index] = self._empty_alignment(unit)
                continue
            try:
                marked_source = build_marked_source(unit)
            except Exception:  # noqa: BLE001 - corrupt metadata affects only this unit
                results[index] = fallback_alignment(unit)
                continue
            request_items.append(
                {
                    "unit_id": unit.unit_id,
                    "source_with_markers": marked_source,
                    "immutable_target": unit.target,
                }
            )
            requested_indices.append(index)

        if not request_items:
            return [
                result if result is not None else fallback_alignment(units[index])
                for index, result in enumerate(results)
            ]

        payload = json.dumps({"items": request_items}, ensure_ascii=False)
        aggregate_target_length = sum(len(units[index].target) for index in requested_indices)
        max_tokens = max(
            1024,
            min(8192, aggregate_target_length * 2 + len(requested_indices) * 64 + 512),
        )
        try:
            data = self._ask_json(
                _SYSTEM_PROMPT,
                "Insert the source markers at the corresponding positions in each immutable "
                "target. Return exactly one output item for every input item and no other "
                f"content.\n\nINPUT JSON:\n{payload}",
                tier="cheap",
                default={},
                max_tokens=max_tokens,
            )
            if not isinstance(data, dict) or not isinstance(data.get("items"), list):
                raise AnnotationAlignmentError("response_not_items_object")
            response_items = data["items"]
        except Exception:  # noqa: BLE001 - an unusable batch response falls back safely
            for index in requested_indices:
                results[index] = fallback_alignment(units[index])
            return [
                result if result is not None else fallback_alignment(units[index])
                for index, result in enumerate(results)
            ]

        expected_ids = {units[index].unit_id for index in requested_indices}
        responses_by_id: dict[str, list[dict[str, Any]]] = {unit_id: [] for unit_id in expected_ids}
        for response_item in response_items:
            if not isinstance(response_item, dict):
                continue
            response_unit_id = response_item.get("unit_id")
            if isinstance(response_unit_id, str) and response_unit_id in responses_by_id:
                responses_by_id[response_unit_id].append(response_item)

        for index in requested_indices:
            unit = units[index]
            candidates = responses_by_id[unit.unit_id]
            if len(candidates) != 1:
                results[index] = fallback_alignment(unit)
                continue
            marked_target = candidates[0].get("marked_target")
            if not isinstance(marked_target, str):
                results[index] = fallback_alignment(unit)
                continue
            try:
                placements = validate_marked_target(unit, marked_target)
            except Exception:  # noqa: BLE001 - one malformed item must not poison its peers
                results[index] = fallback_alignment(unit)
                continue
            results[index] = AnnotationAlignment(
                unit_id=unit.unit_id,
                target_digest=target_digest(unit.target),
                placements=placements,
            )

        return [
            result if result is not None else fallback_alignment(units[index])
            for index, result in enumerate(results)
        ]

    def align_unit(self, unit: AnnotationUnit) -> AnnotationAlignment:
        """Align one logical block through the shared batched implementation."""

        return self.align_units([unit])[0]


__all__ = [
    "AnnotationAligner",
    "AnnotationAlignment",
    "AnnotationAlignmentError",
    "AnnotationUnit",
    "build_marked_source",
    "fallback_alignment",
    "marker_tokens",
    "target_digest",
    "validate_marked_target",
]
