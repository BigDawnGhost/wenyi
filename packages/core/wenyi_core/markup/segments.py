"""Annotate one markup resource and extract stable translatable segments."""

from __future__ import annotations

import posixpath
import re

from bs4 import BeautifulSoup
from bs4.element import NavigableString, Tag

from wenyi_core.alignment.objects import object_marker
from wenyi_core.markup import _epub_native
from wenyi_core.markup.annotations import (
    _ANNOTATION_MARKER_ONLY,
    _annotation_roots,
    _hoist_processing_instructions,
    _is_internal_link,
    _is_text_string,
    _range_marker_node,
)
from wenyi_core.markup.contracts import (
    _ATOMIC_INLINE_TAGS,
    _BLOCK_CANDIDATE_TAGS,
    _BLOCK_TAGS,
    _HEADING_TAGS,
    ANNOTATION_ID_ATTR,
    ANNOTATION_META_KEY,
    HTML_MARKUP_META_KEY,
    INLINE_ID_ATTR,
    INLINE_META_KEY,
    LINE_WRAPPER_ATTR,
)
from wenyi_core.markup.epub_dom import parse_epub_dom
from wenyi_core.markup.objects import OBJECT_RELATION
from wenyi_core.markup.ruby import RUBY_MARK_LEFT, RUBY_MARK_RIGHT
from wenyi_core.models.document import KIND_HEADING, KIND_TEXT, Segment

_LAYOUT_WHITESPACE = re.compile(r"[ \t\r\n\f\v]+")


def _preserved_inline_roots(block: Tag) -> list[Tag]:
    """Return nontext nodes for unchanged backfill, preserving text-free wrappers where
    possible.
    """
    roots: list[Tag] = []
    seen: set[int] = set()
    for candidate in block.find_all(True):
        is_atomic = candidate.name in _ATOMIC_INLINE_TAGS
        is_empty_anchor = (
            candidate.name in {"a", "span"}
            and (candidate.has_attr("id") or candidate.has_attr("name"))
            and not candidate.get_text(strip=True)
        )
        if not is_atomic and not is_empty_anchor:
            continue
        # Annotation roots are restored separately. Still record their atomic children
        # independently so rebuilding a range link cannot erase its images.
        if candidate.has_attr(ANNOTATION_ID_ATTR) or any(
            parent.name in _ATOMIC_INLINE_TAGS
            for parent in candidate.parents
            if parent is not block
        ):
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
    if not offsets:
        return _LAYOUT_WHITESPACE.sub(" ", raw_text).strip(), []

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
    *,
    html_mode: bool = False,
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

    def walk(root: Tag, *, inside_range: bool = False) -> None:
        """Iteratively collect body text and source offsets for nodes that must be preserved."""
        stack: list[tuple] = [
            ("enter", child, inside_range) for child in reversed(list(root.children))
        ]
        while stack:
            item = stack.pop()
            kind = item[0]
            if kind == "end_ruby":
                ruby_tag, base_start = item[1], item[2]
                reading = "".join(rt.get_text() for rt in ruby_tag.find_all("rt")).strip()
                if reading and raw_length > base_start:
                    append_text(f"{RUBY_MARK_LEFT}{reading}{RUBY_MARK_RIGHT}")
                continue
            if kind == "end_range":
                annotation_id, start = item[1], item[2]
                annotation_events[annotation_id] = (start, raw_length)
                continue

            _, child, cur_range = item
            if isinstance(child, Tag):
                if child.name == "ruby":
                    # Include base kanji in body text and append marked kana readings for translation/review disambiguation.
                    stack.append(("end_ruby", child, raw_length))
                    for sub in reversed(list(child.children)):
                        stack.append(("enter", sub, cur_range))
                    continue
                if child.name in {"rt", "rp"}:
                    # Exclude standalone reading nodes from body text; the ruby branch already emits reading hints.
                    # Retain rt/rp elements in the template for bilingual export.
                    continue
                if cur_range and id(child) in marker_node_ids:
                    # Annotation numbers in range links are structural markers, not translatable text.
                    continue
                annotation = annotations.get(id(child))
                if annotation is not None:
                    annotation_id = str(annotation["id"])
                    start = raw_length
                    if id(child) in root_ids:
                        preserved_nodes.append((child, raw_length))
                        if annotation["mode"] == "range":
                            stack.append(("end_range", annotation_id, start))
                            for sub in reversed(list(child.children)):
                                stack.append(("enter", sub, True))
                        else:
                            annotation_events[annotation_id] = (start, raw_length)
                        continue
                    if annotation["mode"] == "range":
                        stack.append(("end_range", annotation_id, start))
                        for sub in reversed(list(child.children)):
                            stack.append(("enter", sub, True))
                    else:
                        annotation_events[annotation_id] = (start, raw_length)
                    continue
                if id(child) in root_ids:
                    preserved_nodes.append((child, raw_length))
                    continue
                for sub in reversed(list(child.children)):
                    stack.append(("enter", sub, cur_range))
            elif _is_text_string(child):
                value = str(child)
                if (
                    cur_range
                    and isinstance(child.next_sibling, Tag)
                    and id(child.next_sibling) in marker_node_ids
                ):
                    # Range note numbers are usually trailing. Formatting newlines before sup/sub are not body text;
                    # remove that trailing whitespace with the marker.
                    value = value.rstrip(" \t\r\n\f\v")
                if cur_range and not value.strip():
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
    normalize = _epub_native.html_normalize_text if html_mode else _normalize_html_text
    text, normalized_offsets = normalize(raw_text, event_offsets)
    if not text:
        return "", {}
    if not event_offsets:
        return text, {}

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
    dom_order = {id(node): index for index, node in enumerate(block.descendants)}
    annotation_order = {
        annotation["id"]: dom_order.get(id(annotation.get("root")), -1)
        for annotation in ordered_annotations
    }
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
        for key in ("html_tag", "html_parent"):
            if key in annotation:
                annotation_items[-1][key] = annotation[key]
    for record, (node, _raw_offset) in zip(nodes, preserved_nodes):
        # Text-free media/anchors at a block boundary have an exact structural position.
        # Interior objects and text-bearing formulas need a translated insertion boundary.
        if record["placement"] != "inline" and not node.get_text(strip=True):
            continue
        object_id = str(record["id"])
        node[ANNOTATION_ID_ATTR] = object_id
        annotation_order[object_id] = dom_order[id(node)]
        annotation_items.append(
            {
                "id": object_id,
                "mode": "point",
                "source_start": record["offset"],
                "source_end": record["offset"],
                "source_text": "",
                "marker_text": "",
                "raw_href": "",
                "target_key": object_id,
                "relation": OBJECT_RELATION,
                "marker": object_marker(object_id, text),
                "object_source": node.get_text(" ", strip=True) or str(node.get("src", node.name)),
                "object_tag": node.name,
            }
        )
    if annotation_items:
        annotation_items.sort(key=lambda item: annotation_order[item["id"]])
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


