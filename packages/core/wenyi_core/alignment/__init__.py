"""Format-independent contracts for locating points and spans in immutable text.

An aligner reports evidence, not rendering policy. Unresolved items have no target
offset. Callers decide whether to omit them, retain a link fallback, or block export.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from ..errors import WenyiError


class AlignmentError(WenyiError, ValueError):
    """Alignment evidence cannot safely be mapped to the immutable target."""


@dataclass(frozen=True)
class AlignmentUnit:
    """Text plus point/range requests; optional object_reference describes an insertion.

    Offsets are Unicode character boundaries, not bytes. A point without a reference
    follows its translated referent; a point with object_reference locates an omitted
    object's insertion boundary. A range identifies translated source text.
    """

    unit_id: str
    source: str
    target: str
    items: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class AlignmentResult:
    """Digest-bound aligned/unresolved placements, in request order.

    Successful points may carry target_order to order coincident insertions. This
    ordinal describes target evidence, independently of request/source ordering.
    """

    unit_id: str
    target_digest: str
    placements: tuple[dict[str, Any], ...]
    has_unresolved: bool = False


class TextAligner(Protocol):
    """A replaceable locator; implementations never rewrite the supplied passages."""

    def align_unit(self, unit: AlignmentUnit) -> AlignmentResult: ...


def target_digest(text: str) -> str:
    """Bind character offsets to the exact target, without normalization."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def coincident_point_order(placements: Sequence[dict[str, Any]]) -> tuple[int, ...]:
    """Order explicit target-evidence slots at each boundary, leaving other slots intact.

    Points without target_order retain the caller's stable legacy order. Ordinals
    never move an object to a different character boundary or reorder note/range slots.
    """
    indices = list(range(len(placements)))
    groups: dict[int, list[int]] = {}
    for index, row in enumerate(placements):
        offset, order = row.get("target_start"), row.get("target_order")
        if type(offset) is int and type(order) is int and order >= 0:
            groups.setdefault(offset, []).append(index)
    for slots in groups.values():
        for slot, index in zip(slots, sorted(slots, key=lambda i: placements[i]["target_order"])):
            indices[slot] = index
    return tuple(indices)


def unresolved_alignment(unit: AlignmentUnit) -> AlignmentResult:
    """Report missing evidence without inventing a position or a format fallback."""
    return AlignmentResult(
        unit.unit_id,
        target_digest(unit.target),
        tuple(
            {
                "id": item["id"],
                "mode": item.get("mode"),
                "target_start": None,
                "target_end": None,
                "status": "unresolved",
                "method": "unresolved",
            }
            for item in unit.items
            if isinstance(item.get("id"), str) and item["id"]
        ),
        has_unresolved=bool(unit.items),
    )
