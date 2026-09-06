"""EPUB reader using the standard library and BeautifulSoup.
An EPUB ZIP contains META-INF/container.xml pointing to OPF, whose manifest lists resources
and spine defines reading order. Read physical XHTML resources in spine order, then split
logical chapters at top-level NCX/NAV anchors. Chapters and XHTML are not one-to-one: each
Segment retains resource_href for writer aggregation and backfill.
"""

from __future__ import annotations

import os
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from typing import TypeGuard
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, UnicodeDammit
from bs4.element import Comment, NavigableString, ProcessingInstruction, Tag

from .epub_chapters import get_chapter_split_strategy
from .epub_toc import parse_toc_entries, resolve_epub_href
from .models import KIND_HEADING, KIND_TEXT, Chapter, Document, Segment

_CONTAINER = "META-INF/container.xml"
_BLOCK_TAGS = {
    "p",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "blockquote",
    "td",
    "th",
    "dt",
    "dd",
    "figcaption",
}
_BLOCK_CANDIDATE_TAGS = _BLOCK_TAGS | {"div"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_INLINE_META_KEY = "epub_inline"
_INLINE_ID_ATTR = "data-tn-inline-id"
_ANNOTATION_META_KEY = "epub_annotations"
# Embed furigana in Segment.source using distinctive reading markers that prompts can exclude.
# Strip those markers for glossary matching; retain the original ruby element in the template.
_RUBY_MARK_LEFT = "〘"
_RUBY_MARK_RIGHT = "〙"
_RUBY_MARK_RE = re.compile(r"〘[^〙]*〙")
_ANNOTATION_ID_ATTR = "data-tn-annotation-id"
_ANNOTATION_MARKER_ONLY = re.compile(r"^[\d\s*＊※†‡\[\]()〔〕（）{}↩↵←↑↓⤶.·:：\-]+$")
_ANNOTATION_HINT = re.compile(
    r"(?:^|[^a-z0-9])(?:note|noteref|footnote|endnote|fn|jpref|jpnote)"
    r"(?:[-_]?\d+)?(?:$|[^a-z0-9])",
    re.IGNORECASE,
)
_NOTE_TARGET_HINT = re.compile(
    r"(?:^|[^a-z0-9])(?:note|footnote|endnote|fn)(?:[-_]?\d+)?(?:$|[^a-z0-9])",
    re.IGNORECASE,
)
_SHORT_NOTE_IDENTITY = re.compile(r"^n[-_]?\d+$", re.IGNORECASE)
_NOTEREF_SEMANTICS = {"noteref", "doc-noteref"}
_BACKLINK_SEMANTICS = {"backlink", "doc-backlink"}
_NOTE_BODY_SEMANTICS = {"footnote", "endnote", "rearnote", "doc-footnote", "doc-endnote"}
_ATOMIC_INLINE_TAGS = {
    "audio",
    "canvas",
    "embed",
    "hr",
    "iframe",
    "img",
    "math",
    "object",
    "source",
    "svg",
    "video",
}

_LINE_WRAPPER_ATTR = "data-tn-line"


def _preserved_inline_roots(block: Tag) -> list[Tag]:
    """Return nontext nodes for unchanged backfill, preserving text-free wrappers where
    possible.
    """
    roots: list[Tag] = []
    seen: set[int] = set()
    for candidate in block.find_all(True):
        if candidate.has_attr(_ANNOTATION_ID_ATTR):
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
            and not parent.has_attr(_ANNOTATION_ID_ATTR)
            and not parent.get_text(strip=True)
        ):
            root = parent
            parent = root.parent
        if id(root) not in seen:
            seen.add(id(root))
            roots.append(root)
    return roots


def _is_internal_link(link: Tag) -> bool:
    """Determine whether a link targets an EPUB resource rather than web, email or script
    content.
    """
    raw_href = link.get("href")
    if not isinstance(raw_href, str) or not raw_href.strip():
        return False
    parsed = urlsplit(raw_href.strip())
    return not parsed.scheme and not parsed.netloc


