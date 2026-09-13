"""Generate EPUB output for text and FB2 sources."""

from __future__ import annotations

import os
import zipfile
from html import escape

from lxml import etree

from trans_novel.assemble.epub.metadata import epub_language
from trans_novel.assemble.epub.rendering.bilingual import (
    BILINGUAL_SOURCE_CLASS,
)
from trans_novel.assemble.epub.rendering.source_dom import element_children_lxml
from trans_novel.assemble.epub.rendering.theme import (
    LayoutBinding,
    ResourceThemeScope,
    SourcePair,
    ThemeError,
    ThemePlan,
)
from trans_novel.assemble.epub.rendering.theme.service import ThemeService
from trans_novel.assemble.text import bilingual_source, merged_paragraphs
from trans_novel.epub.layout import source_node_digest
from trans_novel.epub.package import HTML_MEDIA, read_package
from trans_novel.ingest import KIND_HEADING
from trans_novel.ingest.fb2 import read_fb2_binaries

_IMAGE_EXTENSION_BY_TYPE = {
    "image/gif": ".gif",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
}


def _tag(node: etree._Element) -> tuple[str | None, str]:
    qualified = etree.QName(node)
    return qualified.namespace, qualified.localname.lower()


def _topology(node: etree._Element) -> tuple[tuple[str | None, str], tuple[object, ...]]:
    return _tag(node), tuple(_topology(child) for child in element_children_lxml(node))


def _body(root: etree._Element) -> tuple[int, etree._Element] | None:
    children = element_children_lxml(root)
    matches = [(index, child) for index, child in enumerate(children) if _tag(child)[1] == "body"]
    return matches[0] if len(matches) == 1 else None


def _generated_theme_scopes(
    out_path: str,
    chapters: dict[
        str,
        tuple[
            str, int, tuple[object, ...], tuple[SourcePair, ...], tuple[LayoutBinding, ...], bool
        ],
    ],
    protected_ids: set[str],
) -> dict[str, ResourceThemeScope]:
    failures: list[dict[str, str]] = []
    with zipfile.ZipFile(out_path) as archive:
        package = read_package(archive, failures)
        model = package.get("model")
        if failures or not package.get("valid") or not isinstance(model, dict):
            raise ThemeError("theme_css", "invalid_scope")
        resolved = model.get("resolved", [])
        scopes: dict[str, ResourceThemeScope] = {}
        for uid, (name, body_index, topology, pairs, bindings, preserved) in chapters.items():
            matches = [
                item
                for item in resolved
                if item.get("id") == uid
                and item.get("href") == name
                and item.get("media") in HTML_MEDIA
                and isinstance(item.get("path"), str)
            ]
            resource = str(matches[0]["path"]) if len(matches) == 1 else None
            if resource is None:
                raise ThemeError("theme_css", "invalid_scope")
            try:
                root = etree.fromstring(
                    archive.read(resource),
                    etree.XMLParser(
                        no_network=True,
                        recover=False,
                        resolve_entities=False,
                        remove_comments=False,
                        remove_pis=False,
                    ),
                )
            except (KeyError, etree.LxmlError, ValueError):
                raise ThemeError("theme_css", "invalid_scope", resource=resource) from None
            final_body = _body(root)
            if (
                _tag(root) != ("http://www.w3.org/1999/xhtml", "html")
                or final_body is None
                or final_body[0] != body_index
                or _topology(final_body[1]) != topology
            ):
                raise ThemeError("theme_css", "invalid_scope", resource=resource)
            scopes[resource] = ResourceThemeScope(
                source_pairs=pairs, layout_bindings=bindings, preserve_resource=preserved
            )
        for item in resolved:
            if item.get("media") in HTML_MEDIA and isinstance(item.get("path"), str):
                scopes.setdefault(
                    str(item["path"]),
                    ResourceThemeScope(preserve_resource=item.get("id") in protected_ids),
                )
    return scopes


def _add_fb2_images(book, manifest: dict, source_path: str) -> dict[str, str]:
    from ebooklib import epub

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
    return image_hrefs