_LOOSE_TEXT_CONTAINERS = frozenset(
    {
        "body",
        "div",
        "section",
        "article",
        "main",
        "aside",
        "blockquote",
        "dd",
        "header",
        "footer",
        "figure",
        "details",
        "summary",
        "address",
    }
)
_NON_PROSE_TAGS = frozenset(
    {"head", "script", "style", "noscript", "template", "title", "meta", "link"}
)
_STRUCTURAL_BOUNDARY_TAGS = frozenset(
    {
        "section",
        "article",
        "main",
        "aside",
        "header",
        "footer",
        "nav",
        "ul",
        "ol",
        "dl",
        "table",
        "thead",
        "tbody",
        "tfoot",
        "tr",
        "figure",
        "hr",
        "pre",
        "form",
        "details",
        "summary",
        "address",
        "fieldset",
    }
)
_F_TEXT = 1 << 0
_F_PROSE = 1 << 1
_F_DESC_CANDIDATE = 1 << 2
_F_DESC_ANY_BLOCK = 1 << 3
_F_DESC_SPLIT = 1 << 4
_F_DESC_NON_PROSE = 1 << 5
_ASCII_WS = " \t\n\r\x0b\x0c"


def _is_note_link(link: Tag) -> bool:
    return _is_internal_link(link) and _range_marker_node(link) is not None