def _semantic_tokens(node: Tag) -> set[str]:
    """Return lowercase EPUB/ARIA/HTML semantic tokens for a link."""
    tokens: set[str] = set()
    for key in ("epub:type", "role", "rel"):
        value = node.get(key)
        values = value if isinstance(value, list) else [value]
        for raw in values:
            if raw is None:
                continue
            for token in str(raw).split():
                normalized = token.strip().lower()
                if normalized:
                    tokens.add(normalized)
                    # Some generators prefix values, for example z3998:footnote.
                    tokens.add(normalized.rsplit(":", 1)[-1])
    return tokens


def _inside_note_body(node: Tag) -> bool:
    """Check whether a node is inside an explicit footnote/endnote semantic container."""
    for parent in (node, *node.parents):
        if isinstance(parent, Tag) and _semantic_tokens(parent) & _NOTE_BODY_SEMANTICS:
            return True
    return False


def _has_note_identity(node: Tag) -> bool:
    """Check whether id, name or class explicitly identifies footnote body content."""
    identity: list[str] = []
    for key in ("id", "name", "class"):
        value = node.get(key)
        if isinstance(value, list):
            identity.extend(str(item) for item in value)
        elif value is not None:
            identity.append(str(value))
    return bool(_NOTE_TARGET_HINT.search(" ".join(identity)))


def _has_short_note_identity(node: Tag) -> bool:
    """Detect n1-style annotation IDs/names that require strong supporting structure."""
    return any(
        isinstance(node.get(key), str) and bool(_SHORT_NOTE_IDENTITY.fullmatch(str(node.get(key))))
        for key in ("id", "name")
    )


def _implicit_note_body_scope(node: Tag) -> Tag | None:
    """Find the nearest named annotation container lacking explicit EPUB semantics."""
    for parent in (node, *node.parents):
        if (
            isinstance(parent, Tag)
            and parent.name in {"aside", "li", "dd", "p", "div", "section"}
            and _has_note_identity(parent)
        ):
            return parent
    return None


def _short_note_body_scope(node: Tag) -> Tag | None:
    """Find an n1-style container; callers must also supply superscript or backlink evidence."""
    for parent in (node, *node.parents):
        if (
            isinstance(parent, Tag)
            and parent.name in {"aside", "li", "dd", "p"}
            and _has_short_note_identity(parent)
        ):
            return parent
    return None


def _inside_implicit_note_body(node: Tag) -> bool:
    """Check whether a node is in a named annotation container lacking EPUB semantics."""
    if _implicit_note_body_scope(node) is not None:
        return True
    short_scope = _short_note_body_scope(node)
    if short_scope is None or not _ANNOTATION_MARKER_ONLY.fullmatch(node.get_text("", strip=True)):
        return False
    before, after = _surrounding_text(short_scope, node)
    return bool(before.strip()) != bool(after.strip())


def _is_text_string(node: object) -> TypeGuard[NavigableString]:
    """Recognize actual text nodes, excluding XML processing instructions such as page-break
    markers.
    """
    return isinstance(node, NavigableString) and not isinstance(
        node, (Comment, ProcessingInstruction)
    )


def _hoist_processing_instructions(el: Tag) -> None:
    """Move block processing instructions before the block so writer clear() cannot erase them."""
    if el.parent is None:
        return
    extracted = [
        node.extract() for node in list(el.descendants) if isinstance(node, ProcessingInstruction)
    ]
    for node in extracted:
        el.insert_before(node)


def _surrounding_text(block: Tag, node: Tag) -> tuple[str, str]:
    """Return visible text before and after a node to identify leading backlinks."""

    def text_of(value: object) -> str:
        if _is_text_string(value):
            return str(value)
        if isinstance(value, Tag):
            return value.get_text(" ", strip=False)
        return ""

    before: list[str] = []
    after: list[str] = []
    cursor: Tag = node
    while cursor is not block and isinstance(cursor.parent, Tag):
        before.extend(text_of(sibling) for sibling in cursor.previous_siblings)
        after.extend(text_of(sibling) for sibling in cursor.next_siblings)
        cursor = cursor.parent
    return "".join(before), "".join(after)


