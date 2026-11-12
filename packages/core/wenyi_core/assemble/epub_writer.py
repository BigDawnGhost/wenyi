"""Coordinate template, generated chapter and HTML-template EPUB export paths."""

from __future__ import annotations

import os
import zipfile
from html import escape
from urllib.parse import quote

from bs4 import BeautifulSoup

from wenyi_core.assemble.epub_navigation import _is_nav, _rewrite_opf_metadata, _rewrite_toc
from wenyi_core.assemble.epub_package import EpubChapter, EpubResource, EpubTocEntry, write_epub
from wenyi_core.assemble.epub_presentation import (
    _HTML_EXTS,
    EpubPresentation,
    _epub_looks_vertical,
    _rewrite_html_document,
)
from wenyi_core.assemble.epub_resources import _render_epub_resources
from wenyi_core.assemble.html_renderer import (
    _html_open_tag,
    _render_chapter_html,
    _render_paragraph_html,
)
from wenyi_core.assemble.html_resources import (
    _IMAGE_EXTENSION_BY_TYPE,
    _package_html_resources,
    _template_resource_source,
)
from wenyi_core.assemble.writer_common import (
    _ch_title,
    _epub_lang,
    _export_book_title,
    _heading_level,
    _manifest_target_lang,
    _merged_paragraphs,
    _sanitize_filename,
)
from wenyi_core.atomicio import publish_target
from wenyi_core.ingest.fb2_reader import read_fb2_binaries
from wenyi_core.models.document import KIND_HEADING

from .export_view import AssembleStore
from .policy import export_options


def _assemble_epub(
    store: AssembleStore,
    source_path: str,
    out_path: str,
    *,
    bilingual: bool = False,
    order: str = "target_first",
    preserve_source_style: bool = False,
) -> str:
    """Copy the EPUB and replace physical body resources, exact TOC titles and target-language
    metadata.
    """
    if os.path.abspath(source_path) == os.path.abspath(out_path):
        # Opening the target for writing would truncate the archive still being read.
        raise ValueError("--out must not point at the source file; choose a different output path")
    m = store.load_manifest()
    target_lang_code = _manifest_target_lang(m)
    target_lang = _epub_lang(target_lang_code)
    raw_source_lang = m.get("source_lang", "")
    source_lang = raw_source_lang if isinstance(raw_source_lang, str) else ""
    raw_meta = m.get("meta")
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    raw_toc_entries = meta.get("toc_entries", [])
    toc_entries: list[dict[str, object]] = (
        [entry for entry in raw_toc_entries if isinstance(entry, dict)]
        if isinstance(raw_toc_entries, list)
        else []
    )
    raw_toc_paths = meta.get("toc_paths")
    toc_paths: set[str] = set()
    if isinstance(raw_toc_paths, list):
        toc_paths.update(path for path in raw_toc_paths if isinstance(path, str) and path)
    for entry in toc_entries:
        toc_path = entry.get("toc_path")
        if isinstance(toc_path, str) and toc_path:
            toc_paths.add(toc_path)
    ncx_paths = {
        str(entry["toc_path"])
        for entry in toc_entries
        if entry.get("kind") == "ncx" and isinstance(entry.get("toc_path"), str)
    }

    chapters = [store.load_chapter(c["index"]) for c in m["chapters"]]

    source_title = m.get("title", "") if isinstance(m.get("title"), str) else ""
    book_title = _export_book_title(
        source_title,
        target_lang_code,
        bilingual=bilingual,
    )

    # Publish atomically: a failure mid-copy must not truncate or destroy an existing
    # export at the same path. The publish context stays outermost so the source handle
    # is closed before the Windows replacement runs (same constraint as about.py).
    with publish_target(out_path) as temp_out:
        with zipfile.ZipFile(source_path, "r") as zin:
            force_horizontal = _epub_looks_vertical(zin)
            rendered = _render_epub_resources(
                zin,
                chapters,
                meta,
                # Use the original book title for annotation so export suffixes cannot be mistaken for body titles.
                book_title=source_title,
                bilingual=bilingual,
                order=order,
                preserve_source_style=preserve_source_style,
                source_lang=source_lang,
                preserve_source_ruby=export_options(store, "epub").preserve_source_ruby,
                presentation=EpubPresentation(target_lang, force_horizontal),
            )

            infos = zin.infolist()
            with zipfile.ZipFile(temp_out, "w") as zout:
                for info in infos:
                    name = info.filename
                    low = name.lower()
                    data = zin.read(name)
                    if name == "mimetype":
                        zout.writestr(info, data, zipfile.ZIP_STORED)
                    elif low.endswith(".opf"):
                        zout.writestr(
                            info,
                            _rewrite_opf_metadata(
                                data,
                                book_title=book_title,
                                lang=target_lang,
                                force_horizontal=force_horizontal,
                            ),
                        )
                    elif low.endswith(".ncx") or name in ncx_paths:
                        zout.writestr(
                            info,
                            _rewrite_toc(
                                data,
                                toc_entries,
                                is_ncx=True,
                                toc_path=name,
                            ),
                        )
                    elif low.endswith(_HTML_EXTS):
                        if name in rendered:
                            # Resource workers already applied TOC, language and layout rewrites.
                            zout.writestr(info, rendered[name].encode("utf-8"))
                        else:
                            if name in toc_paths or _is_nav(data):
                                data = _rewrite_toc(
                                    data,
                                    toc_entries,
                                    is_ncx=False,
                                    toc_path=name,
                                )
                            zout.writestr(
                                info,
                                _rewrite_html_document(
                                    data,
                                    lang=target_lang,
                                    force_horizontal=force_horizontal,
                                    bilingual=bilingual and not preserve_source_style,
                                ),
                            )
                    else:
                        zout.writestr(info, data)
    return out_path


