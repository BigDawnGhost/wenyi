"""Rebuild authoritative EPUB resources and backfill each physical resource once."""

from __future__ import annotations

import zipfile
from dataclasses import dataclass

from bs4 import BeautifulSoup

from wenyi_core.assemble.epub_navigation import _is_nav, _rewrite_toc
from wenyi_core.assemble.epub_presentation import EpubPresentation, _rewrite_html_document
from wenyi_core.assemble.html_bilingual import (
    _build_source_anchor_ids,
    _index_soup_ids,
)
from wenyi_core.assemble.html_renderer import (
    _render_segments_html,
    _segment_render_maps,
)
from wenyi_core.ingest.epub_package import _decode_markup
from wenyi_core.markup.anchors import fragment_anchor_map, resolve_epub_href
from wenyi_core.markup.segments import annotate_epub_resource
from wenyi_core.models.document import Chapter, Segment
from wenyi_core.resource_pool import ResourcePool


@dataclass(frozen=True)
class _RenderOptions:
    book_title: str
    bilingual: bool
    order: str
    preserve_source_style: bool
    source_lang: str
    preserve_source_ruby: bool | None
    toc_paths: frozenset[str]
    toc_entries: list[dict[str, object]]
    presentation: EpubPresentation | None


@dataclass(frozen=True)
class _PrepareTask:
    index: int
    href: str
    data: bytes
    segments: list[Segment]
    options: _RenderOptions


@dataclass(frozen=True)
class _PreparedResource:
    href: str
    segments: list[Segment]
    template: str
    metadata: dict[str, dict[str, object]]
    source_ids: dict[str, str]
    source_links: dict[tuple[str, str], str]
    source_targets: set[tuple[str, str]]


@dataclass(frozen=True)
class _RenderTask:
    resource: _PreparedResource
    options: _RenderOptions
    source_link_targets: dict[tuple[str, str], str]


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


def _prepare_resource(task: _PrepareTask) -> _PreparedResource:
    options = task.options
    _title, annotated_segments, template = annotate_epub_resource(
        _decode_markup(task.data),
        task.index,
        task.href,
        book_title=options.book_title,
        skip_navigation=task.href in options.toc_paths,
    )

    # Never silently omit backfill when source and state disagree.
    available_anchors = {segment.anchor for segment in annotated_segments if segment.anchor}
    required_anchors = {
        segment.anchor for segment in task.segments if segment.anchor and not segment.cont
    }
    missing = sorted(required_anchors - available_anchors)
    if missing:
        preview = ", ".join(missing[:3])
        raise ValueError(
            f"EPUB body does not match translation state: {task.href} lacks backfill anchors {preview}"
        )

    fresh_by_anchor = {segment.anchor: segment for segment in annotated_segments if segment.anchor}
    stored_sources: dict[str, str] = {}
    current_anchor: str | None = None
    for segment in task.segments:
        if segment.cont and current_anchor is not None:
            stored_sources[current_anchor] += segment.source
        elif segment.anchor:
            current_anchor = segment.anchor
            stored_sources[current_anchor] = segment.source
        else:
            raise ValueError(f"EPUB state is inconsistent: segment {segment.index} has no anchor")
    changed_anchors = [
        anchor
        for anchor, source in stored_sources.items()
        if fresh_by_anchor[anchor].source != source
    ]
    if changed_anchors:
        preview = ", ".join(changed_anchors[:3])
        raise ValueError(
            f"EPUB source does not match translation state: {task.href} content changed ({preview})"
        )
    metadata = {anchor: segment.meta for anchor, segment in fresh_by_anchor.items()}

    source_ids: dict[str, str] = {}
    source_links: dict[tuple[str, str], str] = {}
    source_targets: set[tuple[str, str]] = set()
    if options.bilingual:
        soup = BeautifulSoup(template, "html.parser")
        occupied_ids, tn_id_index = _index_soup_ids(soup)
        by_anchor, src_by_anchor, kind_by_anchor, _stored_meta = _segment_render_maps(task.segments)
        source_ids = _build_source_anchor_ids(
            by_anchor, src_by_anchor, kind_by_anchor, tn_id_index, occupied_ids
        )
        for fragment, segment_anchor in fragment_anchor_map(template).items():
            source_id = source_ids.get(segment_anchor) if isinstance(segment_anchor, str) else None
            if fragment and source_id:
                source_links[(task.href, fragment)] = source_id
        for link in soup.find_all("a", href=True):
            raw_href = link.get("href")
            if not isinstance(raw_href, str):
                continue
            resolved = resolve_epub_href(task.href, raw_href)
            if not resolved.external and resolved.fragment:
                source_targets.add((resolved.resource_href, resolved.fragment))
    return _PreparedResource(
        task.href, task.segments, template, metadata, source_ids, source_links, source_targets
    )


