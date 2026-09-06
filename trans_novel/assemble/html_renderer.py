"""Render Chapter/Segment translations into BeautifulSoup DOM nodes.
Backfill data-tn-id anchors, merge continuations, insert bilingual source text, preserve
Japanese ruby, and restore annotation links and inline elements. Rendering changes only the
DOM and returns HTML without file I/O.
"""

from __future__ import annotations

import hashlib
from html import escape

from bs4 import BeautifulSoup
from bs4.element import Comment, ProcessingInstruction, Tag

from ..ingest.epub_toc import resolve_epub_href
from ..ingest.models import KIND_HEADING, Chapter, Segment
from .writer_common import _bilingual_source, _ordered_pair, _seg_text

# Bilingual source style ID for injection and detection in head.
_BILINGUAL_STYLE_ID = "tn-bilingual-style"

# Muted source styles with dark-mode support.
_BILINGUAL_CSS = """\
.tn-source {
  font-size: 0.88em;
  line-height: 1.55;
  color: #6b6b6b;
  background-color: #f4f3f0;
  padding: 0.5em 0.8em;
  border-radius: 5px;
  margin: 0.2em 0 1em;
}
@media (prefers-color-scheme: dark) {
  .tn-source {
    color: #a8a8a8;
    background-color: #2a2a2a;
    box-shadow: inset 0 0 0 1px rgba(255,255,255,0.14);
  }
}
"""

# Metadata keys and attributes for inline elements such as images.
_INLINE_META_KEY = "epub_inline"
_INLINE_ID_ATTR = "data-tn-inline-id"
_ANNOTATION_META_KEY = "epub_annotations"
_ANNOTATION_ID_ATTR = "data-tn-annotation-id"
_LINE_WRAPPER_ATTR = "data-tn-line"
_SOURCE_ANCHOR_PREFIX = "tn-source-"


def _render_paragraph_html(
    kind: str,
    target: str,
    source: str,
    *,
    bilingual: bool,
    order: str,
    preserve_source_style: bool = True,
    heading_level: int | None = None,
) -> list[str]:
    """Render one paragraph into HTML fragments for EPUB chapter construction.
    Use h1 for headings when heading_level is None, otherwise h{level}. With
    preserve_source_style, use only tn-source; otherwise add
    ibooks-dark-theme-use-custom-text-color.
    """
    if kind == KIND_HEADING:
        level = heading_level if heading_level is not None else 1
        target_html = f"<h{level}>{escape(target)}</h{level}>"
    else:
        target_html = f"<p>{escape(target)}</p>"
    src = _bilingual_source(source, target) if (bilingual and kind != KIND_HEADING) else ""
    if not src:
        return [target_html]
    source_class = (
        "tn-source"
        if preserve_source_style
        else "tn-source ibooks-dark-theme-use-custom-text-color"
    )
    src_html = f'<p class="{source_class}">{escape(src)}</p>'
    first, second = _ordered_pair(src_html, target_html, order)
    return [first, second]


