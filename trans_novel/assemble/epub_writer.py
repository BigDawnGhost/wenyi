"""EPUB template backfill, TOC rewriting, resource copying and new EPUB construction.
Replace translations by physical resource in the original ZIP, update exact NCX/NAV titles
and OPF metadata/language, or build a new EPUB from TXT/FB2 chapters or HTML templates.
"""

from __future__ import annotations

import os
import re
import zipfile
from html import escape

from bs4 import BeautifulSoup, UnicodeDammit
from bs4.element import Tag
from bs4.exceptions import ParserRejectedMarkup

from ..ingest.epub_toc import nav_root_list, nav_toc_scopes
from ..ingest.fb2_reader import read_fb2_binaries
from ..ingest.models import Chapter, Segment
from ..pipeline.runstore import RunStore
from .html_renderer import (
    _BILINGUAL_CSS,
    _BILINGUAL_STYLE_ID,
    _build_source_anchor_ids,
    _index_soup_ids,
    _render_chapter_html,
    _render_paragraph_html,
    _render_segments_html,
    _segment_render_maps,
)
from .html_resources import (
    _IMAGE_EXTENSION_BY_TYPE,
    _package_html_resources,
    _template_resource_source,
)
from .writer_common import (
    _ch_title,
    _epub_lang,
    _export_book_title,
    _manifest_target_lang,
    _merged_paragraphs,
    _sanitize_filename,
)

# XHTML/HTML extensions identify ZIP entries that require translation backfill.
_HTML_EXTS = (".xhtml", ".html", ".htm")

# Detect vertical layout through writing-mode, page-progression-direction and vrtl classes.
_VERTICAL_MARKERS = (
    re.compile(
        rb"(?:-epub-|-webkit-)?writing-mode\s*:\s*(?:vertical-rl|vertical-lr|tb-rl)",
        re.IGNORECASE,
    ),
    re.compile(
        rb"page-progression-direction\s*=\s*['\"]rtl['\"]",
        re.IGNORECASE,
    ),
    re.compile(
        rb"\bclass\s*=\s*['\"][^'\"]*\bvrtl\b",
        re.IGNORECASE,
    ),
)
_HORIZONTAL_OVERRIDE_ID = "trans-novel-horizontal-override"
_XML_ENCODING = re.compile(
    r"(<\?xml[^>]*\bencoding\s*=\s*)(['\"])[^'\"]+\2",
    re.IGNORECASE,
)


# Path and attribute helpers.


def _attr_str(value: object) -> str:
    """Safely narrow a BeautifulSoup attribute to a string."""
    return value if isinstance(value, str) else ""


# OPF metadata.
def _rewrite_opf_metadata(
    data: bytes,
    *,
    book_title: str,
    lang: str,
    force_horizontal: bool,
) -> bytes:
    """Update OPF metadata with the Wenyi version and target language."""
    try:
        soup = BeautifulSoup(data, "xml")
        if book_title:
            title_el = soup.find("dc:title") or soup.find("title")
            if title_el is not None:
                title_el.clear()
                title_el.append(book_title)

        lang_el = soup.find("dc:language") or soup.find("language")
        if lang_el is None:
            metadata = soup.find("metadata")
            if metadata is not None:
                lang_el = soup.new_tag("dc:language")
                metadata.append(lang_el)
        if lang_el is not None:
            lang_el.clear()
            lang_el.append(lang)

        if force_horizontal:
            for spine in soup.find_all("spine"):
                spine["page-progression-direction"] = "ltr"
        return soup.encode()
    except (ParserRejectedMarkup, UnicodeError, ValueError):
        return data


def _epub_looks_vertical(zf: zipfile.ZipFile) -> bool:
    """Detect whether an EPUB declares vertical layout."""
    for info in zf.infolist():
        low = info.filename.lower()
        if not low.endswith((".opf", ".css", ".xhtml", ".html", ".htm")):
            continue
        try:
            data = zf.read(info.filename)
        except (
            EOFError,
            KeyError,
            NotImplementedError,
            OSError,
            RuntimeError,
            zipfile.BadZipFile,
        ):
            data = b""
        if any(marker.search(data) for marker in _VERTICAL_MARKERS):
            return True
    return False


