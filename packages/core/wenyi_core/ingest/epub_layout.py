"""Map physical EPUB resources into logical chapters and auxiliary note contexts."""

from __future__ import annotations

from bs4 import BeautifulSoup

from wenyi_core.ingest.epub_chapters import get_chapter_split_strategy
from wenyi_core.ingest.models import KIND_HEADING, Chapter, Segment
from wenyi_core.markup.anchors import fragment_nodes, resolve_epub_href, scope_segment_anchors
from wenyi_core.markup.annotations import _note_context_scope
from wenyi_core.markup.contracts import ANNOTATION_META_KEY


def _build_epub_annotation_contexts(
    reference_resources: list[dict[str, object]],
    lookup_resources: list[dict[str, object]],
) -> dict[str, object]:
    """Resolve forward annotation references into a deduplicated immutable source-context
    index.
    """
    lookup_by_href = {
        str(resource.get("href")): resource
        for resource in lookup_resources
        if isinstance(resource.get("href"), str) and resource.get("href")
    }
    soups: dict[str, BeautifulSoup] = {}
    sources_by_href: dict[str, dict[str, str]] = {}
    for href, resource in lookup_by_href.items():
        template = resource.get("template")
        if isinstance(template, str):
            soups[href] = BeautifulSoup(template, "html.parser")
        raw_segments = resource.get("segments")
        segments = raw_segments if isinstance(raw_segments, list) else []
        sources_by_href[href] = {
            segment.anchor: segment.source
            for segment in segments
            if isinstance(segment, Segment) and isinstance(segment.anchor, str) and segment.anchor
        }

    contexts: dict[str, dict[str, object]] = {}
    for resource in reference_resources:
        raw_segments = resource.get("segments")
        segments = raw_segments if isinstance(raw_segments, list) else []
        for segment in segments:
            if not isinstance(segment, Segment):
                continue
            raw_annotations = segment.meta.get(ANNOTATION_META_KEY)
            annotations = raw_annotations if isinstance(raw_annotations, dict) else {}
            raw_items = annotations.get("items")
            items = raw_items if isinstance(raw_items, list) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                raw_href = item.get("raw_href")
                if not isinstance(raw_href, str):
                    continue
                resolved = resolve_epub_href(segment.resource_href or "", raw_href)
                if (
                    resolved.external
                    or not resolved.resource_href
                    or not resolved.fragment
                    or resolved.target_key != item.get("target_key")
                ):
                    continue
                target_soup = soups.get(resolved.resource_href)
                if target_soup is None:
                    continue
                target_nodes = fragment_nodes(target_soup, resolved.fragment)
                if len(target_nodes) != 1:
                    # Duplicate id/name values make reference ownership ambiguous; omit that context.
                    continue
                scope, semantic_note = _note_context_scope(target_nodes[0], resolved.fragment)
                relation = item.get("relation")
                if relation == "internal_link" and semantic_note:
                    relation = "noteref"
                    item["relation"] = relation
                if relation != "noteref":
                    continue

                anchors = scope_segment_anchors(scope)
                source_map = sources_by_href.get(resolved.resource_href, {})
                anchors = [anchor for anchor in anchors if anchor in source_map]
                if not anchors:
                    continue
                # Self-links provide no new information and cannot define their own annotations.
                if (
                    resolved.resource_href == segment.resource_href
                    and isinstance(segment.anchor, str)
                    and anchors == [segment.anchor]
                ):
                    continue
                source_blocks = [
                    source_map[anchor] for anchor in anchors if source_map[anchor].strip()
                ]
                if not source_blocks:
                    continue
                contexts.setdefault(
                    resolved.target_key,
                    {
                        "target_key": resolved.target_key,
                        "resource_href": resolved.resource_href,
                        "fragment": resolved.fragment,
                        "source_blocks": source_blocks,
                        "segment_anchors": anchors,
                    },
                )
    return {"version": 1, "contexts": contexts}


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
            # An invalid fragment must not silently fall back to the resource start; that would split chapters
            # at the wrong point and assign the first heading's translation to the wrong TOC entry.
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
