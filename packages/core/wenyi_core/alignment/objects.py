"""Lossless protected-object transport, independent of formats, storage and models."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

OBJECT_PREFIX = "〘object:"
# IDs are opaque transport tokens, never arbitrary prose or nested delimiters.
_OBJECT_ID = re.compile(r"[A-Za-z0-9_.:-]+")
_OBJECT_MARKER = re.compile(r"〘object:[A-Za-z0-9_.:-]+〙")


@dataclass(frozen=True)
class ObjectAnchor:
    """One immutable object owned by the caller; source_offset excludes object content."""

    id: str
    marker: str
    source_offset: int


@dataclass(frozen=True)
class ObjectResolution:
    """Cleaned slices and direct placements, or unchanged slices when transport is damaged.

    Missing, duplicated or changed complete tokens require semantic alignment.
    Malformed transport preserves the entire logical response and must not be aligned:
    an immutable-target aligner cannot repair prose or infer a missing token boundary.
    """

    targets: tuple[str, ...]
    placements: tuple[dict[str, Any], ...] = ()
    damaged: bool = False


def object_marker(object_id: str, source: str) -> str:
    """Choose a deterministic token that cannot consume literal source content."""
    if not _OBJECT_ID.fullmatch(object_id):
        raise ValueError("Protected object IDs must use ASCII letters, digits, or _ . : -")
    marker = f"{OBJECT_PREFIX}{object_id}〙"
    while marker in source:
        object_id += "_x"
        marker = f"{OBJECT_PREFIX}{object_id}〙"
    return marker


def marked_sources(sources: list[str], contexts: list[list[dict[str, str]]]) -> list[str]:
    """Insert caller-owned markers at slice-local boundaries without changing sources."""
    if len(sources) != len(contexts):
        raise ValueError("Protected object context count does not match source count")
    result = []
    for source, references in zip(sources, contexts):
        insertions: dict[int, list[str]] = {}
        for reference in references:
            marker = reference.get("marker")
            if marker is not None:
                offset = int(reference["source_offset"])
                if not 0 <= offset <= len(source) or not _OBJECT_MARKER.fullmatch(marker):
                    raise ValueError("Invalid protected object context")
                insertions.setdefault(offset, []).append(marker)
        result.append(
            "".join(
                "".join(insertions.get(offset, []))
                + (source[offset] if offset < len(source) else "")
                for offset in range(len(source) + 1)
            )
            if insertions
            else source
        )
    return result


def _scan(target: str, source: str) -> tuple[list[re.Match[str]], bool]:
    """Recognize complete tokens; no match may cross prose, whitespace or a new opener."""
    matches = list(_OBJECT_MARKER.finditer(target))
    starts = {match.start() for match in matches}
    damaged = any(match.start() not in starts for match in re.finditer(OBJECT_PREFIX, target))
    return [match for match in matches if match.group() not in source], damaged


def _remove_tokens(targets: Sequence[str], matches: list[re.Match[str]]) -> tuple[str, ...]:
    """Remove validated token intervals, even across continuation boundaries."""
    result = []
    start = 0
    for target in targets:
        end = start + len(target)
        cursor = start
        parts = []
        for match in matches:
            if match.end() <= start or match.start() >= end:
                continue
            cut_start, cut_end = max(start, match.start()), min(end, match.end())
            parts.append(target[cursor - start : cut_start - start])
            cursor = cut_end
        parts.append(target[cursor - start :])
        result.append("".join(parts))
        start = end
    return tuple(result)


def clean_object_markers(targets: Sequence[str], source: str) -> tuple[str, ...]:
    """Clean only unambiguous transport; malformed responses are preserved verbatim."""
    matches, damaged = _scan("".join(targets), source)
    return tuple(targets) if damaged else _remove_tokens(targets, matches)


def resolve_objects(
    targets: Sequence[str], anchors: Sequence[ObjectAnchor], source: str
) -> ObjectResolution:
    """Consume unique exact tokens and retain their target order at coincident boundaries."""
    if not anchors:
        return ObjectResolution(tuple(targets))
    if len({anchor.id for anchor in anchors}) != len(anchors) or len(
        {anchor.marker for anchor in anchors}
    ) != len(anchors):
        raise ValueError("Protected object IDs and markers must be unique")
    for anchor in anchors:
        if (
            not anchor.id
            or not _OBJECT_MARKER.fullmatch(anchor.marker)
            or anchor.marker in source
            or type(anchor.source_offset) is not int
            or not 0 <= anchor.source_offset <= len(source)
        ):
            raise ValueError("Invalid protected object anchor")
    target = "".join(targets)
    matches, damaged = _scan(target, source)
    if damaged:
        return ObjectResolution(tuple(targets), damaged=True)
    positions: dict[str, list[tuple[int, int]]] = {}
    removed = 0
    for order, match in enumerate(matches):
        positions.setdefault(match.group(), []).append((match.start() - removed, order))
        removed += len(match.group())
    placements = []
    for source_order, anchor in enumerate(anchors):
        occurrences = positions.get(anchor.marker, [])
        if len(occurrences) == 1:
            offset, order = occurrences[0]
            method = "copied_object_marker"
        elif not matches and target == source:
            offset, order = anchor.source_offset, source_order
            method = "unchanged_source"
        else:
            continue
        placements.append(
            {
                "id": anchor.id,
                "mode": "point",
                "target_start": offset,
                "target_end": offset,
                "target_order": order,
                "status": "aligned",
                "method": method,
            }
        )
    return ObjectResolution(_remove_tokens(targets, matches), tuple(placements))
