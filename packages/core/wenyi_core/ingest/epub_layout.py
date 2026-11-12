"""Map physical EPUB resources into logical chapters and auxiliary note contexts."""

from __future__ import annotations

from wenyi_core.ingest.epub_chapters import get_chapter_split_strategy
from wenyi_core.ingest.epub_contexts import (
    _build_epub_annotation_contexts as _build_epub_annotation_contexts,
)
from wenyi_core.models.document import KIND_HEADING, Chapter, Segment


def _toc_collapsed_to_single_boundary(
    toc_entries: list[dict[str, object]],
    canonical_toc_path: str,
    resources: list[dict[str, object]],
) -> bool:
    """Detect TOCs collapsed to one boundary because all nodes target the same resource.
    Treat a TOC as degenerate only when it has multiple resolvable top-level nodes and the
    spine still contains multiple body-bearing resources. Then fall back to spine-resource
    chapter boundaries.
    """
    top_level = 0
    for entry in toc_entries:
        if entry.get("toc_path") != canonical_toc_path:
            continue
        if entry.get("depth") != 0 or entry.get("external"):
            continue
        position = entry.get("boundary_position")
        if isinstance(position, int) and position >= 0:
            top_level += 1
    if top_level < 2:
        return False
    non_empty_resources = 0
    for resource in resources:
        segments = resource.get("segments")
        if isinstance(segments, list) and any(isinstance(segment, Segment) for segment in segments):
            non_empty_resources += 1
    return non_empty_resources >= 2


def _logical_chapters(
    resources: list[dict[str, object]],
    toc_entries: list[dict[str, object]],
) -> tuple[list[Chapter], str, str]:
    """Split physical resources into logical Chapters using the current policy.
    Without usable TOC boundaries, preserve legacy behavior with one chapter per nonempty
    spine XHTML. Any body text before the first TOC boundary becomes a separate front-matter
    chapter rather than being lost.
    """
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
    # Count references per TOC file, not per book: NAV and NCX each mirror the same
    # destinations, and that duplication must not look like competing boundaries.
    resource_toc_refs: dict[tuple[str, str], int] = {}
    for entry in toc_entries:
        href = entry.get("resource_href")
        if isinstance(href, str) and href in resource_starts:
            key = (str(entry.get("toc_path") or ""), href)
            resource_toc_refs[key] = resource_toc_refs.get(key, 0) + 1
    for raw_entry in toc_entries:
        entry = raw_entry
        href = entry.get("resource_href")
        if not isinstance(href, str) or href not in resource_starts:
            continue
        fragment = entry.get("fragment")
        has_fragment = isinstance(fragment, str) and bool(fragment)
        resource = resource_by_href[href]
        raw_fragment_map = resource.get("fragment_anchors")
        fragment_map = raw_fragment_map if isinstance(raw_fragment_map, dict) else {}
        if has_fragment and fragment not in fragment_map:
            # A damaged fragment usually must not silently fall back to the resource start: when
            # several TOC destinations share one file that would split chapters at the wrong
            # point and assign the first heading's translation to the wrong TOC entry. But a
            # resource reached by this entry alone has no competing destination, so its start is
            # the best available boundary. Leave segment_anchor unset so the TOC label remains
            # the chapter title instead of borrowing a body heading.
            refs_key = (str(entry.get("toc_path") or ""), href)
            if resource_toc_refs.get(refs_key, 0) > 1:
                continue
            entry["boundary_position"] = resource_starts[href]
            continue
        segment_anchor = fragment_map.get(fragment) if has_fragment else None
        if not has_fragment:
            raw_segments = resource.get("segments")
            resource_segments = raw_segments if isinstance(raw_segments, list) else []
            first = next(
                (segment for segment in resource_segments if isinstance(segment, Segment)),
                None,
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
            # The fragment exists but follows the final text block.
            entry["boundary_position"] = resource_starts[href] + segment_count
        else:
            # An empty title page remains a valid boundary at the current stream position,
            # allowing subsequent spine body text to belong to that logical chapter.
            entry["boundary_position"] = resource_starts[href]

    # NAV spans or permissive NCX grouping nodes may represent parts without href/content.
    # Inherit the first resolvable descendant's boundary but not its segment_anchor,
    # so a child chapter heading's translation cannot become the grouping title.
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
            if isinstance(entry.get("boundary_position"), int):
                continue
            if entry.get("raw_href"):
                # Only structural groups without links may inherit a child destination. Explicit links that
                # cannot resolve are damaged data and must not silently change to another destination.
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

    strategy = get_chapter_split_strategy()
    ordered_toc_paths = list(
        dict.fromkeys(
            str(entry.get("toc_path"))
            for entry in toc_entries
            if isinstance(entry.get("toc_path"), str) and entry.get("toc_path")
        )
    )
    canonical_toc_path = ""
    boundaries: list[dict[str, object]] = []
    for toc_path in ordered_toc_paths:
        candidates = strategy.select(
            [entry for entry in toc_entries if entry.get("toc_path") == toc_path]
        )
        if candidates:
            # _parse_opf prioritizes EPUB3 NAV over NCX. Fall back to the next TOC only when the preferred
            # one cannot provide any chapter boundaries.
            canonical_toc_path = toc_path
            boundaries = candidates
            break

    def boundary_position(entry: dict[str, object]) -> int:
        """Return an integer boundary position already validated by the chapter policy."""
        value = entry.get("boundary_position")
        if not isinstance(value, int):
            raise ValueError("EPUB chapter boundary is missing an integer position")
        return value

    boundaries.sort(key=boundary_position)

    if len(boundaries) == 1 and _toc_collapsed_to_single_boundary(
        toc_entries, canonical_toc_path, resources
    ):
        # A damaged TOC may collapse every top-level entry onto one resource, reducing the book to one
        # chapter. If the spine still contains multiple body-bearing resources, fall back to resource
        # boundaries to preserve the book's structure.
        boundaries = []

    if not boundaries:
        chapters: list[Chapter] = []
        for resource in resources:
            raw_segments = resource.get("segments")
            segments = (
                [s for s in raw_segments if isinstance(s, Segment)]
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
                    template=None,
                    meta={"epub_split_strategy": "spine-fallback"},
                )
            )
        return chapters, "spine-fallback", canonical_toc_path

    slices: list[tuple[int, int, dict[str, object] | None]] = []
    first_position = boundary_position(boundaries[0])
    if first_position > 0:
        slices.append((0, first_position, None))
    for index, boundary in enumerate(boundaries):
        start = boundary_position(boundary)
        end = (
            boundary_position(boundaries[index + 1])
            if index + 1 < len(boundaries)
            else len(all_segments)
        )
        if end > start:
            slices.append((start, end, boundary))

    chapters = []
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
        meta: dict[str, object] = {"epub_split_strategy": strategy.name}
        if isinstance(toc_entry_id, str):
            meta["toc_entry_id"] = toc_entry_id
        chapters.append(
            Chapter(
                index=len(chapters),
                title=title,
                segments=segments,
                href=first_href or None,
                template=None,
                meta=meta,
            )
        )
    return chapters, strategy.name, canonical_toc_path
