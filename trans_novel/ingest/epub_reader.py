"""EPUB 读取器（纯标准库 + BeautifulSoup）。

EPUB 即一个 zip：
  META-INF/container.xml → 指向 OPF
  OPF → manifest（资源清单）+ spine（阅读顺序）
按 spine 顺序逐个 XHTML 文档当作一章，提取块级元素（p / h1-h6 / li / blockquote）
为 Segment，并在元素上打 data-tn-id 占位标记；整份带标记的 XHTML 存为 chapter.template，
供回填时按标记替换译文。非正文资源（图片/CSS/字体）由 writer 原样拷贝，不在此处理。
"""

from __future__ import annotations

import os
import posixpath
import xml.etree.ElementTree as ET
import zipfile

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from .models import KIND_HEADING, KIND_TEXT, Chapter, Document, Segment

_CONTAINER = "META-INF/container.xml"
_BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_INLINE_META_KEY = "epub_inline"
_INLINE_ID_ATTR = "data-tn-inline-id"
_ATOMIC_INLINE_TAGS = {
    "audio",
    "br",
    "canvas",
    "embed",
    "hr",
    "iframe",
    "img",
    "math",
    "object",
    "svg",
    "video",
}


def _preserved_inline_roots(block: Tag) -> list[Tag]:
    """返回需要原样回填的非文本节点，并尽量保留其无文字包装标签。"""
    roots: list[Tag] = []
    seen: set[int] = set()
    for candidate in block.find_all(True):
        is_atomic = candidate.name in _ATOMIC_INLINE_TAGS
        is_empty_anchor = (
            candidate.name == "a"
            and not candidate.get_text(strip=True)
            and (candidate.has_attr("id") or candidate.has_attr("name"))
        )
        if not is_atomic and not is_empty_anchor:
            continue

        root = candidate
        parent = root.parent
        while (
            isinstance(parent, Tag)
            and parent is not block
            and parent.name not in _BLOCK_TAGS
            and not parent.get_text(strip=True)
        ):
            root = parent
            parent = root.parent
        if id(root) not in seen:
            seen.add(id(root))
            roots.append(root)
    return roots


def _segment_content(block: Tag, anchor: str) -> tuple[str, dict[str, object]]:
    """提取可翻译文本，并给内联非文本节点写入稳定 ID 和位置元数据。"""
    roots = _preserved_inline_roots(block)
    root_ids = {id(node) for node in roots}
    text_parts: list[str] = []
    node_offsets: list[tuple[Tag, int]] = []
    raw_length = 0

    def walk(parent: Tag) -> None:
        nonlocal raw_length
        for child in parent.children:
            if isinstance(child, Tag):
                if id(child) in root_ids:
                    node_offsets.append((child, raw_length))
                else:
                    walk(child)
            elif isinstance(child, NavigableString) and not isinstance(child, Comment):
                value = str(child)
                text_parts.append(value)
                raw_length += len(value)

    walk(block)
    raw_text = "".join(text_parts)
    text = raw_text.strip()
    if not text:
        return "", {}

    leading = len(raw_text) - len(raw_text.lstrip())
    source_length = len(text)
    nodes: list[dict[str, object]] = []
    for index, (node, raw_offset) in enumerate(node_offsets):
        inline_id = f"{anchor}_inline_{index}"
        offset = min(max(raw_offset - leading, 0), source_length)
        placement = (
            "before"
            if offset == 0
            else "after"
            if offset == source_length
            else "inline"
        )
        node[_INLINE_ID_ATTR] = inline_id
        nodes.append(
            {
                "id": inline_id,
                "tag": node.name,
                "placement": placement,
                "offset": offset,
            }
        )

    meta: dict[str, object] = {}
    if nodes:
        meta[_INLINE_META_KEY] = {
            "version": 1,
            "source_length": source_length,
            "nodes": nodes,
        }
    return text, meta


def _find_opf_path(zf: zipfile.ZipFile) -> str:
    data = zf.read(_CONTAINER)
    root = ET.fromstring(data)
    # container.xml 用了默认命名空间，按 localname 匹配
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == "rootfile":
            return el.attrib["full-path"]
    raise ValueError("EPUB 损坏：container.xml 未找到 rootfile")


def _zip_href(base_path: str, href: str) -> str:
    """Resolve an EPUB-relative href to a normalized zip member path."""
    clean = (href or "").split("#", 1)[0]
    if not clean:
        return ""
    base_dir = posixpath.dirname(base_path)
    return posixpath.normpath(posixpath.join(base_dir, clean)) if base_dir else clean


