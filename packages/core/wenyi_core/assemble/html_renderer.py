"""Coordinate segment and chapter rendering against stable markup anchors."""

from __future__ import annotations

from html import escape

from bs4 import BeautifulSoup

from wenyi_core.assemble.html_bilingual import (
    _append_source,
    _bilingual_source_markup,
    _build_source_anchor_ids,
    _build_source_fragment_ids,
    _index_soup_ids,
)
from wenyi_core.assemble.html_inline import _merge_epub_render_meta, _replace_block_content
from wenyi_core.assemble.writer_common import _bilingual_source, _ordered_pair, _seg_text
from wenyi_core.markup.contracts import (
    ANNOTATION_ID_ATTR,
    HTML_ATTRIBUTE_ID_ATTR,
    HTML_ATTRIBUTE_META_KEY,
    HTML_MARKUP_META_KEY,
    INLINE_ID_ATTR,
    LINE_WRAPPER_ATTR,
)
from wenyi_core.markup.html import html_attribute_names
from wenyi_core.models.document import KIND_HEADING, Chapter, Segment


def _html_open_tag(name: str, attrs: object, **overrides: str) -> str:
    """Serialize preserved shell attributes using the parser's escaping rules."""
    element = BeautifulSoup("", "html.parser").new_tag(name)
    if isinstance(attrs, dict):
        element.attrs.update(attrs)
    element.attrs.update(overrides)
    if "lang" in overrides and element.has_attr("xml:lang"):
        element["xml:lang"] = overrides["lang"]
    return str(element).removesuffix(f"</{name}>")


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
        else:
            # Template chapters always carry anchors; reaching this branch means the
            # persisted state was truncated or corrupted. Fail loudly instead of
            # silently dropping text (audit X-4).
            raise ValueError(
                f"Chapter state is inconsistent: segment {segment.index} has no anchor"
            )
    return by_anchor, src_by_anchor, kind_by_anchor, stored_meta_by_anchor


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
    attribute_nodes = {
        node.get(HTML_ATTRIBUTE_ID_ATTR): node
        for node in soup.find_all(True, attrs={HTML_ATTRIBUTE_ID_ATTR: True})
    }
    if bilingual and source_ids_by_anchor is None:
        source_ids_by_anchor = _build_source_anchor_ids(
            by_anchor,
            src_by_anchor,
            kind_by_anchor,
            tn_id_index,
            occupied_ids,
        )
    source_ids_by_anchor = source_ids_by_anchor or {}
    source_fragments = _build_source_fragment_ids(source_ids_by_anchor, tn_id_index, occupied_ids)
    if bilingual and source_link_targets is None:
        # Direct calls support same-XHTML links. Complete EPUB exports supply the book-wide mapping
        # to support links across resources as well.
        from wenyi_core.markup.anchors import fragment_anchor_map

        source_link_targets = {
            (resource_href, fragment): source_ids_by_anchor[segment_anchor]
            for fragment, segment_anchor in fragment_anchor_map(template).items()
            if fragment
            and isinstance(segment_anchor, str)
            and segment_anchor in source_ids_by_anchor
        }
    source_link_targets = dict(source_link_targets or {})
    source_link_targets.update(
        {
            (resource_href, original): cloned
            for fragments in source_fragments.values()
            for original, cloned in fragments.items()
        }
    )
    # Snapshot every source before changing any text or translated attribute. Earlier
    # regions may otherwise mutate content that a later source block must preserve.
    source_markup_by_anchor = {
        anchor: _bilingual_source_markup(
            tn_id_index[anchor],
            resource_href=resource_href,
            source_link_targets=source_link_targets,
            source_fragment_ids=source_fragments[anchor],
        )
        for anchor in source_ids_by_anchor
    }
    for anchor, text in by_anchor.items():
        stored_meta = stored_meta_by_anchor.get(anchor, {})
        fresh_meta = (
            render_meta_by_anchor.get(anchor, {}) if render_meta_by_anchor is not None else {}
        )
        render_meta = _merge_epub_render_meta(stored_meta, fresh_meta)
        attribute = (fresh_meta if render_meta_by_anchor is not None else stored_meta).get(
            HTML_ATTRIBUTE_META_KEY
        )
        if isinstance(attribute, dict):
            node_id = attribute.get("node_id")
            name = attribute.get("name")
            node = attribute_nodes.get(node_id) if isinstance(node_id, str) else None
            if (
                node is not None
                and isinstance(name, str)
                and name in html_attribute_names(node)
                and node.has_attr(name)
            ):
                node[name] = text
            continue
        el = tn_id_index.get(anchor)
        if el is None:
            continue
        src = (
            _bilingual_source(src_by_anchor.get(anchor, ""), text)
            if bilingual and kind_by_anchor.get(anchor) != KIND_HEADING and el.name != "option"
            else ""
        )
        source_snapshot = source_markup_by_anchor.get(anchor) if src else None
        line_wrapper = el.has_attr(LINE_WRAPPER_ATTR)
        if text != src_by_anchor.get(anchor, ""):
            # Temporary HTML runs omit edge whitespace from model input. Preserve it
            # around fixed code/media without shifting aligned target offsets.
            original = (
                el.get_text() if line_wrapper and render_meta.get(HTML_MARKUP_META_KEY) else ""
            )
            leading = original[: len(original) - len(original.lstrip())]
            trailing = original[len(original.rstrip()) :]
            _replace_block_content(soup, el, text, render_meta)
            if leading:
                el.insert(0, leading)
            if trailing:
                el.append(trailing)
        del el["data-tn-id"]
        if not src:
            continue
        # Source text for p can be an adjacent paragraph. Keep li/blockquote content inside its container
        # to avoid invalid structures such as <ul><li>...</li><p>...</p></ul>
        # and preserve blockquote semantics and styling.
        html_nested_source = bool(render_meta.get(HTML_MARKUP_META_KEY)) and el.name != "p"
        nested_source = html_nested_source or el.name in {"li", "blockquote"}
        src_el = soup.new_tag(
            "span" if line_wrapper or html_nested_source else "div" if nested_source else "p"
        )
        if source_snapshot is not None and source_snapshot.name != "a":
            src_el.attrs.update(source_snapshot.attrs)
        if source_lang and not src_el.has_attr("lang"):
            src_el["lang"] = source_lang
            if src_el.has_attr("xml:lang"):
                src_el["xml:lang"] = source_lang
        source_classes = ["tn-source"]
        if preserve_source_style:
            original_classes = src_el.get("class")
            if isinstance(original_classes, list):
                source_classes = [str(value) for value in original_classes]
                if "tn-source" not in source_classes:
                    source_classes.append("tn-source")
        else:
            src_el.attrs.pop("style", None)
            source_classes.append("ibooks-dark-theme-use-custom-text-color")
        src_el["class"] = " ".join(source_classes)
        source_id = source_ids_by_anchor.get(anchor)
        if source_id:
            src_el["id"] = source_id
        _append_source(src_el, src, source_snapshot)
        if line_wrapper and order == "source_first":
            el.insert_before(src_el)
            src_el.insert_after(soup.new_tag("br"))
        elif line_wrapper:
            el.insert_after(src_el)
            el.insert_after(soup.new_tag("br"))
        elif nested_source and order == "source_first":
            if html_nested_source:
                el.insert(0, soup.new_tag("br"))
            el.insert(0, src_el)
        elif nested_source:
            if html_nested_source:
                el.append(soup.new_tag("br"))
            el.append(src_el)
        elif order == "source_first":
            el.insert_before(src_el)
        else:
            el.insert_after(src_el)
    # Line-break wrappers provide temporary backfill anchors; unwrap them afterward for a clean DOM.
    for wrapper in list(soup.find_all(True, attrs={LINE_WRAPPER_ATTR: True})):
        wrapper.unwrap()
    for node in soup.find_all(True, attrs={ANNOTATION_ID_ATTR: True}):
        node.attrs.pop(ANNOTATION_ID_ATTR, None)
    for node in soup.find_all(True, attrs={INLINE_ID_ATTR: True}):
        node.attrs.pop(INLINE_ID_ATTR, None)
    for node in soup.find_all(True, attrs={HTML_ATTRIBUTE_ID_ATTR: True}):
        node.attrs.pop(HTML_ATTRIBUTE_ID_ATTR, None)
    return str(soup)


def _render_chapter_html(
    chapter: Chapter,
    *,
    bilingual: bool = False,
    order: str = "target_first",
    preserve_source_style: bool = False,
    source_lang: str = "",
) -> str:
    """Backfill a chapter template for HTML/PDF input and generated HTML output."""
    return _render_segments_html(
        chapter.template or "",
        chapter.segments,
        bilingual=bilingual,
        order=order,
        preserve_source_style=preserve_source_style,
        source_lang=source_lang,
        resource_href=chapter.href or "",
    )