def _rewrite_html_document(
    data: bytes | str,
    *,
    lang: str,
    force_horizontal: bool,
    bilingual: bool = False,
) -> bytes:
    """Set the target HTML language and inject horizontal-layout or bilingual styles as needed."""
    try:
        if isinstance(data, bytes):
            text = UnicodeDammit(data).unicode_markup
            if text is None:
                text = data.decode("utf-8", errors="replace")
        else:
            text = data
        soup = BeautifulSoup(text, "html.parser")
        html = soup.find("html")
        if html is None:
            return text.encode("utf-8")
        html["lang"] = lang
        html["xml:lang"] = lang
        classes = html.get("class")
        if isinstance(classes, list) and "vrtl" in classes:
            html["class"] = " ".join(str(c) for c in classes if c != "vrtl")

        if force_horizontal and soup.find(id=_HORIZONTAL_OVERRIDE_ID) is None:
            head = soup.find("head")
            if head is None:
                head = soup.new_tag("head")
                html.insert(0, head)
            style = soup.new_tag("style", id=_HORIZONTAL_OVERRIDE_ID)
            style.string = (
                "html, body { "
                "writing-mode: horizontal-tb !important; "
                "-epub-writing-mode: horizontal-tb !important; "
                "-webkit-writing-mode: horizontal-tb !important; "
                "direction: ltr !important; "
                "text-orientation: mixed !important; "
                "} "
                '.vrtl, .vertical, [class*="vrtl"] { '
                "writing-mode: horizontal-tb !important; "
                "-epub-writing-mode: horizontal-tb !important; "
                "-webkit-writing-mode: horizontal-tb !important; "
                "direction: ltr !important; "
                "}"
            )
            head.append(style)

        if bilingual and soup.find(id=_BILINGUAL_STYLE_ID) is None:
            head = soup.find("head")
            if head is None:
                head = soup.new_tag("head")
                html.insert(0, head)
            style = soup.new_tag("style", id=_BILINGUAL_STYLE_ID)
            style.string = _BILINGUAL_CSS
            head.append(style)
        output = _XML_ENCODING.sub(r'\1"utf-8"', str(soup))
        return output.encode("utf-8")
    except (ParserRejectedMarkup, UnicodeError, ValueError):
        return data if isinstance(data, bytes) else data.encode("utf-8")


# Table-of-contents backfill.
def _direct_child(parent: Tag | BeautifulSoup, name: str) -> Tag | None:
    """Return the first direct child matching the requested tag."""
    child = parent.find(name, recursive=False)
    return child if isinstance(child, Tag) else None


def _nav_label_nodes(soup: BeautifulSoup) -> list[tuple[Tag, str]]:
    """Enumerate EPUB3 TOC tags and original hrefs in the reader's preorder.
    Prefer each li's direct a child, then its direct span. Exclude li elements with neither
    from node_index. Grouping spans are translatable TOC entries without content targets.
    Recurse through nested lists to keep indices identical to ingestion.
    """
    labels: list[tuple[Tag, str]] = []

    def walk_list(ordered_list: Tag) -> None:
        for child in ordered_list.children:
            if not isinstance(child, Tag) or child.name != "li":
                continue
            label = _direct_child(child, "a") or _direct_child(child, "span")
            if label is not None:
                labels.append((label, _attr_str(label.get("href"))))
            nested = _direct_child(child, "ol")
            if nested is not None:
                walk_list(nested)

    for scope in nav_toc_scopes(soup):
        root = nav_root_list(scope)
        if root is not None:
            walk_list(root)
    return labels


def _ncx_nav_points(soup: BeautifulSoup) -> list[Tag]:
    """Enumerate NCX navPoint elements using the reader's direct-child preorder."""
    nav_map = soup.find("navMap")
    if not isinstance(nav_map, Tag):
        return []
    points: list[Tag] = []

    def walk(parent: Tag) -> None:
        for child in parent.children:
            if not isinstance(child, Tag) or child.name != "navPoint":
                continue
            points.append(child)
            walk(child)

    walk(nav_map)
    return points


def _translated_toc_title(entry: dict[str, object]) -> str:
    """Return a valid translated TOC title, falling back to the original."""
    value = entry.get("title_translated") or entry.get("title")
    return value.strip() if isinstance(value, str) else ""


def _indexed_toc_entries(
    entries: list[dict[str, object]], toc_path: str
) -> dict[int, dict[str, object]]:
    """Index exact TOC nodes by toc_path and node_index."""
    indexed: dict[int, dict[str, object]] = {}
    for entry in entries:
        if entry.get("toc_path") != toc_path:
            continue
        node_index = entry.get("node_index")
        if isinstance(node_index, int) and node_index >= 0:
            indexed[node_index] = entry
    return indexed