def _build_epub_from_chapters(
    store: AssembleStore,
    source_path: str,
    out_path: str,
    *,
    bilingual: bool = False,
    order: str = "target_first",
    preserve_source_style: bool = False,
) -> str:
    """Build a standard EPUB3 from chapters when no original EPUB template exists."""
    m = store.load_manifest()
    target_lang_code = _manifest_target_lang(m)
    title = _export_book_title(
        m.get("title", "translated") if isinstance(m.get("title"), str) else "translated",
        target_lang_code,
        bilingual=bilingual,
    )
    lang = _epub_lang(target_lang_code)

    chapters: list[EpubChapter] = []
    resources: list[EpubResource] = []
    toc: list[EpubTocEntry] = []
    toc_stack: list[tuple[int, list[EpubTocEntry]]] = []
    image_hrefs: dict[str, str] = {}
    raw_meta = m.get("meta")
    manifest_meta = raw_meta if isinstance(raw_meta, dict) else {}
    if m.get("fmt") == "fb2":
        binaries = read_fb2_binaries(source_path)
        cover_id = manifest_meta.get("fb2_cover_image")
        used_hrefs: set[str] = set()
        for index, (resource_id, (content_type, payload)) in enumerate(binaries.items()):
            stem, extension = os.path.splitext(os.path.basename(resource_id))
            safe_stem = _sanitize_filename(stem, f"image-{index}")
            extension = extension.lower() or _IMAGE_EXTENSION_BY_TYPE.get(content_type, ".bin")
            href = f"images/{safe_stem}{extension}"
            suffix = 2
            while href in used_hrefs:
                href = f"images/{safe_stem}-{suffix}{extension}"
                suffix += 1
            used_hrefs.add(href)
            image_hrefs[resource_id] = quote(href, safe="/")
            resources.append(
                EpubResource(
                    href=href,
                    media_type=content_type,
                    content=payload,
                    cover_image=resource_id == cover_id,
                )
            )

    for c in m["chapters"]:
        ch = store.load_chapter(c["index"])
        ch_title = _ch_title(c) or ch.title
        body_parts = []
        images_by_position: dict[int, list[str]] = {}
        raw_images = ch.meta.get("fb2_images")
        if isinstance(raw_images, list):
            for image in raw_images:
                if not isinstance(image, dict):
                    continue
                position = image.get("position")
                resource_id = image.get("id")
                if not isinstance(position, int) or not isinstance(resource_id, str):
                    continue
                href = image_hrefs.get(resource_id)
                if href:
                    images_by_position.setdefault(position, []).append(href)

        paragraphs = _merged_paragraphs(ch)
        for position, (kind, target, source, level) in enumerate(paragraphs):
            body_parts.extend(
                f'<div class="fb2-image"><img src="{escape(href, quote=True)}" alt=""/></div>'
                for href in images_by_position.get(position, [])
            )
            body_parts.extend(
                _render_paragraph_html(
                    kind,
                    target,
                    source,
                    bilingual=bilingual,
                    order=order,
                    preserve_source_style=preserve_source_style,
                    heading_level=min(level, 6),
                )
            )
        body_parts.extend(
            f'<div class="fb2-image"><img src="{escape(href, quote=True)}" alt=""/></div>'
            for href in images_by_position.get(len(paragraphs), [])
        )
        fname = f"ch{c['index']}.xhtml"
        content = (
            f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{lang}">'
            f"<head><title>{escape(ch_title)}</title></head>"
            f"<body>{''.join(body_parts)}</body></html>"
        )
        chapters.append(
            EpubChapter(
                fname,
                ch_title,
                _rewrite_html_document(
                    content,
                    lang=lang,
                    force_horizontal=False,
                    bilingual=bilingual and not preserve_source_style,
                ).decode("utf-8"),
            )
        )
        entry = EpubTocEntry(fname, ch_title)
        heading = next(
            (
                segment
                for segment in ch.segments
                if segment.kind == KIND_HEADING and not segment.cont and segment.source.strip()
            ),
            None,
        )
        if heading is None:
            # Untitled prefaces are navigation entries, not parents of later headings.
            toc.append(entry)
            toc_stack.clear()
            continue
        level = _heading_level(ch, heading)
        while toc_stack and toc_stack[-1][0] >= level:
            toc_stack.pop()
        siblings = toc_stack[-1][1] if toc_stack else toc
        siblings.append(entry)
        toc_stack.append((level, entry.children))

    write_epub(out_path, title=title, lang=lang, chapters=chapters, toc=toc, resources=resources)
    return out_path


