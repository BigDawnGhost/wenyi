"""Coordinate EPUB package reading, physical-resource annotation and logical chapter layout."""

from __future__ import annotations

import os
import zipfile
from dataclasses import dataclass

from wenyi_core.ingest.epub_layout import _build_epub_annotation_contexts, _logical_chapters
from wenyi_core.ingest.epub_package import (
    _decode_markup,
    _find_opf_path,
    _manifest_xhtml_hrefs,
    _parse_opf,
)
from wenyi_core.ingest.epub_toc import parse_toc_entries
from wenyi_core.markup.anchors import fragment_anchor_map
from wenyi_core.markup.contracts import INLINE_META_KEY
from wenyi_core.markup.segments import annotate_epub_resource
from wenyi_core.models.document import Document
from wenyi_core.resource_pool import ResourcePool


@dataclass(frozen=True)
class _ResourceTask:
    index: int
    href: str
    data: bytes
    book_title: str
    skip_navigation: bool
    auxiliary: bool


def _read_resource(task: _ResourceTask) -> dict[str, object]:
    title, segments, template = annotate_epub_resource(
        _decode_markup(task.data),
        task.index,
        task.href,
        book_title=task.book_title,
        skip_navigation=task.skip_navigation,
    )
    resource: dict[str, object] = {
        "index": task.index,
        "href": task.href,
        "title": title,
        "segments": segments,
        "template": template,
    }
    if not task.auxiliary:
        resource["fragment_anchors"] = fragment_anchor_map(template)
    return resource


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

        specs = [(index, href, False) for index, href in enumerate(hrefs) if href in names]
        spine_count = len(specs)
        # Annotation bodies may appear in the manifest without a spine reference. They do not become
        # formal chapters or backfill resources, but can supply immutable context to referring paragraphs.
        spine_hrefs = {href for _index, href, _auxiliary in specs}
        for auxiliary_ordinal, href in enumerate(manifest_xhtml_hrefs):
            if href in spine_hrefs or href not in names or href in toc_paths:
                continue
            specs.append((len(hrefs) + auxiliary_ordinal, href, True))
        source_bytes = sum(zf.getinfo(href).file_size for _index, href, _auxiliary in specs)
        with ResourcePool(len(specs), source_bytes) as pool:
            tasks = (
                _ResourceTask(index, href, zf.read(href), book_title, href in toc_paths, auxiliary)
                for index, href, auxiliary in specs
            )
            annotated = list(pool.map(_read_resource, tasks))
            resources, auxiliary_resources = annotated[:spine_count], annotated[spine_count:]
            annotation_contexts = _build_epub_annotation_contexts(
                resources,
                [*resources, *auxiliary_resources],
                pool=pool,
            )
        chapters, split_strategy, split_toc_path = _logical_chapters(resources, toc_entries)
        # Rebuild XHTML templates and inline layout deterministically from the original EPUB, not state.
        # Preserve other format metadata and fields added by later stages unchanged.
        for chapter in chapters:
            chapter.template = None
            for segment in chapter.segments:
                segment.meta.pop(INLINE_META_KEY, None)

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
