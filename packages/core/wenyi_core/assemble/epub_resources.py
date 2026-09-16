"""Rebuild authoritative EPUB resources and backfill each physical resource once."""

from __future__ import annotations

import zipfile

from bs4 import BeautifulSoup, UnicodeDammit

from wenyi_core.assemble.html_bilingual import (
    _build_source_anchor_ids,
    _index_soup_ids,
)
from wenyi_core.assemble.html_renderer import (
    _render_segments_html,
    _segment_render_maps,
)
from wenyi_core.ingest.models import Chapter, Segment


def _epub_resource_specs(meta: dict[str, object]) -> list[tuple[int, str]]:
    """Read the physical XHTML inventory from state, excluding damaged or duplicate
    records.
    """
    raw_resources = meta.get("epub_resources")
    if not isinstance(raw_resources, list):
        return []
    resources: list[tuple[int, str]] = []
    seen: set[str] = set()
    for fallback_index, raw_resource in enumerate(raw_resources):
        if not isinstance(raw_resource, dict):
            continue
        href = raw_resource.get("href")
        if not isinstance(href, str) or not href or href in seen:
            continue
        raw_index = raw_resource.get("index")
        resource_index = raw_index if isinstance(raw_index, int) else fallback_index
        resources.append((resource_index, href))
        seen.add(href)
    return resources


def _segments_by_resource(chapters: list[Chapter]) -> dict[str, list[Segment]]:
    """Group EPUB segments from logical chapters by physical resource in source order."""
    grouped: dict[str, list[Segment]] = {}
    for chapter in chapters:
        for segment in chapter.segments:
            href = segment.resource_href
            if href:
                grouped.setdefault(href, []).append(segment)
    return grouped


def _render_epub_resources(
    zin: zipfile.ZipFile,
    chapters: list[Chapter],
    meta: dict[str, object],
    *,
    book_title: str,
    bilingual: bool,
    order: str,
    preserve_source_style: bool,
    source_lang: str,
) -> dict[str, str]:
    """Rebuild stable templates from the original EPUB and render each physical XHTML once.
    State stores only segments and resource_href; the original EPUB remains authoritative
    for layout and inline elements. Repeating deterministic anchor annotation saves space
    compared with copying XHTML into every logical chapter and avoids multiple chapter
    writes overwriting one physical file.
    """
    resources = _epub_resource_specs(meta)
    grouped = _segments_by_resource(chapters)
    if not resources or not grouped:
        return {}
    declared_hrefs = {href for _index, href in resources}
    undeclared = sorted(set(grouped) - declared_hrefs)
    if undeclared:
        raise ValueError(
            "EPUB state refers to undeclared body resources: " + ", ".join(undeclared[:3])
        )

    from wenyi_core.markup.anchors import fragment_anchor_map
    from wenyi_core.markup.segments import annotate_epub_resource

    names = set(zin.namelist())
    raw_toc_paths = meta.get("toc_paths")
    toc_paths = (
        {path for path in raw_toc_paths if isinstance(path, str)}
        if isinstance(raw_toc_paths, list)
        else set()
    )
    prepared: dict[
        str,
        tuple[list[Segment], str, dict[str, dict[str, object]]],
    ] = {}
    for resource_index, href in resources:
        segments = grouped.get(href)
        if not segments:
            continue
        if href not in names:
            raise ValueError(f"EPUB body resource not found: {href}")
        source_data = zin.read(href)
        html = UnicodeDammit(source_data).unicode_markup
        if html is None:
            html = source_data.decode("utf-8", errors="replace")
        _title, annotated_segments, template = annotate_epub_resource(
            html,
            resource_index,
            href,
            book_title=book_title,
            skip_navigation=href in toc_paths,
        )

        # Never silently omit backfill when source and state disagree; the source may have been replaced.
        available_anchors = {segment.anchor for segment in annotated_segments if segment.anchor}
        required_anchors = {
            segment.anchor for segment in segments if segment.anchor and not segment.cont
        }
        missing = sorted(required_anchors - available_anchors)
        if missing:
            preview = ", ".join(missing[:3])
            raise ValueError(
                f"EPUB body does not match translation state: {href} lacks backfill anchors {preview}"
            )

        fresh_by_anchor = {
            segment.anchor: segment for segment in annotated_segments if segment.anchor
        }
        stored_sources: dict[str, str] = {}
        current_anchor: str | None = None
        for segment in segments:
            if segment.cont and current_anchor is not None:
                stored_sources[current_anchor] += segment.source
            elif segment.anchor:
                current_anchor = segment.anchor
                stored_sources[current_anchor] = segment.source
            else:
                current_anchor = None
        changed_anchors = [
            anchor
            for anchor, source in stored_sources.items()
            if fresh_by_anchor[anchor].source != source
        ]
        if changed_anchors:
            preview = ", ".join(changed_anchors[:3])
            raise ValueError(
                f"EPUB source does not match translation state: {href} content changed ({preview})"
            )
        fresh_meta_by_anchor = {anchor: segment.meta for anchor, segment in fresh_by_anchor.items()}
        prepared[href] = (segments, template, fresh_meta_by_anchor)

    source_ids_by_resource: dict[str, dict[str, str]] = {}
    source_link_targets: dict[tuple[str, str], str] = {}
    if bilingual:
        for href, (segments, template, _fresh_meta) in prepared.items():
            template_soup = BeautifulSoup(template, "html.parser")
            occupied_ids, tn_id_index = _index_soup_ids(template_soup)
            by_anchor, src_by_anchor, kind_by_anchor, _stored_meta = _segment_render_maps(segments)
            source_ids = _build_source_anchor_ids(
                by_anchor,
                src_by_anchor,
                kind_by_anchor,
                tn_id_index,
                occupied_ids,
            )
            source_ids_by_resource[href] = source_ids
            for fragment, segment_anchor in fragment_anchor_map(template).items():
                if not isinstance(segment_anchor, str):
                    continue
                source_id = source_ids.get(segment_anchor)
                if fragment and source_id:
                    source_link_targets[(href, fragment)] = source_id

    rendered: dict[str, str] = {}
    for _resource_index, href in resources:
        resource = prepared.get(href)
        if resource is None:
            continue
        segments, template, fresh_meta_by_anchor = resource
        rendered[href] = _render_segments_html(
            template,
            segments,
            render_meta_by_anchor=fresh_meta_by_anchor,
            bilingual=bilingual,
            order=order,
            preserve_source_style=preserve_source_style,
            source_lang=source_lang,
            resource_href=href,
            source_ids_by_anchor=source_ids_by_resource.get(href),
            source_link_targets=source_link_targets,
        )
    return rendered
