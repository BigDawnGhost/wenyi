"""Annotate one markup resource and extract stable translatable segments."""

from __future__ import annotations

import posixpath

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from wenyi_core.ingest.models import KIND_HEADING, KIND_TEXT, Segment
from wenyi_core.markup.annotations import (
    _ANNOTATION_MARKER_ONLY,
    _annotation_roots,
    _hoist_processing_instructions,
    _is_text_string,
)
from wenyi_core.markup.contracts import (
    _ATOMIC_INLINE_TAGS,
    _BLOCK_CANDIDATE_TAGS,
    _BLOCK_TAGS,
    _HEADING_TAGS,
    ANNOTATION_ID_ATTR,
    ANNOTATION_META_KEY,
    INLINE_ID_ATTR,
    INLINE_META_KEY,
    LINE_WRAPPER_ATTR,
)
from wenyi_core.markup.ruby import RUBY_MARK_LEFT, RUBY_MARK_RIGHT


def _preserved_inline_roots(block: Tag) -> list[Tag]:
    """Return nontext nodes for unchanged backfill, preserving text-free wrappers where
    possible.
    """
    roots: list[Tag] = []
    seen: set[int] = set()
    for candidate in block.find_all(True):
        if candidate.has_attr(ANNOTATION_ID_ATTR):
            # Annotation roots are restored separately by epub_annotations, so do not duplicate them as
            # ordinary inline nodes. Still record their atomic children, such as images, independently
            # so rebuilding a range link's body cannot erase those children.
            continue
        is_atomic = candidate.name in _ATOMIC_INLINE_TAGS
        is_empty_anchor = (
            candidate.name in {"a", "span"}
            and not candidate.get_text(strip=True)
            and (candidate.has_attr("id") or candidate.has_attr("name"))
        )
        if not is_atomic and not is_empty_anchor:
            continue

        root = candidate
        parent = root.parent
        while (
            isinstance(parent, Tag)
            and parent is not block
            and parent.name not in _BLOCK_TAGS
            and not parent.has_attr(ANNOTATION_ID_ATTR)
            and not parent.get_text(strip=True)
        ):
            root = parent
            parent = root.parent
        if id(root) not in seen:
            seen.add(id(root))
            roots.append(root)
    return roots


def _normalize_html_text(
    raw_text: str,
    offsets: list[int],
) -> tuple[str, list[int]]:
    """Collapse HTML layout whitespace and map raw character boundaries to normalized text."""
    output: list[str] = []
    boundary_map = [0] * (len(raw_text) + 1)
    for index, char in enumerate(raw_text):
        boundary_map[index] = len(output)
        if char in " \t\r\n\f\v":
            if not output or output[-1] != " ":
                output.append(" ")
        else:
            output.append(char)
    boundary_map[len(raw_text)] = len(output)

    collapsed = "".join(output)
    leading = len(collapsed) - len(collapsed.lstrip())
    text = collapsed.strip()
    mapped = [
        min(max(boundary_map[min(max(offset, 0), len(raw_text))] - leading, 0), len(text))
        for offset in offsets
    ]
    return text, mapped