def _theme_chapter(
    item,
    source_pairs: list[tuple[int, int]],
    layout_bindings: list[tuple[int, int, str]],
    preserved: bool,
):
    constructed_root = etree.fromstring(item.content.encode())
    constructed_body = _body(constructed_root)
    if constructed_body is None:
        raise ThemeError("theme_css", "invalid_scope")
    body_index = constructed_body[0]
    return (
        item.get_name(),
        body_index,
        _topology(constructed_body[1]),
        tuple(
            SourcePair((body_index, source_position), ((body_index, target_position),))
            for source_position, target_position in source_pairs
        ),
        tuple(
            LayoutBinding(
                source_path=(source_position,),
                target_paths=((body_index, target_position),),
                source_sha256=source_sha256,
            )
            for source_position, target_position, source_sha256 in layout_bindings
        ),
        preserved,
    )


def build_epub_from_chapters(
    store,
    source_path: str,
    out_path: str,
    *,
    bilingual: bool = False,
    order: str = "target_first",
    theme: ThemeService | None = None,
) -> ThemePlan | None:
    """从译文章节生成 EPUB 3，并按需应用主题。"""
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
    theme_chapters: dict[
        str,
        tuple[
            str, int, tuple[object, ...], tuple[SourcePair, ...], tuple[LayoutBinding, ...], bool
        ],
    ] = {}
    image_hrefs = _add_fb2_images(book, manifest, source_path)
    for chapter_meta, chapter in chapters:
        chapter_lang = source_lang if chapter.preserve_source and source_lang else target_lang
        chapter_title = (
            chapter.title if chapter.preserve_source else _ch_title(chapter_meta) or chapter.title
        )
        body_parts: list[str] = []
        source_pairs: list[tuple[int, int]] = []
        images_by_position, layout_bindings = {}, []
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
            source_sha256 = source_node_digest(tag, {}, source)
            src = (
                bilingual_source(source, target)
                if bilingual and kind != KIND_HEADING and not chapter.preserve_source
                else ""
            )
            if not src:
                target_position = len(body_parts)
                body_parts.append(target_html)
            else:
                src_html = f'<p class="{BILINGUAL_SOURCE_CLASS}">{escape(src)}</p>'
                target_position = len(body_parts) + (1 if order == "source_first" else 0)
                source_position = len(body_parts) + (0 if order == "source_first" else 1)
                body_parts.extend(
                    (src_html, target_html) if order == "source_first" else (target_html, src_html)
                )
                source_pairs.append((source_position, target_position))
            layout_bindings.append((position, target_position, source_sha256))
        body_parts.extend(
            f'<div class="fb2-image"><img src="{escape(href, quote=True)}" alt=""/></div>'
            for href in images_by_position.get(len(paragraphs), [])
        )
        filename = f"ch{chapter_meta['index']}.xhtml"
        chapter_filenames[filename] = chapter_lang
        item = epub.EpubHtml(title=chapter_title, file_name=filename, lang=chapter_lang)
        item.content = f'<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{chapter_lang}"><head><title>{escape(chapter_title)}</title></head><body>{"".join(body_parts)}</body></html>'
        book.add_item(item)
        if theme is not None:
            theme_chapters[item.get_id()] = _theme_chapter(
                item, source_pairs, layout_bindings, chapter.preserve_source
            )
        spine.append(item)
        toc.append(item)
    book.toc = toc
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = spine
    epub.write_epub(out_path, book)
    from trans_novel.assemble.epub.rendering.generated_finalize import annotate_generated_navigation

    annotate_generated_navigation(out_path, chapter_filenames, lang)
    if theme is None:
        return None
    protected_ids = {
        item.get_id() for item in book.get_items() if isinstance(item, epub.EpubCoverHtml)
    }
    scopes = _generated_theme_scopes(out_path, theme_chapters, protected_ids)
    return theme.render(out_path, scopes, bilingual=bilingual)


def _sanitize_filename(name: str, fallback: str = "translated") -> str:
    import re

    value = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", name or "").strip().strip(".")
    return re.sub(r"\s+", " ", value)[:120] or fallback


def _ch_title(chapter: dict) -> str:
    from trans_novel.postprocess.punct import normalize_heading_numbering

    return normalize_heading_numbering(
        (chapter.get("title_translated") or chapter.get("title") or "").strip()
    )