def _build_epub_from_html_templates(
    store: AssembleStore,
    source_path: str,
    out_path: str,
    *,
    bilingual: bool = False,
    order: str = "target_first",
    preserve_source_style: bool = False,
) -> str:
    """Build EPUB from rendered HTML templates and package their media resources."""
    manifest = store.load_manifest()
    target_lang_code = _manifest_target_lang(manifest)
    raw_title = manifest.get("title", "translated")
    title = _export_book_title(
        raw_title if isinstance(raw_title, str) else "translated",
        target_lang_code,
        bilingual=bilingual,
    )
    lang = _epub_lang(target_lang_code)
    raw_meta = manifest.get("meta")
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    head_html = meta.get("head_html", "")
    head_html = head_html if isinstance(head_html, str) else ""
    # Carry inline styles, not unbundled external head resources or source scripts.
    head_html = "".join(
        str(style) for style in BeautifulSoup(head_html, "html.parser").find_all("style")
    )
    resource_source = _template_resource_source(store, manifest, source_path)

    chapters: list[EpubChapter] = []
    toc: list[EpubTocEntry] = []
    packaged_assets: dict[str, tuple[str, bytes]] = {}

    for chapter_meta in manifest["chapters"]:
        chapter = store.load_chapter(chapter_meta["index"])
        chapter_title = _ch_title(chapter_meta) or chapter.title
        rendered = _render_chapter_html(
            chapter,
            bilingual=bilingual,
            order=order,
            preserve_source_style=preserve_source_style,
            preserve_source_ruby=export_options(store, "epub").preserve_source_ruby,
        )
        rendered, assets = _package_html_resources(
            rendered,
            source_dir=os.path.dirname(os.path.abspath(resource_source)),
            href_prefix="assets",
        )
        packaged_assets.update(assets)
        filename = f"ch{chapter.index}.xhtml"
        html_open = _html_open_tag(
            "html",
            meta.get("html_attrs"),
            lang=lang,
            **{"xmlns": "http://www.w3.org/1999/xhtml", "xml:lang": lang},
        )
        body_open = _html_open_tag("body", meta.get("body_attrs"))
        content = (
            html_open + f"<head><title>{escape(chapter_title)}</title>{head_html}</head>"
            f"{body_open}{rendered}</body></html>"
        )
        chapters.append(
            EpubChapter(
                filename,
                chapter_title,
                _rewrite_html_document(
                    content,
                    lang=lang,
                    force_horizontal=False,
                    bilingual=bilingual and not preserve_source_style,
                ).decode("utf-8"),
            )
        )
        toc.append(EpubTocEntry(filename, chapter_title))

    resources = [
        EpubResource(href, media_type, payload)
        for href, (media_type, payload) in packaged_assets.items()
    ]
    write_epub(out_path, title=title, lang=lang, chapters=chapters, toc=toc, resources=resources)
    return out_path
