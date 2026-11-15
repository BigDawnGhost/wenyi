"""Adapt serialized markup objects to the format-independent text alignment contract."""

from __future__ import annotations

from typing import Any

from ..alignment import AlignmentResult, target_digest
from ..alignment.objects import ObjectAnchor, ObjectResolution

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


def export_object_placements(
    metadata: dict[str, Any], resolution: ObjectResolution
) -> list[dict[str, Any]]:
    """Keep only digest-bound positions or exact transport evidence on an export copy."""
    target = "".join(resolution.targets)
    rows = (
        metadata.get("placements", [])
        if metadata.get("target_digest") == target_digest(target)
        else []
    )
    copied = {row["id"]: row for row in resolution.placements}
    result = []
    for item in metadata.get("items", []):
        candidates = [row for row in rows if isinstance(row, dict) and row.get("id") == item["id"]]
        row = copied.get(item["id"]) or (candidates[0] if len(candidates) == 1 else {})
        if item.get("relation") != OBJECT_RELATION:
            if row:
                result.append(dict(row))
            continue
        start, end = row.get("target_start"), row.get("target_end")
        if (
            row.get("status") == "aligned"
            and row.get("mode") == "point"
            and type(start) is int
            and type(end) is int
            and start == end
            and 0 <= start <= len(target)
            and type(row.get("target_order", 0)) is int
            and row.get("target_order", 0) >= 0
        ):
            result.append(dict(row))
        else:
            result.append(pending_object(item))
    objects = {
        item["id"] for item in metadata.get("items", []) if item.get("relation") == OBJECT_RELATION
    }
    aligned = [row for row in result if row["id"] in objects and row["status"] == "aligned"]
    for order, row in enumerate(
        sorted(aligned, key=lambda row: (row["target_start"], row.get("target_order", len(result))))
    ):
        row["target_order"] = order
    return result


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
