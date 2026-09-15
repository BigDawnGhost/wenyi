"""Coordinate segment and chapter rendering against stable markup anchors."""

from __future__ import annotations

from html import escape

from bs4 import BeautifulSoup

from wenyi_core.assemble.html_bilingual import (
    _append_source,
    _bilingual_source_markup,
    _build_source_anchor_ids,
    _index_soup_ids,
)
from wenyi_core.assemble.html_inline import _merge_epub_render_meta, _replace_block_content
from wenyi_core.assemble.writer_common import _bilingual_source, _ordered_pair, _seg_text
from wenyi_core.ingest.models import KIND_HEADING, Chapter, Segment
from wenyi_core.markup.contracts import (
    ANNOTATION_ID_ATTR,
    INLINE_ID_ATTR,
    LINE_WRAPPER_ATTR,
)


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
        from wenyi_core.markup.anchors import fragment_anchor_map

        source_link_targets = {
            (resource_href, fragment): source_ids_by_anchor[segment_anchor]
            for fragment, segment_anchor in fragment_anchor_map(template).items()
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
        line_wrapper = el.has_attr(LINE_WRAPPER_ATTR)
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
    for wrapper in list(soup.find_all(True, attrs={LINE_WRAPPER_ATTR: True})):
        wrapper.unwrap()
    for node in soup.find_all(True, attrs={ANNOTATION_ID_ATTR: True}):
        node.attrs.pop(ANNOTATION_ID_ATTR, None)
    for node in soup.find_all(True, attrs={INLINE_ID_ATTR: True}):
        node.attrs.pop(INLINE_ID_ATTR, None)
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
