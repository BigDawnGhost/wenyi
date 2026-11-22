"""Coordinate template, generated chapter and HTML-template EPUB export paths."""

from __future__ import annotations

import os
from html import escape
from urllib.parse import quote

from bs4 import BeautifulSoup

from wenyi_core.assemble import epub_native
from wenyi_core.assemble.about import AboutPage
from wenyi_core.assemble.epub_package import EpubChapter, EpubResource, EpubTocEntry, write_epub
from wenyi_core.assemble.html_renderer import (
    _html_open_tag,
    _render_chapter_html,
    _render_paragraph_html,
)
from wenyi_core.assemble.html_resources import (
    _IMAGE_EXTENSION_BY_TYPE,
    _package_html_resources,
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

from .export_view import AssembleStore, ExportViewStore


def _assemble_epub(
    store: ExportViewStore,
    source_path: str,
    out_path: str,
    *,
    bilingual: bool = False,
    order: str = "target_first",
    preserve_source_style: bool = False,
    about: AboutPage | None = None,
    warnings: list[str] | None = None,
) -> str:
    """Copy the EPUB and replace physical body resources, exact TOC titles and target-language
    metadata.

    The native engine handles resource resolution itself, so ``warnings`` stays unused here.
    """
    if os.path.abspath(source_path) == os.path.abspath(out_path):
        # Opening the target for writing would truncate the archive still being read.
        raise ValueError("--out must not point at the source file; choose a different output path")
    m = store.load_manifest()
    target_lang_code = _manifest_target_lang(m)
    raw_source_lang = m.get("source_lang", "")
    source_lang = raw_source_lang if isinstance(raw_source_lang, str) else ""
    raw_meta = m.get("meta")
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    source_title = m.get("title", "") if isinstance(m.get("title"), str) else ""
    state = epub_native.options(
        store.iter_chapters_once(c["index"] for c in m["chapters"]),
        meta,
        book_title=source_title,
        source_lang=source_lang,
        bilingual=bilingual,
        order=order,
        preserve_source_style=preserve_source_style,
        target_lang=_epub_lang(target_lang_code),
        export_title=_export_book_title(source_title, target_lang_code, bilingual=bilingual),
        about=about.render().decode("utf-8") if about is not None else None,
    )
    with publish_target(out_path) as temp_out:
        epub_native.export(source_path, temp_out, state)
    return out_path


def _build_epub_from_chapters(
    store: AssembleStore,
    source_path: str,
    out_path: str,
    *,
    bilingual: bool = False,
    order: str = "target_first",
    preserve_source_style: bool = False,
    about: AboutPage | None = None,
    warnings: list[str] | None = None,
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
                elif warnings is not None:
                    warnings.append(
                        f"chapter {c['index']}: unresolved FB2 image reference: {resource_id}"
                    )

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
        chapters.append(EpubChapter(fname, ch_title, content))
        heading = next(
            (
                segment
                for segment in ch.segments
                if segment.kind == KIND_HEADING and not segment.cont and segment.source.strip()
            ),
            None,
        )
        level = None if heading is None else _heading_level(ch, heading)
        _push_toc_entry(toc, toc_stack, EpubTocEntry(fname, ch_title), level)

    write_epub(
        out_path,
        title=title,
        lang=lang,
        chapters=chapters,
        toc=toc,
        resources=resources,
        about=about,
        styled_bilingual=bilingual and not preserve_source_style,
    )
    return out_path


def _push_toc_entry(
    toc: list[EpubTocEntry],
    toc_stack: list[tuple[int, list[EpubTocEntry]]],
    entry: EpubTocEntry,
    level: int | None,
) -> None:
    """Place one TOC entry at its heading level; None marks an untitled preface."""
    if level is None:
        # Untitled prefaces are navigation entries, not parents of later headings.
        toc.append(entry)
        toc_stack.clear()
        return
    while toc_stack and toc_stack[-1][0] >= level:
        toc_stack.pop()
    siblings = toc_stack[-1][1] if toc_stack else toc
    siblings.append(entry)
    toc_stack.append((level, entry.children))


def _rendered_toc_level(rendered: str) -> int | None:
    """Return a chapter's original heading level from its rendered template."""
    heading = BeautifulSoup(rendered, "html.parser").find(["h1", "h2", "h3", "h4", "h5", "h6"])
    return int(heading.name[1]) if heading is not None else None


def _build_epub_from_html_templates(
    store: AssembleStore,
    source_path: str,
    out_path: str,
    *,
    bilingual: bool = False,
    order: str = "target_first",
    preserve_source_style: bool = False,
    about: AboutPage | None = None,
    warnings: list[str] | None = None,
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
    chapters: list[EpubChapter] = []
    toc: list[EpubTocEntry] = []
    toc_stack: list[tuple[int, list[EpubTocEntry]]] = []
    packaged_assets: dict[str, tuple[str, bytes]] = {}

    for chapter_meta in manifest["chapters"]:
        chapter = store.load_chapter(chapter_meta["index"])
        chapter_title = _ch_title(chapter_meta) or chapter.title
        rendered = _render_chapter_html(
            chapter,
            bilingual=bilingual,
            order=order,
            preserve_source_style=preserve_source_style,
            prefer_native=True,
        )
        chapter_warnings: list[str] = []
        rendered, assets = _package_html_resources(
            rendered,
            source_dir=os.path.dirname(os.path.abspath(source_path)),
            href_prefix="assets",
            warnings=chapter_warnings,
        )
        if warnings is not None:
            warnings.extend(f"chapter {chapter.index}: {item}" for item in chapter_warnings)
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
        chapters.append(EpubChapter(filename, chapter_title, content))
        _push_toc_entry(
            toc, toc_stack, EpubTocEntry(filename, chapter_title), _rendered_toc_level(rendered)
        )

    resources = [
        EpubResource(href, media_type, payload)
        for href, (media_type, payload) in packaged_assets.items()
    ]
    write_epub(
        out_path,
        title=title,
        lang=lang,
        chapters=chapters,
        toc=toc,
        resources=resources,
        about=about,
        styled_bilingual=bilingual and not preserve_source_style,
    )
    return out_path
