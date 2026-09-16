"""Coordinate template, generated chapter and HTML-template EPUB export paths."""

from __future__ import annotations

import os
import zipfile
from html import escape

from wenyi_core.assemble.epub_navigation import _is_nav, _rewrite_opf_metadata, _rewrite_toc
from wenyi_core.assemble.epub_presentation import (
    _HTML_EXTS,
    _epub_looks_vertical,
    _inject_bilingual_style,
    _rewrite_html_document,
)
from wenyi_core.assemble.epub_resources import _render_epub_resources
from wenyi_core.assemble.html_renderer import (
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
    _manifest_target_lang,
    _merged_paragraphs,
    _sanitize_filename,
)
from wenyi_core.ingest.fb2_reader import read_fb2_binaries

from .export_view import AssembleStore


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
        )

        infos = zin.infolist()
        with zipfile.ZipFile(out_path, "w") as zout:
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
                    html_data = rendered[name].encode("utf-8") if name in rendered else data
                    if name in toc_paths or _is_nav(html_data):
                        html_data = _rewrite_toc(
                            html_data,
                            toc_entries,
                            is_ncx=False,
                            toc_path=name,
                        )
                    zout.writestr(
                        info,
                        _rewrite_html_document(
                            html_data,
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
    from ebooklib import epub

    m = store.load_manifest()
    target_lang_code = _manifest_target_lang(m)
    title = _export_book_title(
        m.get("title", "translated") if isinstance(m.get("title"), str) else "translated",
        target_lang_code,
        bilingual=bilingual,
    )
    lang = _epub_lang(target_lang_code)

    book = epub.EpubBook()
    book.set_identifier(f"trans-novel-{title}")
    book.set_title(title)
    book.set_language(lang)

    spine: list = ["nav"]
    toc: list = []
    chapter_filenames: set[str] = set()
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
            image_hrefs[resource_id] = href
            if resource_id == cover_id:
                book.set_cover(href, payload, create_page=True)
            else:
                book.add_item(
                    epub.EpubItem(
                        uid=f"fb2-image-{index}",
                        file_name=href,
                        media_type=content_type,
                        content=payload,
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
        for position, (kind, target, source) in enumerate(paragraphs):
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
                )
            )
        body_parts.extend(
            f'<div class="fb2-image"><img src="{escape(href, quote=True)}" alt=""/></div>'
            for href in images_by_position.get(len(paragraphs), [])
        )
        fname = f"ch{c['index']}.xhtml"
        chapter_filenames.add(fname)
        item = epub.EpubHtml(title=ch_title, file_name=fname, lang=lang)
        item.content = (
            f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{lang}">'
            f"<head><title>{escape(ch_title)}</title></head>"
            f"<body>{''.join(body_parts)}</body></html>"
        )
        book.add_item(item)
        spine.append(item)
        toc.append(item)

    book.toc = toc
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = spine
    epub.write_epub(out_path, book)
    if bilingual and not preserve_source_style:
        _inject_bilingual_style(out_path, chapter_filenames, lang)
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
    from ebooklib import epub

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
    resource_source = _template_resource_source(store, manifest, source_path)

    book = epub.EpubBook()
    book.set_identifier(f"trans-novel-{title}")
    book.set_title(title)
    book.set_language(lang)
    spine: list = ["nav"]
    toc: list = []
    packaged_assets: dict[str, tuple[str, bytes]] = {}

    for chapter_meta in manifest["chapters"]:
        chapter = store.load_chapter(chapter_meta["index"])
        chapter_title = _ch_title(chapter_meta) or chapter.title
        rendered = _render_chapter_html(
            chapter,
            bilingual=bilingual,
            order=order,
            preserve_source_style=preserve_source_style,
        )
        rendered, assets = _package_html_resources(
            rendered,
            source_dir=os.path.dirname(os.path.abspath(resource_source)),
            href_prefix="assets",
        )
        packaged_assets.update(assets)
        filename = f"ch{chapter.index}.xhtml"
        item = epub.EpubHtml(title=chapter_title, file_name=filename, lang=lang)
        item.content = (
            f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{lang}">'
            f"<head><title>{escape(chapter_title)}</title>{head_html}</head>"
            f"<body>{rendered}</body></html>"
        )
        book.add_item(item)
        spine.append(item)
        toc.append(item)

    for index, (href, (media_type, payload)) in enumerate(packaged_assets.items()):
        book.add_item(
            epub.EpubItem(
                uid=f"html-resource-{index}",
                file_name=href,
                media_type=media_type,
                content=payload,
            )
        )
    book.toc = toc
    book.spine = spine
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    epub.write_epub(out_path, book)
    return out_path