def _attr_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _parse_opf(zf: zipfile.ZipFile, opf_path: str) -> tuple[str, list[str], list[str]]:
    """返回 (书名, spine 顺序的 XHTML zip 路径列表, TOC/NAV 文件路径列表)。"""
    root = ET.fromstring(zf.read(opf_path))

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    title = ""
    manifest: dict[str, tuple[str, str, str]] = {}  # id -> (href, media-type, properties)
    spine_ids: list[str] = []
    toc_ids: list[str] = []

    for el in root.iter():
        name = local(el.tag)
        if name == "title" and not title and el.text:
            title = el.text.strip()
        elif name == "item":
            manifest[el.attrib["id"]] = (
                el.attrib.get("href", ""),
                el.attrib.get("media-type", ""),
                el.attrib.get("properties", ""),
            )
        elif name == "itemref":
            spine_ids.append(el.attrib["idref"])
        elif name == "spine":
            toc = el.attrib.get("toc")
            if toc:
                toc_ids.append(toc)

    hrefs: list[str] = []
    for sid in spine_ids:
        if sid not in manifest:
            continue
        href, media, _props = manifest[sid]
        if "html" not in media and not href.endswith((".xhtml", ".html", ".htm")):
            continue
        hrefs.append(_zip_href(opf_path, href))

    toc_paths: list[str] = []
    for item_id, (href, media, props) in manifest.items():
        if item_id in toc_ids or "nav" in props.split() or media == "application/x-dtbncx+xml":
            toc_paths.append(_zip_href(opf_path, href))
    return title, hrefs, toc_paths


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _toc_label_map(zf: zipfile.ZipFile, toc_paths: list[str]) -> dict[str, str]:
    """Return zip href -> first TOC label from NCX/NAV documents."""
    labels: dict[str, str] = {}
    names = set(zf.namelist())
    for toc_path in toc_paths:
        if toc_path not in names:
            continue
        data = zf.read(toc_path)
        if toc_path.lower().endswith(".ncx"):
            root = ET.fromstring(data)
            for nav_point in root.iter():
                if _local(nav_point.tag) != "navPoint":
                    continue
                label = ""
                src = ""
                for child in nav_point.iter():
                    name = _local(child.tag)
                    if name == "text" and child.text and not label:
                        label = child.text.strip()
                    elif name == "content" and not src:
                        src = child.attrib.get("src", "")
                href = _zip_href(toc_path, src)
                if href and label and href not in labels:
                    labels[href] = label
            continue

        soup = BeautifulSoup(data, "html.parser")
        toc_navs = [
            n for n in soup.find_all("nav")
            if "toc" in (_attr_str(n.get("epub:type")) or _attr_str(n.get("type"))).split()
        ]
        for nav in toc_navs or [soup]:
            for a in nav.find_all("a", href=True):
                label = a.get_text(" ", strip=True)
                href = _zip_href(toc_path, _attr_str(a.get("href")))
                if href and label and href not in labels:
                    labels[href] = label
    return labels


def _looks_like_internal_title(title: str, href: str, book_title: str = "") -> bool:
    base = posixpath.basename(href).rsplit(".", 1)[0]
    stripped = title.strip()
    return (bool(base) and stripped == base) or (bool(book_title) and stripped == book_title.strip())


def _extract_chapter(
    html: str,
    chapter_index: int,
    href: str,
    *,
    book_title: str = "",
    toc_title: str = "",
) -> tuple[str, list[Segment], str]:
    """解析单个 XHTML 文档，返回 (标题, segments, 带标记的模板 HTML)。"""
    soup = BeautifulSoup(html, "html.parser")
    segments: list[Segment] = []
    idx = 0
    for el in soup.find_all(_BLOCK_TAGS):
        # 跳过嵌套在另一个块级元素内的块（避免重复计数，如 blockquote 里的 p）
        if any(getattr(p, "name", None) in _BLOCK_TAGS for p in el.parents):
            continue
        anchor = f"tn{chapter_index}_{idx}"
        text, meta = _segment_content(el, anchor)
        if not text:
            continue
        el["data-tn-id"] = anchor
        kind = KIND_HEADING if el.name in _HEADING_TAGS else KIND_TEXT
        segments.append(
            Segment(
                index=idx,
                source=text,
                kind=kind,
                anchor=anchor,
                meta=meta,
            )
        )
        idx += 1

    # 标题：官方 TOC → 首个 heading 文本 → 非内部文件名/书名的 <title> → 无标题。
    # 一些 EPUB 把 XHTML 文件名写进 <title>，如 cUH.xhtml 的 <title>cUH</title>，
    # 或把全书书名写进每个 <title>，这不是读者可见章节标题，不能进入目录或标题翻译。
    title = toc_title.strip()
    if not title:
        for s in segments:
            if s.kind == KIND_HEADING:
                title = s.source
                break
    if not title and soup.title and soup.title.string:
        candidate = soup.title.string.strip()
        if not _looks_like_internal_title(candidate, href, book_title):
            title = candidate

    return title, segments, str(soup)


def read_epub(path: str, source_lang: str, target_lang: str) -> Document:
    with zipfile.ZipFile(path, "r") as zf:
        names = set(zf.namelist())
        opf_path = _find_opf_path(zf)
        book_title, hrefs, toc_paths = _parse_opf(zf, opf_path)
        toc_titles = _toc_label_map(zf, toc_paths)
        toc_entries = [
            {"href": href, "title": title}
            for href, title in toc_titles.items()
            if href and title
        ]

        chapters: list[Chapter] = []
        ci = 0
        for href in hrefs:
            if href not in names:
                continue
            html = zf.read(href).decode("utf-8", errors="replace")
            title, segments, template = _extract_chapter(
                html, ci, href, book_title=book_title, toc_title=toc_titles.get(href, "")
            )
            if not any(s.source.strip() for s in segments):
                continue  # 无正文（封面/版权页等）→ writer 原样拷贝，不作为章节
            chapters.append(
                Chapter(
                    index=ci,
                    title=title,
                    segments=segments,
                    href=href,
                    template=template,
                )
            )
            ci += 1

    return Document(
        title=book_title or os.path.splitext(os.path.basename(path))[0],
        source_lang=source_lang,
        target_lang=target_lang,
        fmt="epub",
        source_path=os.path.abspath(path),
        chapters=chapters,
        meta={"opf_path": opf_path, "toc_paths": toc_paths, "toc_entries": toc_entries},
    )