def _annotation_relation(
    link: Tag,
    block: Tag,
    *,
    marker_wrapper: Tag | None,
    range_marker: Tag | None,
    marker_only: bool,
) -> str:
    """Classify structure-preserving internal links as annotation links, backlinks or ordinary
    links.
    """
    semantics = _semantic_tokens(link)
    if semantics & _BACKLINK_SEMANTICS:
        return "backlink"
    if semantics & _NOTEREF_SEMANTICS:
        return "noteref"
    if _inside_note_body(link):
        return "backlink"
    if marker_only and _inside_implicit_note_body(link):
        return "backlink"
    if marker_only and re.fullmatch(r"[↩↵←↑↓⤶\s]+", link.get_text("", strip=True)):
        return "backlink"

    # Footnotes often begin with a bare backlink number followed by explanatory text, without sup/sub.
    # Treat that leading link conservatively as a backlink to avoid injecting body text as annotation.
    if marker_wrapper is None and marker_only and link is not block:
        before, after = _surrounding_text(block, link)
        if not before.strip() and after.strip():
            return "backlink"

    if marker_wrapper is not None or range_marker is not None or marker_only:
        return "noteref"
    return "internal_link"


def _nearest_marker_wrapper(link: Tag, block: Tag) -> Tag | None:
    """Find the nearest semantic superscript/subscript wrapper between a link and its block.
    Besides native sup/sub, accept spans with explicit classes or inline styles indicating
    super/subscript. Ordinary spans remain body text.
    """

    def is_marker_wrapper(node: Tag) -> bool:
        if node.name in {"sup", "sub"}:
            return True
        if node.name != "span":
            return False
        classes = {
            str(value).strip().lower()
            for value in node.get_attribute_list("class")
            if str(value).strip()
        }
        if classes & {"sup", "super", "superscript", "sub", "subscript"}:
            return True
        style = node.get("style")
        return isinstance(style, str) and bool(
            re.search(r"(?:^|;)\s*vertical-align\s*:\s*(?:super|sub)\b", style, re.IGNORECASE)
        )

    parent = link.parent
    while isinstance(parent, Tag):
        if parent is block:
            return parent if is_marker_wrapper(parent) else None
        if is_marker_wrapper(parent):
            return parent
        parent = parent.parent
    return None


def _has_annotation_hint(
    link: Tag,
    marker: Tag,
    marker_text: str,
    *,
    allow_short_n: bool = False,
) -> bool:
    """Use explicit annotation evidence to distinguish note numbers from ordinary internal
    links.
    """
    decorated = bool(re.search(r"[^\d\s.·:\-]", marker_text))
    parsed = urlsplit(str(link.get("href", "")))
    attrs: list[str] = [parsed.fragment]
    for node in (link, marker):
        for key in ("id", "class", "role", "rel", "epub:type"):
            value = node.get(key)
            if isinstance(value, list):
                attrs.extend(str(item) for item in value)
            elif value is not None:
                attrs.append(str(value))
    hint = " ".join(attrs)
    numbered_note_fragment = bool(
        re.search(
            r"(?:notes?|footnotes?|endnotes?|fn)[\-_]?\d+$",
            parsed.fragment,
            re.IGNORECASE,
        )
    )
    short_n_fragment = allow_short_n and bool(_SHORT_NOTE_IDENTITY.fullmatch(parsed.fragment))
    return (
        decorated
        or bool(_ANNOTATION_HINT.search(hint))
        or numbered_note_fragment
        or short_n_fragment
    )


def _range_marker_node(link: Tag) -> Tag | None:
    """Recognize high-confidence trailing note markers without deleting semantic
    super/subscripts.
    Chemical subscripts and formula exponents are body content, not removable merely because
    they use sup/sub. Accept only trailing number-like markers supported by href, id, class,
    semantic attributes or decorations.
    """
    significant = [
        child for child in link.children if not (_is_text_string(child) and not str(child).strip())
    ]
    if not significant:
        return None
    candidate = significant[-1]
    if not isinstance(candidate, Tag) or candidate.name not in {"sup", "sub"}:
        return None
    marker_text = candidate.get_text("", strip=True)
    if not marker_text or not _ANNOTATION_MARKER_ONLY.fullmatch(marker_text):
        return None

    # Numeric subscripts are usually chemical or mathematical content. Treat sub as a note marker
    # only when clear decorations such as brackets, asterisks or arrows support that interpretation.
    decorated = bool(re.search(r"[^\d\s.·:\-]", marker_text))
    if candidate.name == "sub" and not decorated:
        return None
    return (
        candidate
        if _has_annotation_hint(link, candidate, marker_text, allow_short_n=True)
        else None
    )


