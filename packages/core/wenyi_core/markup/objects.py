"""Adapt serialized markup objects to the format-independent text alignment contract."""

from __future__ import annotations

from typing import Any

from ..alignment import AlignmentResult
from ..alignment.objects import ObjectAnchor

OBJECT_RELATION = "inline_object"
OBJECT_SCHEMA_VERSION = 1


def object_anchors(items: list[dict[str, Any]]) -> tuple[ObjectAnchor, ...]:
    """Keep DOM/format metadata out of the transport resolver."""
    return tuple(
        ObjectAnchor(item["id"], item["marker"], item["source_start"])
        for item in items
        if item.get("relation") == OBJECT_RELATION
    )


def pending_object(item: dict[str, Any], *, damaged: bool = False) -> dict[str, Any]:
    """Objects have no safe visual fallback; keep them pending without a target offset."""
    return {
        "id": item["id"],
        "mode": "point",
        "target_start": None,
        "target_end": None,
        "status": "pending",
        "method": "damaged_object_marker" if damaged else "unresolved_object",
    }


def annotation_placements(
    result: AlignmentResult, items: tuple[dict[str, Any], ...], target: str
) -> list[dict[str, Any]]:
    """Apply rendering policy outside the aligner: pending objects, paragraph-end notes."""
    by_id = {row["id"]: row for row in result.placements}
    placements = []
    for item in items:
        row = by_id.get(item["id"])
        if row is not None and row.get("status") == "aligned":
            placements.append(dict(row))
        elif item.get("relation") == OBJECT_RELATION:
            placements.append(pending_object(item))
        else:
            placements.append(
                {
                    "id": item["id"],
                    "mode": item["mode"],
                    "target_start": len(target),
                    "target_end": len(target),
                    "status": "fallback",
                    "method": "paragraph_end",
                }
            )
    return placements