def _bilingual_source_markup(
    element: Tag,
    source_lang: str,
    *,
    resource_href: str,
    source_link_targets: dict[tuple[str, str], str],
) -> str:
    """Preserve annotation links and Japanese ruby in bilingual source text.
    Source links already have accurate positions in the original EPUB, so target placements
    are unnecessary. Keep annotation roots and descendants while flattening other inline
    tags to clean text. Remove cloned id/name attributes to avoid duplicate anchors on the
    target side.
    """
    normalized_lang = source_lang.strip().replace("_", "-").lower()
    keep_ruby = normalized_lang == "ja" or normalized_lang.startswith("ja-")
    has_annotation = (
        element.has_attr(_ANNOTATION_ID_ATTR)
        or element.find(True, attrs={_ANNOTATION_ID_ATTR: True}) is not None
    )
    if not has_annotation and (not keep_ruby or element.find("ruby") is None):
        return ""

    fragment = BeautifulSoup(str(element), "html.parser")
    root = fragment.find(element.name)
    if not isinstance(root, Tag):
        return ""

    root_is_annotation = root.has_attr(_ANNOTATION_ID_ATTR)
    retained: set[int] = set()
    for annotation in root.find_all(True, attrs={_ANNOTATION_ID_ATTR: True}):
        retained.add(id(annotation))
        retained.update(id(descendant) for descendant in annotation.find_all(True))
    if root_is_annotation:
        retained.add(id(root))
        retained.update(id(descendant) for descendant in root.find_all(True))
    if keep_ruby:
        for ruby in root.find_all("ruby"):
            retained.add(id(ruby))
            retained.update(id(descendant) for descendant in ruby.find_all(True))

    for comment in list(root.find_all(string=lambda node: isinstance(node, Comment))):
        comment.extract()
    for tag in list(
        root.find_all(
            [
                "audio",
                "canvas",
                "embed",
                "hr",
                "iframe",
                "img",
                "math",
                "object",
                "script",
                "source",
                "style",
                "svg",
                "video",
            ]
        )
    ):
        tag.decompose()

    if not keep_ruby:
        for tag in list(root.find_all(["rt", "rp"])):
            tag.decompose()

    for tag in list(root.find_all(True)):
        if id(tag) not in retained:
            tag.unwrap()
            continue
        for attr in (
            "id",
            "name",
            "data-tn-id",
            _INLINE_ID_ATTR,
            _ANNOTATION_ID_ATTR,
            _LINE_WRAPPER_ATTR,
        ):
            tag.attrs.pop(attr, None)
    for attr in (
        "id",
        "name",
        "data-tn-id",
        _INLINE_ID_ATTR,
        _ANNOTATION_ID_ATTR,
        _LINE_WRAPPER_ATTR,
    ):
        root.attrs.pop(attr, None)

    # Translations retain original fragments. Rewrite source mirrors to synthetic anchors only when
    # the destination also has a source block. Preserve paths and queries so cross-XHTML links
    # keep their original relative resolution; preserve unmapped links to avoid dangling anchors.
    links = [root] if root.name == "a" else []
    links.extend(root.find_all("a", href=True))
    for link in links:
        raw_href = link.get("href")
        if not isinstance(raw_href, str):
            continue
        resolved = resolve_epub_href(resource_href, raw_href)
        source_anchor = source_link_targets.get((resolved.resource_href, resolved.fragment))
        if resolved.external or not resolved.fragment or not source_anchor:
            continue
        path_and_query, separator, _fragment = raw_href.partition("#")
        if separator:
            link["href"] = f"{path_and_query}#{source_anchor}"
    return str(root) if root_is_annotation else root.decode_contents()


def _append_source(soup: BeautifulSoup, element: Tag, source: str, markup: str) -> None:
    """Write plain text or sanitized annotation/ruby markup into a bilingual source block."""
    if not markup:
        element.append(source)
        return
    fragment = BeautifulSoup(markup, "html.parser")
    for child in list(fragment.contents):
        element.append(child.extract())


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
    stored_raw = stored.get(_ANNOTATION_META_KEY)
    fresh_raw = fresh.get(_ANNOTATION_META_KEY)
    stored_annotations = stored_raw if isinstance(stored_raw, dict) else {}
    fresh_annotations = fresh_raw if isinstance(fresh_raw, dict) else {}
    if stored_annotations or fresh_annotations:
        annotations = dict(stored_annotations)
        annotations.update(fresh_annotations)
        for key in ("target_digest", "placements"):
            if key in stored_annotations:
                annotations[key] = stored_annotations[key]
        merged[_ANNOTATION_META_KEY] = annotations
    return merged


