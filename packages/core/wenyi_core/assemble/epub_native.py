"""Project export state onto the Rust EPUB engine's input contract."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Mapping
from os import PathLike

from wenyi_core.assemble.html_bilingual import _BILINGUAL_CSS
from wenyi_core.assemble.writer_common import _seg_text
from wenyi_core.markup import _epub_native
from wenyi_core.markup.contracts import ANNOTATION_META_KEY
from wenyi_core.models.document import KIND_HEADING, Chapter


def resource_specs(meta: dict[str, object]) -> list[tuple[int, str]]:
    resources = meta.get("epub_resources")
    if not isinstance(resources, list):
        return []
    specs: list[tuple[int, str]] = []
    seen: set[str] = set()
    for fallback, record in enumerate(resources):
        if not isinstance(record, dict):
            continue
        href = record.get("href")
        if not isinstance(href, str) or not href or href in seen:
            continue
        seen.add(href)
        index = record.get("index")
        specs.append((index if type(index) is int and index >= 0 else fallback, href))
    return specs


def _translated_list(meta: Mapping[str, object], key: str) -> list[str]:
    """Positional translations; blank slots keep the matching source value."""
    raw = meta.get(key)
    if not isinstance(raw, list):
        return []
    return [item.strip() if isinstance(item, str) else "" for item in raw]


def _nav_titles(
    meta: Mapping[str, object],
    book_title: str,
    export_title: str,
    original_title: str | None,
) -> dict[str, str]:
    """Map each source navigation-document label to its saved translation.

    A label that repeats the book title uses the translated book title, so the navigation
    page cannot name the book differently from the package metadata.
    """
    sources = meta.get("epub_nav_titles")
    if not isinstance(sources, list):
        return {}
    translated = _translated_list(meta, "epub_nav_titles_translated")
    titles = (
        {
            source: target
            for source, target in zip(sources, translated)
            if isinstance(source, str) and source and target
        }
        if len(translated) == len(sources)
        else {}
    )
    book = book_title.strip()
    if original_title and book and book in sources and export_title.strip():
        titles[book] = export_title.strip()
    return titles


def options(
    chapters: Iterable[Chapter],
    meta: dict[str, object],
    *,
    book_title: str,
    source_lang: str,
    bilingual: bool,
    order: str,
    preserve_source_style: bool,
    target_lang: str | None = None,
    export_title: str = "",
    original_title: str | None = None,
    description: str | None = None,
    subjects: list[str] | None = None,
    chapter_titles: Mapping[int, str] | None = None,
    horizontal: bool = False,
    about: str | None = None,
) -> bytes:
    specs = resource_specs(meta)
    raw_paths = meta.get("toc_paths")
    raw_entries = meta.get("toc_entries")
    package_path = meta.get("opf_path")
    entries = []
    resource_titles: dict[str, str] = {}
    for record in raw_entries if isinstance(raw_entries, list) else []:
        if not isinstance(record, dict):
            continue
        href, index = record.get("toc_path"), record.get("node_index")
        if not isinstance(href, str) or type(index) is not int or index < 0:
            continue
        row = {"toc_path": href, "node_index": index}
        for key in ("title", "title_translated", "raw_href", "kind"):
            if isinstance(value := record.get(key), str):
                row[key] = value
        entries.append(row)
        resource, title = record.get("resource_href"), row.get("title_translated")
        if (
            target_lang is not None
            and isinstance(resource, str)
            and resource
            and record.get("fragment") == ""
            and not record.get("external")
            and isinstance(title, str)
            and (title := title.strip())
        ):
            resource_titles.setdefault(resource, title)
    chapter_resource_titles: dict[str, str] = {}
    merged = {}
    for chapter in chapters:
        chapter_hrefs: set[str] = set()
        for segment in chapter.segments:
            href = segment.resource_href
            if not href:
                continue
            if chapter_titles is not None:
                chapter_hrefs.add(href)
            current = merged.setdefault(href, {})
            if segment.cont and current:
                previous = next(reversed(current.values()))
                previous["source"] += segment.source
                previous["target"] += _seg_text(segment)
            elif segment.anchor:
                current[segment.anchor] = {
                    "resource_href": href,
                    "anchor": segment.anchor,
                    "source": segment.source,
                    "target": _seg_text(segment),
                    "heading": segment.kind == KIND_HEADING,
                    "annotations": segment.meta.get(ANNOTATION_META_KEY),
                }
            elif segment.cont:
                # The anchor slice is missing, so no template block can absorb this
                # continuation. Keep the template's original block (footnote markup
                # included) instead of failing the whole export.
                logging.getLogger(__name__).warning(
                    "Omitting orphaned continuation segment %s on %s: no anchor slice",
                    segment.index,
                    href,
                )
            else:
                raise ValueError(
                    f"EPUB state is inconsistent: segment {segment.index} has no anchor"
                )
        if chapter_titles is not None:
            # Only use a chapter title when the chapter and page are one-to-one.
            for href in chapter_hrefs:
                chapter_resource_titles[href] = (
                    chapter_titles.get(chapter.index, "").strip()
                    if len(chapter_hrefs) == 1 and href not in chapter_resource_titles
                    else ""
                )
    for href, title in chapter_resource_titles.items():
        if title:
            resource_titles.setdefault(href, title)
    return json.dumps(
        {
            "title": book_title,
            "source_lang": source_lang,
            "bilingual": bilingual,
            "source_first": order == "source_first",
            "preserve_source_style": preserve_source_style,
            "resources": [{"index": index, "href": href} for index, href in specs],
            "toc_paths": [s for s in raw_paths if isinstance(s, str)]
            if isinstance(raw_paths, list)
            else [],
            "toc_entries": entries,
            "segments": [record for page in merged.values() for record in page.values()],
            "about": about,
            "package_path": package_path if isinstance(package_path, str) else None,
            "resource_titles": resource_titles,
            "presentation": (
                {
                    "lang": target_lang,
                    "title": export_title,
                    "css": _BILINGUAL_CSS,
                    "horizontal": horizontal,
                    "original_title": original_title,
                    "description": description,
                    "subjects": subjects or [],
                    "creators": _translated_list(meta, "epub_creators_translated"),
                    "nav_titles": _nav_titles(meta, book_title, export_title, original_title),
                }
                if target_lang is not None
                else None
            ),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _warn_omitted(ids: list[str]) -> None:
    for object_id in ids:
        logging.getLogger(__name__).warning(
            "Omitting protected object %s: pending or stale alignment; inspect this paragraph",
            object_id,
        )


def render_resources(source: str | PathLike[str], state: bytes) -> dict[str, str]:
    """Return physical XHTML for adapters producing non-EPUB output."""
    pages, missing = _epub_native.render_resources(source, state)
    _warn_omitted(missing)
    return dict(pages)


def render_html_bodies(
    source: str | PathLike[str], state: bytes, mime_types: dict[str, str]
) -> list[str]:
    """Read, backfill and embed EPUB media in spine order in the native engine."""
    bodies, missing = _epub_native.render_html_bodies(source, state, mime_types)
    _warn_omitted(missing)
    return bodies


def export(source: str, temporary: str | PathLike[str], state: bytes) -> None:
    """Write an EPUB to a caller-owned temporary path."""
    _warn_omitted(_epub_native.export(source, temporary, state))
