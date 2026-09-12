"""EPUB TOC boundary mapping and logical chapter construction."""

from __future__ import annotations

from trans_novel.epub.navigation import select_boundaries
from trans_novel.ingest.models import KIND_HEADING, Chapter, Segment

_STRATEGY_SPINE_FALLBACK = "spine-fallback"


def _semantic_hints(segments: list[Segment]) -> list[str]:
    return sorted(
        {
            hint
            for segment in segments
            for hint in segment.meta.get("semantic_hints", [])
            if isinstance(hint, str)
        }
    )


def _collect_segments(
    resources: list[dict[str, object]],
) -> tuple[
    list[Segment], dict[str, int], dict[str, dict[str, object]], dict[str, dict[str, object]]
]:
    all_segments: list[Segment] = []
    anchor_positions: dict[str, int] = {}
    resource_starts: dict[str, int] = {}
    resource_by_href: dict[str, dict[str, object]] = {}
    for resource in resources:
        href = str(resource["href"])
        resource_by_href[href] = resource
        resource_starts[href] = len(all_segments)
        raw_segments = resource.get("segments")
        segments = raw_segments if isinstance(raw_segments, list) else []
        for segment in segments:
            if not isinstance(segment, Segment):
                continue
            if segment.anchor:
                anchor_positions[segment.anchor] = len(all_segments)
            all_segments.append(segment)
    return all_segments, anchor_positions, resource_starts, resource_by_href


def _map_entry_boundaries(
    toc_entries: list[dict[str, object]],
    *,
    anchor_positions: dict[str, int],
    resource_starts: dict[str, int],
    resource_by_href: dict[str, dict[str, object]],
) -> None:
    for entry in toc_entries:
        href = entry.get("resource_href")
        if not isinstance(href, str) or href not in resource_starts:
            continue
        fragment = entry.get("fragment")
        has_fragment = isinstance(fragment, str) and bool(fragment)
        resource = resource_by_href[href]
        raw_fragment_map = resource.get("fragment_anchors")
        fragment_map = raw_fragment_map if isinstance(raw_fragment_map, dict) else {}
        if has_fragment and fragment not in fragment_map:
            # A broken fragment must not silently become the resource start.
            continue
        segment_anchor = fragment_map.get(fragment) if has_fragment else None
        if not has_fragment:
            raw_segments = resource.get("segments")
            resource_segments = raw_segments if isinstance(raw_segments, list) else []
            first = next(
                (segment for segment in resource_segments if isinstance(segment, Segment)), None
            )
            segment_anchor = first.anchor if first is not None else None
        if isinstance(segment_anchor, str) and segment_anchor in anchor_positions:
            entry["segment_anchor"] = segment_anchor
            entry["boundary_position"] = anchor_positions[segment_anchor]
        elif has_fragment:
            raw_segments = resource.get("segments")
            segment_count = (
                sum(isinstance(segment, Segment) for segment in raw_segments)
                if isinstance(raw_segments, list)
                else 0
            )
            entry["boundary_position"] = resource_starts[href] + segment_count
        else:
            entry["boundary_position"] = resource_starts[href]


def _inherit_group_boundaries(toc_entries: list[dict[str, object]]) -> None:
    toc_paths = {
        str(entry.get("toc_path"))
        for entry in toc_entries
        if isinstance(entry.get("toc_path"), str) and entry.get("toc_path")
    }
    for toc_path in toc_paths:
        path_entries = [entry for entry in toc_entries if entry.get("toc_path") == toc_path]
        children: dict[int, list[dict[str, object]]] = {}
        for entry in path_entries:
            parent_index = entry.get("parent_index")
            if isinstance(parent_index, int):
                children.setdefault(parent_index, []).append(entry)
        for entry in reversed(path_entries):
            if isinstance(entry.get("boundary_position"), int) or entry.get("raw_href"):
                continue
            node_index = entry.get("node_index")
            if not isinstance(node_index, int):
                continue
            descendant = next(
                (
                    child
                    for child in children.get(node_index, [])
                    if isinstance(child.get("boundary_position"), int)
                ),
                None,
            )
            if descendant is not None:
                entry["boundary_position"] = descendant["boundary_position"]
                entry["inherited_boundary_from"] = descendant.get("entry_id")