def _compute_flags(root: Tag | BeautifulSoup) -> dict[int, int]:
    """Compute bottom-up prose and structural flags iteratively in O(N) time."""
    flags: dict[int, int] = {}
    stack: list[tuple[Tag | BeautifulSoup | NavigableString, bool]] = [(root, False)]
    while stack:
        node, visited = stack.pop()
        if not visited:
            if isinstance(node, (Tag, BeautifulSoup)):
                stack.append((node, True))
                for child in reversed(list(node.children)):
                    if isinstance(child, Tag) or _is_text_string(child):
                        stack.append((child, False))
            elif _is_text_string(node) and str(node).strip():
                flags[id(node)] = _F_TEXT | _F_PROSE
            continue
        name = node.name if isinstance(node, Tag) else ""
        prose_ok = not (
            name in _ATOMIC_INLINE_TAGS or name in _NON_PROSE_TAGS or name in {"rt", "rp"}
        )
        f = 0
        for child in node.children:
            cf = flags.get(id(child), 0)
            if cf & _F_TEXT:
                f |= _F_TEXT
            if prose_ok and (cf & _F_PROSE):
                f |= _F_PROSE
            if isinstance(child, Tag):
                cname = child.name
                is_internal_link = cname == "a" and _is_internal_link(child)
                if (
                    cname not in _ATOMIC_INLINE_TAGS
                    and cname not in _NON_PROSE_TAGS
                    and (
                        (cname in _BLOCK_CANDIDATE_TAGS and (cf & _F_TEXT))
                        or (cf & _F_DESC_CANDIDATE)
                    )
                ):
                    f |= _F_DESC_CANDIDATE
                if (
                    cname in _BLOCK_CANDIDATE_TAGS
                    or cname in _STRUCTURAL_BOUNDARY_TAGS
                    or cname in _NON_PROSE_TAGS
                    or (cname not in _ATOMIC_INLINE_TAGS and (cf & _F_DESC_ANY_BLOCK))
                ):
                    f |= _F_DESC_ANY_BLOCK
                if (
                    cname in _BLOCK_CANDIDATE_TAGS
                    or cname in _STRUCTURAL_BOUNDARY_TAGS
                    or cname in _NON_PROSE_TAGS
                    or cname == "br"
                    or (
                        cname not in _ATOMIC_INLINE_TAGS
                        and not is_internal_link
                        and (cf & _F_DESC_SPLIT)
                    )
                    or (is_internal_link and (cf & _F_DESC_ANY_BLOCK))
                ):
                    f |= _F_DESC_SPLIT
                if cname in _NON_PROSE_TAGS or (
                    cname not in _ATOMIC_INLINE_TAGS and (cf & _F_DESC_NON_PROSE)
                ):
                    f |= _F_DESC_NON_PROSE
        flags[id(node)] = f
    return flags


def _trim_inline_boundary_space(
    node: Tag | NavigableString, *, leading: bool
) -> tuple[str | None, str | None]:
    if not _is_text_string(node):
        return None, None
    text = str(node)
    trimmed = text.lstrip(_ASCII_WS) if leading else text.rstrip(_ASCII_WS)
    ws_len = len(text) - len(trimmed)
    if ws_len == 0:
        return None, None
    if leading:
        return text[:ws_len], trimmed
    return text[len(text) - ws_len :], trimmed


def _inside_navigation_list(element: Tag) -> bool:
    """Check whether a block belongs to an EPUB3 nav TOC list."""
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


def _legacy_translation_targets(soup: BeautifulSoup, *, skip_navigation: bool) -> list[Tag]:
    """Pre-schema-7 candidate-only target selection for backward-compatible testing/export."""
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