def _segment_content(
    block: Tag,
    anchor: str,
    annotation_roots: dict[int, dict[str, object]] | None = None,
) -> tuple[str, dict[str, object]]:
    """Extract translatable text and assign stable IDs/positions to inline nontext nodes.
    Collapse XHTML formatting whitespace as browsers do. Translation-target selection
    already splits br-separated visual lines, so br does not enter an individual Segment's
    text.
    """
    annotations = annotation_roots or {}
    marker_node_ids: set[int] = set()
    for annotation in annotations.values():
        raw_marker_ids = annotation.get("marker_node_ids")
        if isinstance(raw_marker_ids, set):
            marker_node_ids.update(
                marker_id for marker_id in raw_marker_ids if isinstance(marker_id, int)
            )
    roots = _preserved_inline_roots(block)
    root_ids = {id(node) for node in roots}
    text_parts: list[str] = []
    preserved_nodes: list[tuple[Tag, int]] = []
    annotation_events: dict[str, tuple[int, int]] = {}
    raw_length = 0

    def append_text(value: str) -> None:
        """Append raw text while tracking character positions at DOM boundaries."""
        nonlocal raw_length
        text_parts.append(value)
        raw_length += len(value)

    def walk(parent: Tag, *, inside_range: bool = False) -> None:
        """Recursively collect body text and source offsets for nodes that must be preserved."""
        for child in parent.children:
            if isinstance(child, Tag):
                if child.name == "ruby":
                    # Include base kanji in body text and append marked kana readings for translation/review disambiguation.
                    base_start = raw_length
                    walk(child, inside_range=inside_range)
                    reading = "".join(rt.get_text() for rt in child.find_all("rt")).strip()
                    if reading and raw_length > base_start:
                        append_text(f"{RUBY_MARK_LEFT}{reading}{RUBY_MARK_RIGHT}")
                    continue
                if child.name in {"rt", "rp"}:
                    # Exclude standalone reading nodes from body text; the ruby branch already emits reading hints.
                    # Retain rt/rp elements in the template for bilingual export.
                    continue
                if inside_range and id(child) in marker_node_ids:
                    # Annotation numbers in range links are structural markers, not translatable text.
                    continue
                annotation = annotations.get(id(child))
                if annotation is not None:
                    annotation_id = str(annotation["id"])
                    start = raw_length
                    if annotation["mode"] == "range":
                        walk(child, inside_range=True)
                    annotation_events[annotation_id] = (start, raw_length)
                if id(child) in root_ids:
                    preserved_nodes.append((child, raw_length))
                elif annotation is not None:
                    continue
                else:
                    walk(child, inside_range=inside_range)
            elif _is_text_string(child):
                value = str(child)
                if (
                    inside_range
                    and isinstance(child.next_sibling, Tag)
                    and id(child.next_sibling) in marker_node_ids
                ):
                    # Range note numbers are usually trailing. Formatting newlines before sup/sub are not body text;
                    # remove that trailing whitespace with the marker.
                    value = value.rstrip(" \t\r\n\f\v")
                if inside_range and not value.strip():
                    previous = child.previous_sibling
                    has_later_text = any(
                        sibling.get_text(strip=True)
                        if isinstance(sibling, Tag)
                        else _is_text_string(sibling) and bool(str(sibling).strip())
                        for sibling in child.next_siblings
                    )
                    if (
                        isinstance(previous, Tag)
                        and id(previous) in marker_node_ids
                        and not has_later_text
                    ):
                        # Indentation after the note number and before the range link closes is also not body text.
                        continue
                append_text(value)

    block_annotation = annotations.get(id(block))
    if block_annotation is not None:
        annotation_id = str(block_annotation["id"])
        if block_annotation["mode"] == "range":
            walk(block, inside_range=True)
        annotation_events[annotation_id] = (0, raw_length)
    else:
        walk(block)

    raw_text = "".join(text_parts)
    event_offsets = [offset for _node, offset in preserved_nodes]
    ordered_annotations = list(annotations.values())
    for annotation in ordered_annotations:
        start, end = annotation_events.get(str(annotation["id"]), (raw_length, raw_length))
        event_offsets.extend((start, end))
    text, normalized_offsets = _normalize_html_text(raw_text, event_offsets)
    if not text:
        return "", {}

    source_length = len(text)
    nodes: list[dict[str, object]] = []
    offset_cursor = 0
    for index, (node, _raw_offset) in enumerate(preserved_nodes):
        inline_id = f"{anchor}_inline_{index}"
        offset = normalized_offsets[offset_cursor]
        offset_cursor += 1
        placement = "before" if offset == 0 else "after" if offset == source_length else "inline"
        node[INLINE_ID_ATTR] = inline_id
        nodes.append(
            {
                "id": inline_id,
                "tag": node.name,
                "placement": placement,
                "offset": offset,
            }
        )

    meta: dict[str, object] = {}
    if nodes:
        meta[INLINE_META_KEY] = {
            "version": 1,
            "source_length": source_length,
            "nodes": nodes,
        }
    annotation_items: list[dict[str, object]] = []
    for annotation in ordered_annotations:
        start = normalized_offsets[offset_cursor]
        end = normalized_offsets[offset_cursor + 1]
        offset_cursor += 2
        annotation_items.append(
            {
                "id": annotation["id"],
                "mode": annotation["mode"],
                "source_start": start,
                "source_end": end,
                "source_text": text[start:end],
                "marker_text": annotation["marker_text"],
                "raw_href": annotation["raw_href"],
                "target_key": annotation["target_key"],
                "relation": annotation["relation"],
            }
        )
    if annotation_items:
        meta[ANNOTATION_META_KEY] = {
            "version": 1,
            "source_length": source_length,
            "items": annotation_items,
        }
    return text, meta