def _clean_annotation_attrs(node: Tag) -> None:
    """Remove temporary backfill attributes so they cannot leak into the exported EPUB."""
    node.attrs.pop(_ANNOTATION_ID_ATTR, None)
    for descendant in node.find_all(True, attrs={_ANNOTATION_ID_ATTR: True}):
        descendant.attrs.pop(_ANNOTATION_ID_ATTR, None)


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
    raw_annotations = meta.get(_ANNOTATION_META_KEY)
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
    for order, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        annotation_id = item.get("id")
        mode = item.get("mode")
        if not isinstance(annotation_id, str) or mode not in {"point", "range"}:
            continue
        root = el.find(True, attrs={_ANNOTATION_ID_ATTR: annotation_id})
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
        rejected = {"fallback", "failed", "invalid", "missing", "stale"}
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
        if not usable:
            source_start = item.get("source_start")
            source_end = item.get("source_end")
            source_length = annotations.get("source_length")
            # Only whole-source wraps (e.g. <h1><a>CHAPTER 1</a></h1>) keep the
            # translation inside the link. Partial inline links still fall back to ↩.
            covers_whole_source = (
                mode == "range"
                and not marker_text
                and len(items) == 1
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
            fallbacks.append(_fallback_annotation_node(root, mode=mode, marker_text=marker_text))
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
        fallbacks.append(_fallback_annotation_node(root, mode=mode, marker_text=marker_text))

    # HTML links cannot cross or nest. Degrade invalid ranges to avoid producing a broken DOM.
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
        if any(start < offset < end for start, end, _order, _root, _markers in ranges):
            fallbacks.append(root)
        else:
            safe_points.append((offset, order, root))
    return safe_points, ranges, fallbacks


def _render_text_with_nodes(
    soup: BeautifulSoup,
    el: Tag,
    text: str,
    nodes: list[tuple[int, int, Tag]],
    ranges: list[tuple[int, int, int, Tag, list[Tag]]],
    fallbacks: list[Tag],
) -> None:
    """Backfill inline nodes, annotation points and nonoverlapping ranges at target offsets."""
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
    cursor = 0
    for start, end, _order, root, markers in sorted(ranges):
        append_until(el, cursor, start)
        root.clear()
        # Point annotations at a range's end are siblings after the link, not nested anchors inside it.
        append_until(root, start, end, include_end=False)
        for marker in markers:
            root.append(marker)
        el.append(root)
        cursor = end
    append_until(el, cursor, len(text))
    # Adjacent fallback links at paragraph ends have no separating text, so footnote numbers can
    # merge (11, 12, 13 becomes 111213). Insert a separator to keep them readable.
    for index, fallback in enumerate(fallbacks):
        if index > 0:
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
    self_annotation_id = el.get(_ANNOTATION_ID_ATTR)
    if isinstance(self_annotation_id, str):
        raw_annotations = meta.get(_ANNOTATION_META_KEY)
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
        el.attrs.pop(_ANNOTATION_ID_ATTR, None)
    raw_inline = meta.get(_INLINE_META_KEY)
    inline = raw_inline if isinstance(raw_inline, dict) else {}
    raw_nodes = inline.get("nodes")
    nodes = raw_nodes if isinstance(raw_nodes, list) else []
    source_length = inline.get("source_length")
    if not isinstance(source_length, int) or source_length < 0:
        source_length = 0

    restored: list[tuple[int, int, Tag]] = []
    for order, record in enumerate(nodes):
        if not isinstance(record, dict):
            continue
        inline_id = record.get("id")
        offset = record.get("offset")
        if not isinstance(inline_id, str) or not isinstance(offset, int):
            continue
        node = el.find(True, attrs={_INLINE_ID_ATTR: inline_id})
        if not isinstance(node, Tag):
            continue
        node.extract()
        node.attrs.pop(_INLINE_ID_ATTR, None)
        if offset <= 0:
            target_offset = 0
        elif source_length <= 0 or offset >= source_length:
            target_offset = len(text)
        else:
            target_offset = round(offset * len(text) / source_length)
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
    )
    for marker in self_markers:
        el.append(marker)