def _schema7_translation_targets(soup: BeautifulSoup, *, skip_navigation: bool) -> list[Tag]:
    """Iteratively select replaceable targets in document order, including loose prose runs."""
    flags = _compute_flags(soup)
    targets: list[Tag] = []
    # Work items are either ("emit", wrapper) or ("visit", node, in_nav, in_nav_li, loose_inline).
    stack: list[tuple] = [("visit", soup, False, False, False)]
    while stack:
        item = stack.pop()
        if item[0] == "emit":
            targets.append(item[1])
            continue

        _, node, in_nav, in_nav_li, loose_inline = item
        name = node.name if isinstance(node, Tag) else ""
        if name in _NON_PROSE_TAGS or name in _ATOMIC_INLINE_TAGS:
            continue
        if name == "nav":
            next_nav, next_nav_li = True, False
        elif in_nav and name == "li":
            next_nav, next_nav_li = True, True
        else:
            next_nav, next_nav_li = in_nav, in_nav_li
        excluded = skip_navigation and next_nav_li
        node_flags = flags.get(id(node), 0)

        if isinstance(node, Tag) and name in _BLOCK_CANDIDATE_TAGS:
            if name == "li":
                link = node.find("a", recursive=False)
                if isinstance(link, Tag):
                    lf = flags.get(id(link), 0)
                    valid_link = bool(lf & _F_TEXT) and all(
                        child is link
                        or (
                            isinstance(child, Tag)
                            and (
                                child.name in _BLOCK_CANDIDATE_TAGS
                                or child.name in {"ul", "ol", "dl"}
                            )
                        )
                        or not (flags.get(id(child), 0) & _F_TEXT)
                        for child in node.children
                    )
                    if valid_link:
                        split_link = bool(lf & _F_DESC_ANY_BLOCK) or (
                            bool(lf & _F_DESC_SPLIT) and not _is_note_link(link)
                        )
                        if not split_link and not excluded:
                            targets.append(link)
                        for child in reversed(list(node.children)):
                            if child is link:
                                if split_link:
                                    stack.append(
                                        ("visit", link, next_nav, next_nav_li, not excluded)
                                    )
                            elif isinstance(child, Tag):
                                stack.append(("visit", child, next_nav, next_nav_li, False))
                        continue
            descend = (
                bool(node_flags & _F_DESC_CANDIDATE)
                or bool(node_flags & _F_DESC_NON_PROSE)
                or (name in _LOOSE_TEXT_CONTAINERS and bool(node_flags & _F_DESC_SPLIT))
            )
            if not descend:
                if not excluded:
                    targets.extend(_split_direct_break_lines(node, soup))
                continue

        if not excluded and (
            name in _LOOSE_TEXT_CONTAINERS
            or loose_inline
            or (name in _BLOCK_CANDIDATE_TAGS and bool(node_flags & _F_DESC_NON_PROSE))
        ):
            items: list[tuple] = []
            run_buf: list[Tag | NavigableString] = []
            prev_inline_split = loose_inline

            def flush(*, trail_split: bool) -> None:
                if not run_buf:
                    return
                if any(flags.get(id(n), 0) & _F_PROSE for n in run_buf):
                    lead_ws: str | None = None
                    trail_ws: str | None = None
                    if prev_inline_split:
                        lead_ws, trimmed = _trim_inline_boundary_space(run_buf[0], leading=True)
                        if lead_ws is not None and trimmed is not None:
                            if trimmed:
                                replacement = soup.new_string(trimmed)
                                run_buf[0].replace_with(replacement)
                                run_buf[0] = replacement
                            elif len(run_buf) > 1:
                                run_buf.pop(0).extract()
                    if trail_split:
                        trail_ws, trimmed = _trim_inline_boundary_space(run_buf[-1], leading=False)
                        if trail_ws is not None and trimmed is not None:
                            if trimmed:
                                replacement = soup.new_string(trimmed)
                                run_buf[-1].replace_with(replacement)
                                run_buf[-1] = replacement
                            elif len(run_buf) > 1:
                                run_buf.pop().extract()
                    wrapper = soup.new_tag("span")
                    wrapper[LINE_WRAPPER_ATTR] = "true"
                    run_buf[0].insert_before(wrapper)
                    if lead_ws is not None:
                        wrapper.insert_before(soup.new_string(lead_ws))
                    for elem in run_buf:
                        wrapper.append(elem.extract())
                    if trail_ws is not None:
                        wrapper.insert_after(soup.new_string(trail_ws))
                    items.append(("emit", wrapper))
                run_buf.clear()

            for child in list(node.children):
                if isinstance(child, Tag):
                    cname = child.name
                    if cname == "br":
                        flush(trail_split=False)
                        prev_inline_split = False
                    elif cname in _NON_PROSE_TAGS:
                        flush(trail_split=True)
                        prev_inline_split = True
                    elif cname in _BLOCK_CANDIDATE_TAGS or cname in _STRUCTURAL_BOUNDARY_TAGS:
                        flush(trail_split=False)
                        items.append(("visit", child, next_nav, next_nav_li, False))
                        prev_inline_split = False
                    elif (
                        cname not in _ATOMIC_INLINE_TAGS
                        and not (
                            cname == "a"
                            and _is_internal_link(child)
                            and not (flags.get(id(child), 0) & _F_DESC_ANY_BLOCK)
                        )
                        and (flags.get(id(child), 0) & _F_DESC_SPLIT)
                    ):
                        flush(trail_split=True)
                        items.append(("visit", child, next_nav, next_nav_li, True))
                        prev_inline_split = True
                    else:
                        run_buf.append(child)
                elif _is_text_string(child):
                    run_buf.append(child)
            flush(trail_split=loose_inline)
            for work in reversed(items):
                stack.append(work)
            continue

        for child in reversed(list(node.children)):
            if isinstance(child, Tag):
                stack.append(("visit", child, next_nav, next_nav_li, False))
    return targets


