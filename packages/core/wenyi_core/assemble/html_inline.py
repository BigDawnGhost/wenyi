"""Restore inline nodes, note ranges and annotation placements into translated blocks."""

from __future__ import annotations

import hashlib

from bs4 import BeautifulSoup
from bs4.element import ProcessingInstruction, Tag

from wenyi_core.alignment import coincident_point_order
from wenyi_core.markup.contracts import (
    ANNOTATION_ID_ATTR,
    ANNOTATION_META_KEY,
    HTML_MARKUP_META_KEY,
    INLINE_ID_ATTR,
    INLINE_META_KEY,
)
from wenyi_core.markup.objects import OBJECT_RELATION


def _append_text_with_breaks(soup: BeautifulSoup, element: Tag, text: str) -> None:
    """Append text, converting translation newlines into XHTML br elements."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for index, line in enumerate(lines):
        if line:
            element.append(line)
        if index + 1 < len(lines):
            element.append(soup.new_tag("br"))


def _merge_epub_render_meta(
    stored: dict[str, object],
    fresh: dict[str, object],
) -> dict[str, object]:
    """Merge persisted placements with temporary DOM metadata rebuilt from the original EPUB.
    Reparsed items and inline-node positions are authoritative for the DOM. Model-generated
    target placements exist only in chapter state and must not be overwritten by newly
    parsed metadata.
    """
    merged = dict(stored)
    merged.update(fresh)
    stored_raw = stored.get(ANNOTATION_META_KEY)
    fresh_raw = fresh.get(ANNOTATION_META_KEY)
    stored_annotations = stored_raw if isinstance(stored_raw, dict) else {}
    fresh_annotations = fresh_raw if isinstance(fresh_raw, dict) else {}
    if stored_annotations or fresh_annotations:
        annotations = dict(stored_annotations)
        annotations.update(fresh_annotations)
        for key in ("target_digest", "placements"):
            if key in stored_annotations:
                annotations[key] = stored_annotations[key]
        merged[ANNOTATION_META_KEY] = annotations
    return merged


def _clean_annotation_attrs(node: Tag) -> None:
    """Remove temporary backfill attributes so they cannot leak into the exported EPUB."""
    node.attrs.pop(ANNOTATION_ID_ATTR, None)
    for descendant in node.find_all(True, attrs={ANNOTATION_ID_ATTR: True}):
        descendant.attrs.pop(ANNOTATION_ID_ATTR, None)


def _range_marker_nodes(root: Tag, marker_text: str) -> list[Tag]:
    """Extract footnote markers from range links and discard source body nodes being replaced."""
    if not marker_text:
        return []
    candidates = root.find_all(["sup", "sub"])
    for node in reversed(candidates):
        if node.get_text("", strip=True) == marker_text:
            return [node.extract()]
    for node in reversed(root.find_all(True)):
        if node.get_text("", strip=True) == marker_text and not node.find(True):
            return [node.extract()]
    return []


def _fallback_annotation_node(
    root: Tag,
    *,
    mode: str,
    marker_text: str,
) -> Tag:
    """Fall back to paragraph-end markers when links cannot be aligned reliably; preserve
    attributes.
    """
    _clean_annotation_attrs(root)
    if mode != "range":
        return root
    markers = _range_marker_nodes(root, marker_text)
    root.clear()
    if markers:
        for marker in markers:
            root.append(marker)
    else:
        root.append(marker_text or "↩")
    return root


def _annotation_restorations(
    el: Tag,
    text: str,
    meta: dict[str, object],
) -> tuple[list[tuple[int, int, Tag]], list[tuple[int, int, int, Tag, list[Tag]]], list[Tag]]:
    """Extract annotation DOM into point, range and safe-fallback groups."""
    raw_annotations = meta.get(ANNOTATION_META_KEY)
    annotations = raw_annotations if isinstance(raw_annotations, dict) else {}
    raw_items = annotations.get("items")
    items = raw_items if isinstance(raw_items, list) else []
    raw_placements = annotations.get("placements")
    placements = raw_placements if isinstance(raw_placements, list) else []
    placement_by_id = {
        placement["id"]: placement
        for placement in placements
        if isinstance(placement, dict) and isinstance(placement.get("id"), str)
    }
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    digest_matches = annotations.get("target_digest") == digest

    points: list[tuple[int, int, Tag]] = []
    ranges: list[tuple[int, int, int, Tag, list[Tag]]] = []
    fallbacks: list[Tag] = []
    pending_ranges: list[tuple[int, int, int, Tag, list[Tag], str]] = []
    html_markup = bool(meta.get(HTML_MARKUP_META_KEY))
    # Snapshot nested roots before extracting any of them. Extracting an outer shell
    # must not make its descendants invisible to subsequent lookups.
    roots = {
        node.get(ANNOTATION_ID_ATTR): node
        for node in el.find_all(True, attrs={ANNOTATION_ID_ATTR: True})
    }

    def fallback(root: Tag, item: dict[str, object], marker_text: str) -> None:
        if html_markup and item.get("html_tag"):
            _clean_annotation_attrs(root)
            root.clear()
            if root.name == "a" and root.has_attr("href"):
                # Keep navigation available without pretending a failed range was aligned.
                root.append(str(item.get("source_text", "")))
            elif not (root.has_attr("id") or root.has_attr("name")):
                return
            fallbacks.append(root)
        else:
            fallbacks.append(
                _fallback_annotation_node(root, mode=str(item["mode"]), marker_text=marker_text)
            )

    def ordered_points(points: list[tuple[int, int, Tag]]) -> list[tuple[int, int, Tag]]:
        # Validate structure using source identities first. At rendering time replace only
        # coincident point slots with the order proved by copied target markers.
        order = coincident_point_order(
            [placement_by_id.get(items[index]["id"], {}) for _, index, _ in points]
        )
        return [
            (offset, source_order, points[target_index][2])
            for (offset, source_order, _), target_index in zip(points, order)
        ]

    for order, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        annotation_id = item.get("id")
        mode = item.get("mode")
        if not isinstance(annotation_id, str) or mode not in {"point", "range"}:
            continue
        root = roots.get(annotation_id)
        if not isinstance(root, Tag):
            continue
        root.extract()
        _clean_annotation_attrs(root)
        marker_text = item.get("marker_text")
        marker_text = marker_text if isinstance(marker_text, str) else ""
        placement = placement_by_id.get(annotation_id)
        start = placement.get("target_start") if isinstance(placement, dict) else None
        end = placement.get("target_end") if isinstance(placement, dict) else None
        status = placement.get("status") if isinstance(placement, dict) else None
        method = placement.get("method") if isinstance(placement, dict) else None
        rejected = {"fallback", "pending", "failed", "invalid", "missing", "stale"}
        usable = (
            digest_matches
            and isinstance(start, int)
            and not isinstance(start, bool)
            and isinstance(end, int)
            and not isinstance(end, bool)
            and 0 <= start <= end <= len(text)
            and str(status or "").lower() not in rejected
            and str(method or "").lower() not in rejected
        )
        if item.get("relation") == OBJECT_RELATION and not usable:
            if method == "damaged_object_marker":
                raise ValueError(
                    f"Protected object {annotation_id} is pending because its marker is damaged; "
                    "correct the saved paragraph response before resuming translation and exporting"
                )
            raise ValueError(
                f"Protected object {annotation_id} has pending or stale alignment; "
                "run translation again to repair it before exporting"
            )
        if not usable:
            source_start = item.get("source_start")
            source_end = item.get("source_end")
            source_length = annotations.get("source_length")
            # Only whole-source wraps (e.g. <h1><a>CHAPTER 1</a></h1>) keep the
            # translation inside the link. Partial inline links still fall back to ↩.
            covers_whole_source = (
                mode == "range"
                and not marker_text
                and (len(items) == 1 or html_markup)
                and isinstance(source_start, int)
                and not isinstance(source_start, bool)
                and isinstance(source_end, int)
                and not isinstance(source_end, bool)
                and isinstance(source_length, int)
                and not isinstance(source_length, bool)
                and source_start == 0
                and source_end == source_length > 0
            )
            if covers_whole_source:
                pending_ranges.append((0, len(text), order, root, [], marker_text))
                continue
            fallback(root, item, marker_text)
            continue
        assert isinstance(start, int) and not isinstance(start, bool)
        assert isinstance(end, int) and not isinstance(end, bool)
        if mode == "point" and start == end:
            points.append((start, order, root))
            continue
        if mode == "range" and start < end:
            markers = _range_marker_nodes(root, marker_text)
            pending_ranges.append((start, end, order, root, markers, marker_text))
            continue
        fallback(root, item, marker_text)

    if html_markup:
        # HTML style ranges may nest, but must retain their original parent. Source
        # siblings cannot become overlapping styles or bind text to the wrong link.
        stack: list[tuple[int, int, int, Tag, list[Tag]]] = []
        accepted_ids: dict[int, str] = {}
        for start, end, order, root, markers, marker_text in sorted(
            pending_ranges, key=lambda row: (row[0], -row[1], row[2])
        ):
            item = items[order]
            while stack and start >= stack[-1][1]:
                stack.pop()
            parent_id = item.get("html_parent")
            actual_parent = accepted_ids[stack[-1][2]] if stack else None
            valid_parent = parent_id == actual_parent
            valid_bounds = not stack or end <= stack[-1][1]
            nested_link = root.name == "a" and any(row[3].name == "a" for row in stack)
            if not valid_parent or not valid_bounds or nested_link:
                fallback(root, item, marker_text)
                continue
            row = (start, end, order, root, markers)
            ranges.append(row)
            stack.append(row)
            accepted_ids[order] = str(item["id"])
        # Point links inside a range link cannot become nested anchors.
        safe_points = []
        for offset, order, root in points:
            if any(
                start < offset < end and range_root.name == "a"
                for start, end, _, range_root, _ in ranges
            ):
                fallbacks.append(root)
            else:
                safe_points.append((offset, order, root))
        return ordered_points(safe_points), ranges, fallbacks

    # EPUB note links cannot cross or nest. Keep its existing conservative policy.
    last_end = -1
    for start, end, order, root, markers, marker_text in sorted(pending_ranges):
        if start < last_end:
            root.clear()
            if markers:
                for marker in markers:
                    root.append(marker)
            else:
                root.append(marker_text or "↩")
            fallbacks.append(root)
            continue
        ranges.append((start, end, order, root, markers))
        last_end = end
    safe_points: list[tuple[int, int, Tag]] = []
    for offset, order, root in points:
        is_object = items[order].get("relation") == OBJECT_RELATION
        inside_link = any(start < offset < end for start, end, _order, _root, _markers in ranges)
        if inside_link and is_object and root.name == "a":
            raise ValueError(
                f"Protected object {items[order]['id']} has conflicting link alignment"
            )
        if inside_link and not is_object:
            fallbacks.append(root)
        else:
            safe_points.append((offset, order, root))
    return ordered_points(safe_points), ranges, fallbacks


def _render_text_with_nodes(
    soup: BeautifulSoup,
    el: Tag,
    text: str,
    nodes: list[tuple[int, int, Tag]],
    ranges: list[tuple[int, int, int, Tag, list[Tag]]],
    fallbacks: list[Tag],
    *,
    nested: bool = False,
) -> None:
    """Backfill inline nodes and validated flat or nested ranges at target offsets."""
    ordered_nodes = sorted(nodes, key=lambda value: (value[0], value[1]))
    node_index = 0

    def append_until(
        parent: Tag,
        start: int,
        end: int,
        *,
        include_end: bool = True,
    ) -> None:
        nonlocal node_index
        cursor = start
        while node_index < len(ordered_nodes) and (
            ordered_nodes[node_index][0] < end
            or (include_end and ordered_nodes[node_index][0] == end)
        ):
            offset, _order, node = ordered_nodes[node_index]
            node_index += 1
            offset = min(max(offset, cursor), end)
            if offset > cursor:
                _append_text_with_breaks(soup, parent, text[cursor:offset])
            parent.append(node)
            cursor = offset
        if cursor < end:
            _append_text_with_breaks(soup, parent, text[cursor:end])

    # Annotation normally moves processing instructions before the block; protect any remaining ones.
    if el.parent is not None:
        for node in list(el.descendants):
            if isinstance(node, ProcessingInstruction):
                el.insert_before(node.extract())
    el.clear()
    children: dict[int | None, list[int]] = {}
    stack: list[int] = []
    ordered_ranges = sorted(ranges, key=lambda row: (row[0], -row[1], row[2]))
    for index, (start, _end, _order, _root, _markers) in enumerate(ordered_ranges):
        while stack and (not nested or start >= ordered_ranges[stack[-1]][1]):
            stack.pop()
        children.setdefault(stack[-1] if stack else None, []).append(index)
        stack.append(index)

    def render_ranges(parent: Tag, start: int, end: int, owner: int | None) -> None:
        cursor = start
        for index in children.get(owner, []):
            range_start, range_end, _order, root, markers = ordered_ranges[index]
            append_until(parent, cursor, range_start)
            root.clear()
            render_ranges(root, range_start, range_end, index)
            for marker in markers:
                root.append(marker)
            parent.append(root)
            cursor = range_end
        # End-boundary points remain siblings after links, never nested anchors.
        append_until(parent, cursor, end, include_end=owner is None)

    render_ranges(el, 0, len(text), None)
    # Adjacent fallback links at paragraph ends have no separating text, so footnote numbers can
    # merge (11, 12, 13 becomes 111213). Insert a separator to keep them readable.
    for index, fallback in enumerate(fallbacks):
        if nested and fallback.get_text(strip=True):
            el.append(" ")
        elif index > 0:
            el.append("、")
        el.append(fallback)


def _replace_block_content(
    soup: BeautifulSoup,
    el: Tag,
    text: str,
    meta: dict[str, object],
) -> None:
    """Replace block content and restore inline nodes and navigable annotation links."""
    # List items may use the a element itself as a translation block. Preserve its shell and extract
    # any sup/sub annotation markers first so clear() cannot delete them.
    self_markers: list[Tag] = []
    self_annotation_id = el.get(ANNOTATION_ID_ATTR)
    if isinstance(self_annotation_id, str):
        raw_annotations = meta.get(ANNOTATION_META_KEY)
        annotations = raw_annotations if isinstance(raw_annotations, dict) else {}
        raw_items = annotations.get("items")
        items = raw_items if isinstance(raw_items, list) else []
        item = next(
            (
                value
                for value in items
                if isinstance(value, dict) and value.get("id") == self_annotation_id
            ),
            {},
        )
        marker_text = item.get("marker_text")
        self_markers = _range_marker_nodes(
            el,
            marker_text if isinstance(marker_text, str) else "",
        )
        el.attrs.pop(ANNOTATION_ID_ATTR, None)
    raw_inline = meta.get(INLINE_META_KEY)
    inline = raw_inline if isinstance(raw_inline, dict) else {}
    raw_nodes = inline.get("nodes")
    nodes = raw_nodes if isinstance(raw_nodes, list) else []
    source_length = inline.get("source_length")
    if not isinstance(source_length, int) or source_length < 0:
        source_length = 0

    restored: list[tuple[int, int, Tag]] = []
    raw_annotations = meta.get(ANNOTATION_META_KEY)
    annotations = raw_annotations if isinstance(raw_annotations, dict) else {}
    object_ids = {
        item.get("id")
        for item in annotations.get("items", [])
        if isinstance(item, dict) and item.get("relation") == OBJECT_RELATION
    }
    for order, record in enumerate(nodes):
        if not isinstance(record, dict):
            continue
        inline_id = record.get("id")
        offset = record.get("offset")
        if not isinstance(inline_id, str) or not isinstance(offset, int):
            continue
        if inline_id in object_ids:
            continue
        node = el.find(True, attrs={INLINE_ID_ATTR: inline_id})
        if not isinstance(node, Tag):
            continue
        node.extract()
        node.attrs.pop(INLINE_ID_ATTR, None)
        if offset <= 0:
            target_offset = 0
        elif source_length <= 0 or offset >= source_length:
            target_offset = len(text)
        else:
            raise ValueError(f"Protected object {inline_id} has pending alignment")
        restored.append((target_offset, len(nodes) + order, node))

    # Extract ordinary inline nodes first: range links may contain images, which become inaccessible
    # after the link root has been extracted and cleared.
    annotation_points, annotation_ranges, annotation_fallbacks = _annotation_restorations(
        el, text, meta
    )
    # Use stable shared ordering for annotations and inline nodes, with point annotations first at ties.
    restored = list(annotation_points) + restored

    _render_text_with_nodes(
        soup,
        el,
        text,
        restored,
        annotation_ranges,
        annotation_fallbacks,
        nested=bool(meta.get(HTML_MARKUP_META_KEY)),
    )
    for marker in self_markers:
        el.append(marker)