def _segment_render_maps(
    segments: list[Segment],
) -> tuple[
    dict[str, str],
    dict[str, str],
    dict[str, str],
    dict[str, dict[str, object]],
]:
    """Merge continuations by anchor and return target, source, kind and persisted metadata
    mappings.
    """
    by_anchor: dict[str, str] = {}
    src_by_anchor: dict[str, str] = {}
    kind_by_anchor: dict[str, str] = {}
    stored_meta_by_anchor: dict[str, dict[str, object]] = {}
    current_anchor: str | None = None
    for segment in segments:
        if segment.cont and current_anchor is not None:
            by_anchor[current_anchor] += _seg_text(segment)
            src_by_anchor[current_anchor] += segment.source
        elif segment.anchor:
            current_anchor = segment.anchor
            by_anchor[current_anchor] = _seg_text(segment)
            src_by_anchor[current_anchor] = segment.source
            kind_by_anchor[current_anchor] = segment.kind
            stored_meta_by_anchor[current_anchor] = segment.meta
    return by_anchor, src_by_anchor, kind_by_anchor, stored_meta_by_anchor


def _index_soup_ids(soup: BeautifulSoup) -> tuple[set[str], dict[str, Tag]]:
    """Build occupied id/name sets and the data-tn-id index in one find_all traversal.
    Repeated soup.find calls make backfill scale with paragraph count times DOM size. Index
    the page once so subsequent anchor lookup is O(1).
    """
    occupied: set[str] = set()
    tn_id_index: dict[str, Tag] = {}
    for node in soup.find_all(True):
        for attr in ("id", "name"):
            value = node.get(attr)
            if isinstance(value, str) and value:
                occupied.add(value)
        tn_id = node.get("data-tn-id")
        if isinstance(tn_id, str) and tn_id:
            tn_id_index[tn_id] = node
    return occupied, tn_id_index


def _build_source_anchor_ids(
    by_anchor: dict[str, str],
    src_by_anchor: dict[str, str],
    kind_by_anchor: dict[str, str],
    tn_id_index: dict[str, Tag],
    occupied: set[str],
) -> dict[str, str]:
    """Assign stable synthetic IDs to actual source blocks without colliding with original IDs."""
    occupied = set(
        occupied
    )  # Copy the caller's set because this function adds every newly allocated ID.
    assigned: dict[str, str] = {}
    for anchor, target in by_anchor.items():
        if kind_by_anchor.get(anchor) == KIND_HEADING:
            continue
        source = _bilingual_source(src_by_anchor.get(anchor, ""), target)
        if not source or anchor not in tn_id_index:
            continue
        base = f"{_SOURCE_ANCHOR_PREFIX}{anchor}"
        candidate = base
        suffix = 2
        while candidate in occupied:
            candidate = f"{base}-{suffix}"
            suffix += 1
        assigned[anchor] = candidate
        occupied.add(candidate)
    return assigned


