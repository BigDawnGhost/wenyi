"""Bounded special-object repair through the shared immutable-target aligner.

Formats supply source objects and mechanical diagnostics in AlignmentUnit.items.
The model may locate an insertion boundary, never rewrite prose or object content.
This entry point has no format, storage, provider or pipeline dependencies.
"""

from __future__ import annotations

from . import AlignmentResult, AlignmentUnit, TextAligner, target_digest, unresolved_alignment


def repair_object_markers(unit: AlignmentUnit, aligner: TextAligner) -> AlignmentResult:
    """Try once, accepting only unique, digest-bound placements for requested objects."""
    result = aligner.align_unit(unit)
    unresolved = unresolved_alignment(unit)
    if result.unit_id != unit.unit_id or result.target_digest != target_digest(unit.target):
        return unresolved
    placements = []
    for item, fallback in zip(unit.items, unresolved.placements):
        candidates = [row for row in result.placements if row.get("id") == item["id"]]
        row = candidates[0] if len(candidates) == 1 else {}
        start, end = row.get("target_start"), row.get("target_end")
        if (
            row.get("status") == "aligned"
            and row.get("mode") == item["mode"]
            and type(start) is int
            and type(end) is int
            and 0 <= start <= end <= len(unit.target)
            and (start == end) == (item["mode"] == "point")
        ):
            placements.append(dict(row))
        else:
            placements.append({**fallback, "error": row.get("error", "invalid_repair_result")})
    return AlignmentResult(
        unit.unit_id,
        target_digest(unit.target),
        tuple(placements),
        any(row["status"] != "aligned" for row in placements),
    )