def _select_toc_boundaries(
    toc_entries: list[dict[str, object]], all_segments: list[Segment]
) -> tuple[list[dict[str, object]], int, str]:
    ordered_toc_paths = list(
        dict.fromkeys(
            str(entry.get("toc_path"))
            for entry in toc_entries
            if isinstance(entry.get("toc_path"), str) and entry.get("toc_path")
        )
    )
    segment_lengths = [len(segment.source) for segment in all_segments]
    for toc_path in ordered_toc_paths:
        candidates, depth = select_boundaries(
            [entry for entry in toc_entries if entry.get("toc_path") == toc_path], segment_lengths
        )
        if candidates:
            candidates.sort(key=lambda item: int(item["boundary_position"]))
            return candidates, depth, toc_path
    return [], 0, ""


def _spine_fallback(resources: list[dict[str, object]]) -> list[Chapter]:
    chapters: list[Chapter] = []
    for resource in resources:
        raw_segments = resource.get("segments")
        segments = (
            [segment for segment in raw_segments if isinstance(segment, Segment)]
            if isinstance(raw_segments, list)
            else []
        )
        if not segments:
            continue
        for index, segment in enumerate(segments):
            segment.index = index
        chapters.append(
            Chapter(
                index=len(chapters),
                title=str(resource.get("title") or ""),
                segments=segments,
                href=str(resource.get("href") or "") or None,
                meta={
                    "epub_split_strategy": _STRATEGY_SPINE_FALLBACK,
                    "semantic_hints": _semantic_hints(segments),
                },
            )
        )
    return chapters


def _chapter_slices(
    boundaries: list[dict[str, object]], segment_count: int
) -> list[tuple[int, int, dict[str, object] | None]]:
    slices: list[tuple[int, int, dict[str, object] | None]] = []
    first_position = int(boundaries[0]["boundary_position"])
    if first_position > 0:
        slices.append((0, first_position, None))
    for index, boundary in enumerate(boundaries):
        start = int(boundary["boundary_position"])
        end = (
            int(boundaries[index + 1]["boundary_position"])
            if index + 1 < len(boundaries)
            else segment_count
        )
        if end > start:
            slices.append((start, end, boundary))
    return slices


def _build_chapters(
    all_segments: list[Segment],
    slices: list[tuple[int, int, dict[str, object] | None]],
    strategy: str,
) -> list[Chapter]:
    chapters: list[Chapter] = []
    for start, end, boundary in slices:
        segments = all_segments[start:end]
        for index, segment in enumerate(segments):
            segment.index = index
        if boundary is not None:
            title = str(boundary.get("title") or "")
            toc_entry_id = boundary.get("entry_id")
            first_href = segments[0].resource_href or str(boundary.get("resource_href") or "")
        else:
            first_href = segments[0].resource_href or ""
            title = segments[0].source if segments[0].kind == KIND_HEADING else ""
            toc_entry_id = None
        meta: dict[str, object] = {"epub_split_strategy": strategy}
        if isinstance(toc_entry_id, str):
            meta["toc_entry_id"] = toc_entry_id
        meta["semantic_hints"] = _semantic_hints(segments)
        chapters.append(
            Chapter(
                index=len(chapters),
                title=title,
                segments=segments,
                href=first_href or None,
                meta=meta,
            )
        )
    return chapters


def logical_chapters(
    resources: list[dict[str, object]], toc_entries: list[dict[str, object]]
) -> tuple[list[Chapter], str, str]:
    """Split physical-resource segments into logical chapters using local TOC rules."""
    all_segments, anchor_positions, resource_starts, resource_by_href = _collect_segments(resources)
    _map_entry_boundaries(
        toc_entries,
        anchor_positions=anchor_positions,
        resource_starts=resource_starts,
        resource_by_href=resource_by_href,
    )
    _inherit_group_boundaries(toc_entries)
    boundaries, selected_depth, canonical_toc_path = _select_toc_boundaries(
        toc_entries, all_segments
    )
    if not boundaries:
        return _spine_fallback(resources), _STRATEGY_SPINE_FALLBACK, canonical_toc_path
    strategy = f"toc-depth-{selected_depth}"
    return (
        _build_chapters(all_segments, _chapter_slices(boundaries, len(all_segments)), strategy),
        strategy,
        canonical_toc_path,
    )