def _has_meaningful_descendant_block(element: Tag) -> bool:
    """Retain an outer block only as layout when it contains finer-grained body blocks."""
    return any(
        descendant.get_text(strip=True) for descendant in element.find_all(_BLOCK_CANDIDATE_TAGS)
    )


def _list_item_link_target(element: Tag) -> Tag | None:
    """Return the direct link when it is a list item's only body text, preserving li and nested
    lists.
    """
    link = element.find("a", recursive=False)
    if not isinstance(link, Tag) or not link.get_text(strip=True):
        return None
    for child in element.children:
        if child is link:
            continue
        if isinstance(child, Tag):
            # Nested lists/body blocks have their own leaf targets and are not part of this li's text.
            if child.name in _BLOCK_CANDIDATE_TAGS or child.name in {"ul", "ol", "dl"}:
                continue
            if child.get_text(strip=True):
                return None
            continue
        if _is_text_string(child):
            if str(child).strip():
                return None
            continue
        # Ignore non-body nodes such as comments and processing instructions.
    return link


def _split_direct_break_lines(element: Tag, soup: BeautifulSoup) -> list[Tag]:
    """Wrap visible lines separated by direct br elements as independent targets; leave br
    unchanged.
    """
    children = list(element.children)
    if not any(isinstance(child, Tag) and child.name == "br" for child in children):
        return [element]

    runs: list[list[Tag | NavigableString]] = [[]]
    for child in children:
        if isinstance(child, Tag) and child.name == "br":
            runs.append([])
        elif isinstance(child, Tag):
            runs[-1].append(child)
        elif _is_text_string(child):
            # Keep processing instructions and comments outside runs so export clear() preserves them.
            runs[-1].append(child)

    targets: list[Tag] = []
    for run in runs:
        has_text = any(
            node.get_text(strip=True)
            if isinstance(node, Tag)
            else _is_text_string(node) and bool(str(node).strip())
            for node in run
        )
        if not has_text:
            continue
        wrapper = soup.new_tag("span")
        wrapper[LINE_WRAPPER_ATTR] = "true"
        run[0].insert_before(wrapper)
        for node in run:
            wrapper.append(node.extract())
        targets.append(wrapper)
    return targets


def _translation_targets(
    soup: BeautifulSoup,
    *,
    skip_navigation: bool,
) -> list[Tag]:
    """Select the finest safely replaceable EPUB nodes in document order.
    Keep div/blockquote elements with child body blocks as containers. Direct link text
    inside li becomes its own target, preserving list hierarchy and href.
    """
    targets: list[Tag] = []
    for element in soup.find_all(_BLOCK_CANDIDATE_TAGS):
        if skip_navigation and _inside_navigation_list(element):
            continue

        has_descendant_block = _has_meaningful_descendant_block(element)
        if element.name == "li":
            link = _list_item_link_target(element)
            if link is not None:
                targets.extend(_split_direct_break_lines(link, soup))
            if link is not None or has_descendant_block:
                continue

        if has_descendant_block:
            continue
        targets.extend(_split_direct_break_lines(element, soup))
    return targets


def _looks_like_internal_title(title: str, href: str, book_title: str = "") -> bool:
    """Check whether an XHTML title is merely an internal filename or repeated book title."""
    base = posixpath.basename(href).rsplit(".", 1)[0]
    stripped = title.strip()
    return (bool(base) and stripped == base) or (
        bool(book_title) and stripped == book_title.strip()
    )