def _render_segments_html(
    template: str,
    segments: list[Segment],
    *,
    render_meta_by_anchor: dict[str, dict[str, object]] | None = None,
    bilingual: bool = False,
    order: str = "target_first",
    preserve_source_style: bool = False,
    source_lang: str = "",
    resource_href: str = "",
    source_ids_by_anchor: dict[str, str] | None = None,
    source_link_targets: dict[tuple[str, str], str] | None = None,
) -> str:
    """Backfill translations once per physical HTML resource, indexed by anchor.
    Logical EPUB chapters may share one XHTML or span several. Callers must first group
    segments by resource_href, since physical resources are the backfill unit.
    With preserve_source_style, reuse original class/style attributes without muted CSS;
    retain tn-source only as a structural marker.
    """
    soup = BeautifulSoup(template, "html.parser")
    by_anchor, src_by_anchor, kind_by_anchor, stored_meta_by_anchor = _segment_render_maps(segments)
    # Index once so anchor lookups are O(1), avoiding a full DOM scan with soup.find for every anchor.
    # Reuse the resulting index throughout this resource.
    occupied_ids, tn_id_index = _index_soup_ids(soup)
    if bilingual and source_ids_by_anchor is None:
        source_ids_by_anchor = _build_source_anchor_ids(
            by_anchor,
            src_by_anchor,
            kind_by_anchor,
            tn_id_index,
            occupied_ids,
        )
    source_ids_by_anchor = source_ids_by_anchor or {}
    if bilingual and source_link_targets is None:
        # Direct calls support same-XHTML links. Complete EPUB exports supply the book-wide mapping
        # to support links across resources as well.
        from ..ingest.epub_reader import _fragment_anchor_map

        source_link_targets = {
            (resource_href, fragment): source_ids_by_anchor[segment_anchor]
            for fragment, segment_anchor in _fragment_anchor_map(template).items()
            if fragment
            and isinstance(segment_anchor, str)
            and segment_anchor in source_ids_by_anchor
        }
    source_link_targets = source_link_targets or {}
    for anchor, text in by_anchor.items():
        el = tn_id_index.get(anchor)
        if el is None:
            continue
        src = (
            _bilingual_source(src_by_anchor.get(anchor, ""), text)
            if bilingual and kind_by_anchor.get(anchor) != KIND_HEADING
            else ""
        )
        source_markup = (
            _bilingual_source_markup(
                el,
                source_lang,
                resource_href=resource_href,
                source_link_targets=source_link_targets,
            )
            if src
            else ""
        )
        line_wrapper = el.has_attr(_LINE_WRAPPER_ATTR)
        stored_meta = stored_meta_by_anchor.get(anchor, {})
        fresh_meta = (
            render_meta_by_anchor.get(anchor, {}) if render_meta_by_anchor is not None else {}
        )
        render_meta = _merge_epub_render_meta(stored_meta, fresh_meta)
        if text != src_by_anchor.get(anchor, ""):
            _replace_block_content(soup, el, text, render_meta)
        del el["data-tn-id"]
        if not src:
            continue
        # Source text for p can be an adjacent paragraph. Keep li/blockquote content inside its container
        # to avoid invalid structures such as <ul><li>...</li><p>...</p></ul>
        # and preserve blockquote semantics and styling.
        nested_source = el.name in {"li", "blockquote"}
        src_el = soup.new_tag("span" if line_wrapper else "div" if nested_source else "p")
        source_classes = ["tn-source"]
        if preserve_source_style:
            original_classes = el.get("class")
            if isinstance(original_classes, list):
                source_classes = [str(value) for value in original_classes]
                if "tn-source" not in source_classes:
                    source_classes.append("tn-source")
            original_style = el.get("style")
            if isinstance(original_style, str):
                src_el["style"] = original_style
        else:
            source_classes.append("ibooks-dark-theme-use-custom-text-color")
        src_el["class"] = " ".join(source_classes)
        source_id = source_ids_by_anchor.get(anchor)
        if source_id:
            src_el["id"] = source_id
        _append_source(soup, src_el, src, source_markup)
        if line_wrapper and order == "source_first":
            el.insert_before(src_el)
            src_el.insert_after(soup.new_tag("br"))
        elif line_wrapper:
            el.insert_after(src_el)
            el.insert_after(soup.new_tag("br"))
        elif nested_source and order == "source_first":
            el.insert(0, src_el)
        elif nested_source:
            el.append(src_el)
        elif order == "source_first":
            el.insert_before(src_el)
        else:
            el.insert_after(src_el)
    # Line-break wrappers provide temporary backfill anchors; unwrap them afterward for a clean DOM.
    for wrapper in list(soup.find_all(True, attrs={_LINE_WRAPPER_ATTR: True})):
        wrapper.unwrap()
    for node in soup.find_all(True, attrs={_ANNOTATION_ID_ATTR: True}):
        node.attrs.pop(_ANNOTATION_ID_ATTR, None)
    for node in soup.find_all(True, attrs={_INLINE_ID_ATTR: True}):
        node.attrs.pop(_INLINE_ID_ATTR, None)
    return str(soup)


def _render_chapter_html(
    chapter: Chapter,
    *,
    bilingual: bool = False,
    order: str = "target_first",
    preserve_source_style: bool = False,
    source_lang: str = "",
) -> str:
    """Backfill a legacy HTML/EPUB chapter with one template per chapter.
    This wrapper remains for ordinary HTML output and EPUB state from before 0.3.x. Newer
    EPUB state is grouped by physical resource through _render_segments_html.
    """
    return _render_segments_html(
        chapter.template or "",
        chapter.segments,
        bilingual=bilingual,
        order=order,
        preserve_source_style=preserve_source_style,
        source_lang=source_lang,
        resource_href=chapter.href or "",
    )