def _translation_targets(
    soup: BeautifulSoup,
    *,
    skip_navigation: bool,
    extract_loose_text: bool = True,
) -> list[Tag]:
    """Select the finest safely replaceable EPUB nodes in document order.
    Keep div/blockquote elements with child body blocks as containers, while extracting
    loose inline/br-separated prose runs directly inside body or block containers.
    """
    if not extract_loose_text:
        return _legacy_translation_targets(soup, skip_navigation=skip_navigation)
    return _schema7_translation_targets(soup, skip_navigation=skip_navigation)


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
    html_mode: bool = False,
    extract_loose_text: bool = True,
) -> tuple[str, list[Segment], str]:
    """Annotate one physical XHTML and return its title, segments and backfill template.
    Use the physical resource index in anchors rather than the final Chapter index, so
    writers rebuild identical data-tn-id values even if logical chapter policies change.
    """
    soup = BeautifulSoup(html, "html.parser") if html_mode else parse_epub_dom(html)
    title, segments = annotate_resource_dom(
        soup,
        resource_index,
        href,
        book_title=book_title,
        skip_navigation=skip_navigation,
        html_mode=html_mode,
        extract_loose_text=extract_loose_text,
    )
    return title, segments, str(soup)


def annotate_resource_dom(
    soup: BeautifulSoup,
    resource_index: int,
    href: str,
    *,
    book_title: str = "",
    skip_navigation: bool = False,
    html_mode: bool = False,
    extract_loose_text: bool = True,
) -> tuple[str, list[Segment]]:
    """Annotate the caller's DOM in place, allowing export to backfill the same tree."""
    segments: list[Segment] = []
    first_heading: Tag | None = None
    heading_title_parts: list[str] = []
    idx = 0
    if html_mode:
        from wenyi_core.markup.html import (
            html_attribute_segments,
            html_inline_roots,
            html_translation_targets,
        )

        targets = html_translation_targets(soup)
    else:
        targets = _translation_targets(
            soup,
            skip_navigation=skip_navigation,
            extract_loose_text=extract_loose_text,
        )
    for el in targets:
        anchor = f"tn{resource_index}_{idx}"
        annotations = (
            html_inline_roots(el, anchor, href)
            if html_mode
            else _annotation_roots(el, anchor, href)
        )
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
            if not (descendant.has_attr("id") or descendant.has_attr("name")):
                continue
            if not descendant.get_text(strip=True):
                continue
            if descendant.name in _ATOMIC_INLINE_TAGS or any(
                parent.name in _ATOMIC_INLINE_TAGS
                for parent in descendant.parents
                if parent is not el
            ):
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
                    id(parent) in range_annotation_roots
                    or (isinstance(parent, Tag) and parent.name == "a")
                    for parent in descendant.parents
                )
                marker = soup.new_tag("span" if inside_range_link else "a")
                marker.attrs.update(anchor_attrs)
                descendant.insert_before(marker)

        text, meta = _segment_content(el, anchor, annotations, html_mode=html_mode)
        if (
            annotations
            and all(annotation.get("mode") == "point" for annotation in annotations.values())
            and _ANNOTATION_MARKER_ONLY.fullmatch(text)
        ):
            continue
        if not text:
            continue
        if html_mode:
            meta[HTML_MARKUP_META_KEY] = {"version": 1}
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

    if html_mode:
        segments.extend(html_attribute_segments(soup, resource_index, idx))

    # Physical-resource title fallback: first heading, then a title that is neither an internal filename
    # nor the book title, then empty. Logical chapter titles later come from complete TOC nodes.
    # Some EPUBs place internal filenames or the complete book title in every XHTML title element.
    # These are not reader-visible chapter titles and must not enter TOC or title translation.
    title = " ".join(heading_title_parts)
    if not title and soup.title and soup.title.string:
        candidate = soup.title.string.strip()
        if not _looks_like_internal_title(candidate, href, book_title):
            title = candidate

    return title, segments