def annotate_epub_resource(
    html: str,
    resource_index: int,
    href: str,
    *,
    book_title: str = "",
    skip_navigation: bool = False,
) -> tuple[str, list[Segment], str]:
    """Annotate one physical XHTML and return its title, segments and backfill template.
    Use the physical resource index in anchors rather than the final Chapter index, so
    writers rebuild identical data-tn-id values even if logical chapter policies change.
    """
    soup = BeautifulSoup(html, "html.parser")
    segments: list[Segment] = []
    first_heading: Tag | None = None
    heading_title_parts: list[str] = []
    idx = 0
    for el in _translation_targets(soup, skip_navigation=skip_navigation):
        anchor = f"tn{resource_index}_{idx}"
        annotations = _annotation_roots(el, anchor, href)
        protected_annotation_nodes: set[int] = set()
        range_annotation_roots: set[int] = set()
        for annotation in annotations.values():
            root = annotation.get("root")
            if not isinstance(root, Tag):
                continue
            protected_annotation_nodes.add(id(root))
            if annotation.get("mode") == "point":
                protected_annotation_nodes.update(id(node) for node in root.find_all(True))
                continue
            range_annotation_roots.add(id(root))
            raw_marker_ids = annotation.get("marker_node_ids")
            marker_ids = raw_marker_ids if isinstance(raw_marker_ids, set) else set()
            for node in root.find_all(True):
                if id(node) in marker_ids or any(
                    id(parent) in marker_ids for parent in node.parents if parent is not root
                ):
                    protected_annotation_nodes.add(id(node))
        # Text-bearing inline id/name wrappers are flattened during target backfill. Convert them first
        # to empty anchors at the same position so the existing nontext restoration mechanism preserves them.
        for descendant in list(el.find_all(True)):
            if not descendant.get_text(strip=True):
                continue
            if id(descendant) in protected_annotation_nodes:
                # Preserve attributes on point roots, range roots and confirmed note markers. Move id/name from
                # other semantic wrappers inside ranges into empty anchors, allowing the writer to restore
                # those destinations after clearing source nodes.
                continue
            anchor_attrs = {
                key: descendant.attrs.pop(key) for key in ("id", "name") if key in descendant.attrs
            }
            if anchor_attrs:
                # HTML forbids nested anchors. Replace destinations inside range links with equivalent empty spans
                # that preserve id/name without breaking the outer link.
                inside_range_link = any(
                    id(parent) in range_annotation_roots for parent in descendant.parents
                )
                marker = soup.new_tag("span" if inside_range_link else "a")
                marker.attrs.update(anchor_attrs)
                descendant.insert_before(marker)

        text, meta = _segment_content(el, anchor, annotations)
        if (
            annotations
            and all(annotation.get("mode") == "point" for annotation in annotations.values())
            and _ANNOTATION_MARKER_ONLY.fullmatch(text)
        ):
            continue
        if not text:
            continue
        el["data-tn-id"] = anchor
        # Move page-number processing instructions before blocks so backfill clear() preserves them.
        _hoist_processing_instructions(el)
        kind = (
            KIND_HEADING
            if el.name in _HEADING_TAGS or el.find_parent(_HEADING_TAGS) is not None
            else KIND_TEXT
        )
        if kind == KIND_HEADING:
            heading = el if el.name in _HEADING_TAGS else el.find_parent(_HEADING_TAGS)
            if isinstance(heading, Tag):
                if first_heading is None:
                    first_heading = heading
                if heading is first_heading:
                    heading_title_parts.append(text)
        segments.append(
            Segment(
                index=idx,
                source=text,
                kind=kind,
                anchor=anchor,
                resource_href=href,
                meta=meta,
            )
        )
        idx += 1

    # Physical-resource title fallback: first heading, then a title that is neither an internal filename
    # nor the book title, then empty. Logical chapter titles later come from complete TOC nodes.
    # Some EPUBs place internal filenames or the complete book title in every XHTML title element.
    # These are not reader-visible chapter titles and must not enter TOC or title translation.
    title = " ".join(heading_title_parts)
    if not title and soup.title and soup.title.string:
        candidate = soup.title.string.strip()
        if not _looks_like_internal_title(candidate, href, book_title):
            title = candidate

    return title, segments, str(soup)


def _inside_navigation_list(element: Tag) -> bool:
    """Check whether a block belongs to an EPUB3 nav TOC list.
    Protect li elements and their internal blocks from backfill that would erase links or
    nested ol elements. Visible headings or descriptions elsewhere inside nav still need
    translation.
    """
    inside_nav = False
    inside_list_item = element.name == "li"
    for parent in element.parents:
        if not isinstance(parent, Tag):
            continue
        if parent.name == "li":
            inside_list_item = True
        elif parent.name == "nav":
            inside_nav = True
            break
    return inside_nav and inside_list_item