def _rewrite_toc(
    data: bytes,
    entries: list[dict[str, object]],
    *,
    is_ncx: bool,
    toc_path: str = "",
) -> bytes:
    """Replace visible NCX/NAV titles while preserving src/href unchanged.
    Locate nodes by toc_path and node_index so fragments in one XHTML can have distinct
    titles and identical basenames in different directories cannot overwrite one another.
    Preserve the original title when no entry matches.
    """
    try:
        exact_entries = _indexed_toc_entries(entries, toc_path)
        if is_ncx:
            soup = BeautifulSoup(data, "xml")
            for node_index, nav_point in enumerate(_ncx_nav_points(soup)):
                nav_label = _direct_child(nav_point, "navLabel")
                label = nav_label.find("text") if nav_label is not None else None
                if not isinstance(label, Tag):
                    continue
                entry = exact_entries.get(node_index)
                if entry is None:
                    continue
                content = _direct_child(nav_point, "content")
                raw_src = _attr_str(content.get("src")) if content else ""
                expected = entry.get("raw_href")
                if isinstance(expected, str) and expected != raw_src:
                    # Preserve original titles when source and state disagree instead of changing the wrong node.
                    continue
                title = _translated_toc_title(entry)
                if title:
                    label.clear()
                    label.append(title)
            return soup.encode()

        # EPUB3 nav.xhtml: enumerate direct li entries only within nav elements with epub:type="toc".
        soup = BeautifulSoup(data, "html.parser")
        toc_navs = [
            node
            for node in soup.find_all("nav")
            if "toc"
            in (
                _attr_str(node.get("epub:type"))
                or _attr_str(node.get("type"))
                or _attr_str(node.get("role"))
            ).split()
            or _attr_str(node.get("role")) == "doc-toc"
        ]
        if not toc_navs and not (
            toc_path and ("nav" in toc_path.lower() or "toc" in toc_path.lower())
        ):
            # Avoid rewriting chapter bodies that were mis-detected as TOC.
            return data

        for node_index, (label, raw_href) in enumerate(_nav_label_nodes(soup)):
            entry = exact_entries.get(node_index)
            if entry is None:
                continue
            expected = entry.get("raw_href")
            if isinstance(expected, str) and expected != raw_href:
                continue
            title = _translated_toc_title(entry)
            if title:
                label.clear()
                label.append(title)
        return str(soup).encode("utf-8")
    except (ParserRejectedMarkup, UnicodeError, ValueError):
        return data


# Physical resource and Segment mapping.
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

    # Import lazily to avoid unnecessary reader/models/writer dependency cycles during loading.
    from ..ingest.epub_reader import _fragment_anchor_map, annotate_epub_resource

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
            for fragment, segment_anchor in _fragment_anchor_map(template).items():
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


# EPUB backfill entry point.
def _assemble_epub(
    store: RunStore,
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


def _is_nav(data: bytes) -> bool:
    """Detect EPUB3 TOC navigation in an HTML resource (nav epub:type=toc)."""
    return b"<nav" in data and (
        b'epub:type="toc"' in data
        or b"epub:type='toc'" in data
        or b'type="toc"' in data
        or b'role="doc-toc"' in data
    )


def _inject_bilingual_style(out_path: str, chapter_filenames: set[str], lang: str) -> None:
    """ebooklib rebuilds chapter heads from templates and drops inline styles. Postprocess the
    written ZIP to restore bilingual styles through _rewrite_html_document.
    """
    with zipfile.ZipFile(out_path, "r") as zin:
        infos = zin.infolist()
        entries = {info.filename: zin.read(info.filename) for info in infos}
    tmp_path = out_path + ".tmp"
    try:
        with zipfile.ZipFile(tmp_path, "w") as zout:
            for info in infos:
                data = entries[info.filename]
                if os.path.basename(info.filename) in chapter_filenames:
                    data = _rewrite_html_document(
                        data,
                        lang=lang,
                        force_horizontal=False,
                        bilingual=True,
                    )
                zout.writestr(info, data)
        os.replace(tmp_path, out_path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _build_epub_from_chapters(
    store: RunStore,
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
    store: RunStore,
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
