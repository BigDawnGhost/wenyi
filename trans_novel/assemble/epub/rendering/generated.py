"""Generate EPUB output for text and FB2 sources."""

from __future__ import annotations

import os
import zipfile
from html import escape

from lxml import etree

from trans_novel.assemble.epub.metadata import epub_language
from trans_novel.assemble.epub.rendering.bilingual import (
    BILINGUAL_CSS,
    BILINGUAL_SOURCE_CLASS,
    BILINGUAL_STYLE_ID,
)
from trans_novel.assemble.text import bilingual_source, merged_paragraphs
from trans_novel.ingest import KIND_HEADING
from trans_novel.ingest.fb2 import read_fb2_binaries

_HTML_EXTS = (".xhtml", ".html", ".htm")
_IMAGE_EXTENSION_BY_TYPE = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
}


def _inject_generated_bilingual_style(
    out_path: str, chapter_filenames: dict[str, str], chapters, bilingual: bool
) -> None:
    if not bilingual or all(chapter.preserve_source for _, chapter in chapters):
        return
    """Add bilingual style to generated chapter documents."""
    with zipfile.ZipFile(out_path, "r") as zin:
        infos = zin.infolist()
        entries = {info.filename: zin.read(info.filename) for info in infos}
    tmp_path = out_path + ".tmp"
    try:
        with zipfile.ZipFile(tmp_path, "w") as zout:
            for info in infos:
                data = entries[info.filename]
                if os.path.basename(
                    info.filename
                ) in chapter_filenames and info.filename.lower().endswith(_HTML_EXTS):
                    try:
                        tree = etree.fromstring(
                            data,
                            etree.XMLParser(
                                no_network=True,
                                recover=False,
                                resolve_entities=False,
                                remove_comments=False,
                                remove_pis=False,
                            ),
                        ).getroottree()
                        root = tree.getroot()
                        namespace = root.nsmap.get(None)

                        def qualified(name: str, namespace: str | None = namespace) -> str:
                            return f"{{{namespace}}}{name}" if namespace else name

                        head = next(
                            (
                                child
                                for child in root
                                if isinstance(child.tag, str)
                                and child.tag.rsplit("}", 1)[-1].lower() == "head"
                            ),
                            None,
                        )
                        if head is None:
                            head = etree.Element(qualified("head"))
                            root.insert(0, head)
                        if not any(
                            isinstance(style.tag, str)
                            and style.tag.rsplit("}", 1)[-1].lower() == "style"
                            and style.get("id") == BILINGUAL_STYLE_ID
                            for style in head
                        ):
                            style = etree.Element(qualified("style"), id=BILINGUAL_STYLE_ID)
                            style.text = BILINGUAL_CSS
                            head.append(style)
                        data = etree.tostring(tree, encoding="UTF-8", xml_declaration=True)
                    except (etree.XMLSyntaxError, ValueError):
                        pass
                zout.writestr(info, data)
        os.replace(tmp_path, out_path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def build_epub_from_chapters(
    store,
    source_path: str,
    out_path: str,
    *,
    bilingual: bool = False,
    order: str = "target_first",
) -> str:
    """Generate EPUB 3 from translated chapters and restore FB2 images."""
    from ebooklib import epub

    manifest = store.load_manifest()
    title = manifest.get("title", "translated")
    chapters, target_lang, source_lang = (
        [
            (chapter_meta, store.load_chapter(chapter_meta["index"]))
            for chapter_meta in manifest["chapters"]
        ],
        epub_language(manifest.get("target_lang", "zh")),
        str(manifest.get("source_lang") or ""),
    )
    all_preserved = chapters and all(chapter.preserve_source for _, chapter in chapters)
    lang = source_lang if all_preserved else target_lang
    book = epub.EpubBook()
    book.set_identifier(f"trans-novel-{title}")
    book.set_title(title)
    book.set_language(lang)
    spine: list = ["nav"]
    toc: list = []
    chapter_filenames: dict[str, str] = {}
    image_hrefs: dict[str, str] = {}
    raw_meta = manifest.get("meta")
    manifest_meta = raw_meta if isinstance(raw_meta, dict) else {}
    if manifest.get("fmt") == "fb2":
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
    for chapter_meta, chapter in chapters:
        chapter_lang = source_lang if chapter.preserve_source and source_lang else target_lang
        chapter_title = (
            chapter.title if chapter.preserve_source else _ch_title(chapter_meta) or chapter.title
        )
        body_parts: list[str] = []
        images_by_position: dict[int, list[str]] = {}
        raw_images = chapter.meta.get("fb2_images")
        if isinstance(raw_images, list):
            for image in raw_images:
                if not isinstance(image, dict):
                    continue
                position, resource_id = image.get("position"), image.get("id")
                href = (
                    image_hrefs.get(resource_id)
                    if isinstance(position, int) and isinstance(resource_id, str)
                    else None
                )
                if href:
                    images_by_position.setdefault(position, []).append(href)
        paragraphs = merged_paragraphs(chapter)
        for position, (kind, target, source) in enumerate(paragraphs):
            body_parts.extend(
                f'<div class="fb2-image"><img src="{escape(href, quote=True)}" alt=""/></div>'
                for href in images_by_position.get(position, [])
            )
            tag = "h1" if kind == KIND_HEADING else "p"
            target_html = f"<{tag}>{escape(target)}</{tag}>"
            src = (
                bilingual_source(source, target)
                if bilingual and kind != KIND_HEADING and not chapter.preserve_source
                else ""
            )
            if not src:
                body_parts.append(target_html)
            else:
                src_html = f'<p class="{BILINGUAL_SOURCE_CLASS}">{escape(src)}</p>'
                body_parts.extend(
                    (src_html, target_html) if order == "source_first" else (target_html, src_html)
                )
        body_parts.extend(
            f'<div class="fb2-image"><img src="{escape(href, quote=True)}" alt=""/></div>'
            for href in images_by_position.get(len(paragraphs), [])
        )
        filename = f"ch{chapter_meta['index']}.xhtml"
        chapter_filenames[filename] = chapter_lang
        item = epub.EpubHtml(title=chapter_title, file_name=filename, lang=chapter_lang)
        item.content = f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{chapter_lang}"><head><title>{escape(chapter_title)}</title></head><body>{"".join(body_parts)}</body></html>'
        book.add_item(item)
        spine.append(item)
        toc.append(item)
    book.toc = toc
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = spine
    epub.write_epub(out_path, book)
    _inject_generated_bilingual_style(out_path, chapter_filenames, chapters, bilingual)
    from trans_novel.assemble.epub.rendering.generated_finalize import annotate_generated_navigation

    annotate_generated_navigation(out_path, chapter_filenames, lang)
    return out_path


def _sanitize_filename(name: str, fallback: str = "translated") -> str:
    import re

    value = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", name or "").strip().strip(".")
    return re.sub(r"\s+", " ", value)[:120] or fallback


def _ch_title(chapter: dict) -> str:
    from trans_novel.postprocess.punct import normalize_heading_numbering

    return normalize_heading_numbering(
        (chapter.get("title_translated") or chapter.get("title") or "").strip()
    )