def _render_resource(task: _RenderTask) -> tuple[str, str]:
    resource, options = task.resource, task.options
    html = _render_segments_html(
        resource.template,
        resource.segments,
        render_meta_by_anchor=resource.metadata,
        bilingual=options.bilingual,
        order=options.order,
        preserve_source_style=options.preserve_source_style,
        source_lang=options.source_lang,
        preserve_source_ruby=options.preserve_source_ruby,
        resource_href=resource.href,
        source_ids_by_anchor=resource.source_ids,
        source_link_targets=task.source_link_targets,
    )
    if options.presentation is not None:
        data = html.encode("utf-8")
        if (
            resource.href in options.toc_paths
            or any(entry.get("toc_path") == resource.href for entry in options.toc_entries)
            or _is_nav(data)
        ):
            data = _rewrite_toc(data, options.toc_entries, is_ncx=False, toc_path=resource.href)
        html = _rewrite_html_document(
            data,
            lang=options.presentation.lang,
            force_horizontal=options.presentation.force_horizontal,
            bilingual=options.bilingual and not options.preserve_source_style,
        ).decode("utf-8")
    return resource.href, html


def _prepare_and_render_resource(task: _PrepareTask) -> tuple[str, str]:
    return _render_resource(_RenderTask(_prepare_resource(task), task.options, {}))


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
    preserve_source_ruby: bool | None = None,
    presentation: EpubPresentation | None = None,
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

    names = set(zin.namelist())
    raw_toc_paths = meta.get("toc_paths")
    toc_paths = (
        {path for path in raw_toc_paths if isinstance(path, str)}
        if isinstance(raw_toc_paths, list)
        else set()
    )
    active_resources = [(index, href) for index, href in resources if grouped.get(href)]
    for _index, href in active_resources:
        if href not in names:
            raise ValueError(f"EPUB body resource not found: {href}")
    raw_toc_entries = meta.get("toc_entries")
    toc_entries = (
        [entry for entry in raw_toc_entries if isinstance(entry, dict)]
        if isinstance(raw_toc_entries, list)
        else []
    )
    options = _RenderOptions(
        book_title,
        bilingual,
        order,
        preserve_source_style,
        source_lang,
        preserve_source_ruby,
        frozenset(toc_paths),
        toc_entries,
        presentation,
    )
    source_bytes = sum(zin.getinfo(href).file_size for _index, href in active_resources)
    with ResourcePool(len(active_resources), source_bytes) as pool:
        tasks = (
            _PrepareTask(index, href, zin.read(href), grouped[href], options)
            for index, href in active_resources
        )
        if not bilingual:
            return dict(pool.map(_prepare_and_render_resource, tasks))

        prepared = list(pool.map(_prepare_resource, tasks))
        source_link_targets = {
            key: target for resource in prepared for key, target in resource.source_links.items()
        }
        # Resolve the book-wide barrier before rendering. Send each task only the outgoing
        # links it uses, not a serialized copy of the entire book's map.
        render_tasks = (
            _RenderTask(
                resource,
                options,
                {
                    key: source_link_targets[key]
                    for key in sorted(resource.source_targets)
                    if key in source_link_targets
                },
            )
            for resource in prepared
        )
        return dict(pool.map(_render_resource, render_tasks))