def _semantic_link_text(link: Tag, marker_node: Tag | None = None) -> str:
    """Return link body text, excluding only confirmed trailing annotation numbers."""
    parts: list[str] = []

    def collect(parent: Tag) -> None:
        for child in parent.children:
            if isinstance(child, Tag):
                if child is marker_node or child.name in {"rt", "rp"}:
                    continue
                collect(child)
            elif _is_text_string(child):
                parts.append(str(child))

    collect(link)
    return re.sub(r"[ \t\r\n\f\v]+", " ", "".join(parts)).strip()


def _annotation_roots(
    block: Tag,
    anchor: str,
    resource_href: str,
) -> dict[int, dict[str, object]]:
    """Identify inline links, number their DOM roots and return temporary extraction
    specifications.
    """
    # When block itself is an ordinary link, such as li > a, replacing its child text preserves href.
    # No model alignment is needed. Record the root only if it also contains sup/sub note markers
    # that clear() would otherwise erase.
    links: list[Tag] = []
    if block.name == "a" and block.has_attr("href") and block.find(["sup", "sub"]):
        links.append(block)
    links.extend(block.find_all("a", href=True))

    roots: dict[int, dict[str, object]] = {}
    ordinal = 0
    for link in links:
        if not _is_internal_link(link):
            continue

        marker_wrapper = _nearest_marker_wrapper(link, block)
        if marker_wrapper is not None:
            wrapper_text = marker_wrapper.get_text("", strip=True)
            if not _has_annotation_hint(
                link,
                marker_wrapper,
                wrapper_text,
                allow_short_n=True,
            ):
                marker_wrapper = None
        range_marker = None if marker_wrapper is not None else _range_marker_node(link)
        semantic_text = _semantic_link_text(link, range_marker)
        if not semantic_text and marker_wrapper is None:
            # Image-only links and empty anchors have no text needing cross-language alignment. Preserve
            # the entire link through atomic inline handling instead of misclassifying and clearing it as a note.
            continue
        marker_shaped = bool(_ANNOTATION_MARKER_ONLY.fullmatch(semantic_text))
        marker_only = bool(
            marker_shaped
            and (
                _has_annotation_hint(link, link, semantic_text) or _inside_implicit_note_body(link)
            )
        )
        mode = "point" if marker_wrapper is not None or marker_only else "range"
        root = marker_wrapper if mode == "point" and marker_wrapper is not None else link
        raw_href = str(link.get("href") or "")
        resolved = resolve_epub_href(resource_href, raw_href)
        relation = _annotation_relation(
            link,
            block,
            marker_wrapper=marker_wrapper,
            range_marker=range_marker,
            marker_only=marker_only,
        )

        # Record each structural root once. Valid XHTML does not nest links, but this guard prevents
        # malformed documents from assigning multiple IDs to the same sup/sub marker.
        if id(root) in roots:
            continue

        annotation_id = f"{anchor}_annotation_{ordinal}"
        ordinal += 1
        if mode == "point":
            marker_text = root.get_text("", strip=True)
        else:
            marker_text = range_marker.get_text("", strip=True) if range_marker is not None else ""
        root[_ANNOTATION_ID_ATTR] = annotation_id
        roots[id(root)] = {
            "id": annotation_id,
            "mode": mode,
            "marker_text": marker_text,
            "raw_href": raw_href,
            "target_key": resolved.target_key,
            "relation": relation,
            "marker_node_ids": {id(range_marker)} if range_marker is not None else set(),
            "root": root,
        }
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
                        append_text(f"{_RUBY_MARK_LEFT}{reading}{_RUBY_MARK_RIGHT}")
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
        node[_INLINE_ID_ATTR] = inline_id
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
        meta[_INLINE_META_KEY] = {
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
        meta[_ANNOTATION_META_KEY] = {
            "version": 1,
            "source_length": source_length,
            "items": annotation_items,
        }
    return text, meta


def strip_ruby_markers(text: str) -> str:
    """Strip pronunciation hint markers for glossary matching and accidental-copy recovery."""
    if not text or _RUBY_MARK_LEFT not in text:
        return text
    return _RUBY_MARK_RE.sub("", text)


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
        wrapper[_LINE_WRAPPER_ATTR] = "true"
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


def _find_opf_path(zf: zipfile.ZipFile) -> str:
    """Resolve the EPUB package document ZIP path from container.xml."""
    data = zf.read(_CONTAINER)
    root = ET.fromstring(data)
    # Match local names because container.xml uses a default namespace.
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == "rootfile":
            path = el.attrib.get("full-path", "").strip()
            if path:
                return path
    raise ValueError("Invalid EPUB: container.xml has no valid rootfile full-path")


def _zip_href(base_path: str, href: str) -> str:
    """Resolve an EPUB-relative href to a normalized zip member path."""
    return resolve_epub_href(base_path, href).resource_href


def _parse_opf(zf: zipfile.ZipFile, opf_path: str) -> tuple[str, list[str], list[str]]:
    """Return the book title, spine-ordered XHTML ZIP paths and TOC/NAV paths."""
    root = ET.fromstring(zf.read(opf_path))

    def local(tag: str) -> str:
        """Remove the XML namespace and return the local tag name."""
        return tag.rsplit("}", 1)[-1]

    title = ""
    manifest: dict[str, tuple[str, str, str]] = {}  # id -> (href, media-type, properties)
    spine_ids: list[str] = []
    toc_ids: list[str] = []

    for el in root.iter():
        name = local(el.tag)
        if name == "title" and not title and el.text:
            title = el.text.strip()
        elif name == "item":
            item_id = el.attrib.get("id", "").strip()
            if not item_id:
                continue
            manifest[item_id] = (
                el.attrib.get("href", ""),
                el.attrib.get("media-type", ""),
                el.attrib.get("properties", ""),
            )
        elif name == "itemref":
            idref = el.attrib.get("idref", "").strip()
            if idref:
                spine_ids.append(idref)
        elif name == "spine":
            toc = el.attrib.get("toc")
            if toc:
                toc_ids.append(toc)

    hrefs: list[str] = []
    for sid in spine_ids:
        if sid not in manifest:
            continue
        href, media, _props = manifest[sid]
        if "html" not in media and not href.endswith((".xhtml", ".html", ".htm")):
            continue
        resolved_href = _zip_href(opf_path, href)
        if resolved_href and resolved_href not in hrefs:
            # The spine may reference one physical resource repeatedly, but the ZIP contains only one XHTML.
            # Annotate it once to avoid a second set of anchors that cannot be backfilled.
            hrefs.append(resolved_href)

    # Prefer EPUB3 NAV as the main TOC, then the EPUB2 NCX named by spine.toc. Retain other TOCs
    # for title backfill without mixing their chapter boundaries with those of the main TOC.
    nav_ids = [
        item_id for item_id, (_href, _media, props) in manifest.items() if "nav" in props.split()
    ]
    ncx_ids = [
        item_id
        for item_id, (_href, media, _props) in manifest.items()
        if media == "application/x-dtbncx+xml"
    ]
    ordered_toc_ids = nav_ids + toc_ids + ncx_ids
    toc_paths: list[str] = []
    for item_id in ordered_toc_ids:
        if item_id not in manifest:
            continue
        href = _zip_href(opf_path, manifest[item_id][0])
        if href and href not in toc_paths:
            toc_paths.append(href)
    return title, hrefs, toc_paths


def _manifest_xhtml_hrefs(zf: zipfile.ZipFile, opf_path: str) -> list[str]:
    """List all OPF XHTML/HTML resources so annotations outside the spine can be parsed."""
    root = ET.fromstring(zf.read(opf_path))
    hrefs: list[str] = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "item":
            continue
        raw_href = element.attrib.get("href", "").strip()
        media_type = element.attrib.get("media-type", "").strip().lower()
        path = urlsplit(raw_href).path.lower()
        if "html" not in media_type and not path.endswith((".xhtml", ".html", ".htm")):
            continue
        href = _zip_href(opf_path, raw_href)
        if href and href not in hrefs:
            hrefs.append(href)
    return hrefs


def _decode_markup(data: bytes) -> str:
    """Decode XHTML using declarations and byte signatures; use UTF-8 replacement only as a
    last resort.
    """
    decoded = UnicodeDammit(data).unicode_markup
    return decoded if decoded is not None else data.decode("utf-8", errors="replace")


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


def _fragment_anchor_map(template: str) -> dict[str, str | None]:
    """Map XHTML id/name values to Segment anchors.
    None means the ID exists after the resource's final translatable block. Keep this
    distinct from a fragment that does not exist at all.
    """
    soup = BeautifulSoup(template, "html.parser")
    mapping: dict[str, str | None] = {}
    for node in soup.find_all(True):
        identifiers = [node.get("id"), node.get("name")]
        if not any(isinstance(value, str) and value for value in identifiers):
            continue
        block = (
            node if node.has_attr("data-tn-id") else node.find_parent(attrs={"data-tn-id": True})
        )
        if not isinstance(block, Tag):
            block = node.find_next(attrs={"data-tn-id": True})
        raw_anchor = block.get("data-tn-id") if isinstance(block, Tag) else None
        anchor = raw_anchor if isinstance(raw_anchor, str) and raw_anchor else None
        for value in identifiers:
            if isinstance(value, str) and value:
                mapping.setdefault(value, anchor)
    return mapping


def _fragment_nodes(soup: BeautifulSoup, fragment: str) -> list[Tag]:
    """Return unique DOM nodes exactly matching a fragment, retaining duplicate IDs for
    ambiguity checks.
    """
    nodes: list[Tag] = []
    seen: set[int] = set()
    for node in soup.find_all(True):
        if node.get("id") != fragment and node.get("name") != fragment:
            continue
        if id(node) not in seen:
            seen.add(id(node))
            nodes.append(node)
    return nodes


def _semantic_note_scope(node: Tag) -> Tag | None:
    """Find the nearest explicit footnote/endnote semantic container enclosing the destination
    anchor.
    """
    for candidate in (node, *node.parents):
        if isinstance(candidate, Tag) and _semantic_tokens(candidate) & _NOTE_BODY_SEMANTICS:
            return candidate
    return None


def _note_context_scope(node: Tag, fragment: str) -> tuple[Tag, bool]:
    """Select an annotation body's DOM scope and report whether its note identity is explicit."""
    semantic = _semantic_note_scope(node)
    if semantic is not None:
        return semantic, True

    implicit = _implicit_note_body_scope(node)
    if implicit is not None and (
        implicit.has_attr("data-tn-id") or implicit.find(True, attrs={"data-tn-id": True})
    ):
        return implicit, True

    short_scope = _short_note_body_scope(node)
    if short_scope is not None and (
        short_scope.has_attr("data-tn-id") or short_scope.find(True, attrs={"data-tn-id": True})
    ):
        # An n1 ID alone cannot promote an ordinary link to a note, but a confirmed noteref can use it
        # to obtain the complete list annotation instead of just the numbered anchor.
        return short_scope, False

    # Older EPUBs often wrap multi-paragraph notes in li/dd/aside without semantic attributes.
    # A div/section with explicit note/fn identity can also safely supply all its body blocks.
    has_note_identity = bool(_NOTE_TARGET_HINT.search(fragment)) or _has_note_identity(node)
    if node.name in {"aside", "li", "dd"} or (
        node.name in {"div", "section"} and has_note_identity
    ):
        if node.has_attr("data-tn-id") or node.find(True, attrs={"data-tn-id": True}):
            return node, False

    if node.has_attr("data-tn-id"):
        return node, False
    parent = node.find_parent(attrs={"data-tn-id": True})
    if isinstance(parent, Tag):
        return parent, False
    following = node.find_next(attrs={"data-tn-id": True})
    return (following, False) if isinstance(following, Tag) else (node, False)


def _scope_segment_anchors(scope: Tag) -> list[str]:
    """Return translation-block anchors within the annotation scope in DOM order."""
    candidates = [scope] if scope.has_attr("data-tn-id") else []
    candidates.extend(scope.find_all(True, attrs={"data-tn-id": True}))
    anchors: list[str] = []
    for candidate in candidates:
        raw_anchor = candidate.get("data-tn-id")
        if isinstance(raw_anchor, str) and raw_anchor and raw_anchor not in anchors:
            anchors.append(raw_anchor)
    return anchors


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
            raw_annotations = segment.meta.get(_ANNOTATION_META_KEY)
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
                target_nodes = _fragment_nodes(target_soup, resolved.fragment)
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

                anchors = _scope_segment_anchors(scope)
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


def peek_epub_title(path: str) -> str:
    """Read the book title from OPF without annotating resources, for locating existing state.
    Parse only container.xml and OPF, not XHTML body text. Use the same Document.title rule
    as read_epub, including filename-stem fallback, so state lookup agrees with preparation.
    """

    with zipfile.ZipFile(path, "r") as zf:
        opf_path = _find_opf_path(zf)
        book_title, _hrefs, _toc_paths = _parse_opf(zf, opf_path)
    return book_title or os.path.splitext(os.path.basename(path))[0]


def read_epub(path: str, source_lang: str, target_lang: str) -> Document:
    """Read physical spine resources and construct logical chapters from top-level TOC anchors."""
    with zipfile.ZipFile(path, "r") as zf:
        names = set(zf.namelist())
        opf_path = _find_opf_path(zf)
        book_title, hrefs, toc_paths = _parse_opf(zf, opf_path)
        manifest_xhtml_hrefs = _manifest_xhtml_hrefs(zf, opf_path)
        toc_entries = parse_toc_entries(zf, toc_paths)

        resources: list[dict[str, object]] = []
        for resource_index, href in enumerate(hrefs):
            if href not in names:
                continue
            html = _decode_markup(zf.read(href))
            title, segments, template = annotate_epub_resource(
                html,
                resource_index,
                href,
                book_title=book_title,
                skip_navigation=href in toc_paths,
            )
            resources.append(
                {
                    "index": resource_index,
                    "href": href,
                    "title": title,
                    "segments": segments,
                    "template": template,
                    "fragment_anchors": _fragment_anchor_map(template),
                }
            )

        # Annotation bodies may appear in the manifest without a spine reference. They do not become
        # formal chapters or backfill resources, but can supply immutable context to referring paragraphs.
        auxiliary_resources: list[dict[str, object]] = []
        spine_hrefs = {str(resource["href"]) for resource in resources}
        for auxiliary_ordinal, href in enumerate(manifest_xhtml_hrefs):
            if href in spine_hrefs or href not in names or href in toc_paths:
                continue
            html = _decode_markup(zf.read(href))
            title, segments, template = annotate_epub_resource(
                html,
                len(hrefs) + auxiliary_ordinal,
                href,
                book_title=book_title,
            )
            auxiliary_resources.append(
                {
                    "index": len(hrefs) + auxiliary_ordinal,
                    "href": href,
                    "title": title,
                    "segments": segments,
                    "template": template,
                }
            )
        annotation_contexts = _build_epub_annotation_contexts(
            resources,
            [*resources, *auxiliary_resources],
        )
        chapters, split_strategy, split_toc_path = _logical_chapters(resources, toc_entries)
        # Rebuild XHTML templates and inline layout deterministically from the original EPUB, not state.
        # Preserve other format metadata and fields added by later stages unchanged.
        for chapter in chapters:
            chapter.template = None
            for segment in chapter.segments:
                segment.meta.pop(_INLINE_META_KEY, None)

    return Document(
        title=book_title or os.path.splitext(os.path.basename(path))[0],
        source_lang=source_lang,
        target_lang=target_lang,
        fmt="epub",
        source_path=os.path.abspath(path),
        chapters=chapters,
        meta={
            "epub_schema": 5,
            "opf_path": opf_path,
            "toc_paths": toc_paths,
            "toc_entries": toc_entries,
            "epub_resources": [
                {"index": resource["index"], "href": resource["href"]} for resource in resources
            ],
            "epub_split_strategy": split_strategy,
            "epub_split_toc_path": split_toc_path,
            "epub_annotation_contexts": annotation_contexts,
        },
    )
