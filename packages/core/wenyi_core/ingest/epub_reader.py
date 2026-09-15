"""Coordinate EPUB package reading, physical-resource annotation and logical chapter layout."""

from __future__ import annotations

import os
import zipfile

from wenyi_core.ingest.epub_layout import _build_epub_annotation_contexts, _logical_chapters
from wenyi_core.ingest.epub_package import (
    _decode_markup,
    _find_opf_path,
    _manifest_xhtml_hrefs,
    _parse_opf,
)
from wenyi_core.ingest.epub_toc import parse_toc_entries
from wenyi_core.ingest.models import Document
from wenyi_core.markup.anchors import fragment_anchor_map
from wenyi_core.markup.contracts import INLINE_META_KEY
from wenyi_core.markup.segments import annotate_epub_resource


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
                    "fragment_anchors": fragment_anchor_map(template),
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
