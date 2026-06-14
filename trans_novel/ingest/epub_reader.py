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

from bs4 import BeautifulSoup

from .models import KIND_HEADING, KIND_TEXT, Chapter, Document, Segment

_CONTAINER = "META-INF/container.xml"
_BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def _find_opf_path(zf: zipfile.ZipFile) -> str:
    data = zf.read(_CONTAINER)
    root = ET.fromstring(data)
    # container.xml 用了默认命名空间，按 localname 匹配
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == "rootfile":
            return el.attrib["full-path"]
    raise ValueError("EPUB 损坏：container.xml 未找到 rootfile")


def _parse_opf(zf: zipfile.ZipFile, opf_path: str) -> tuple[str, list[str]]:
    """返回 (书名, spine 顺序的 XHTML zip 路径列表)。"""
    root = ET.fromstring(zf.read(opf_path))
    opf_dir = posixpath.dirname(opf_path)

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    title = ""
    manifest: dict[str, tuple[str, str]] = {}  # id -> (href, media-type)
    spine_ids: list[str] = []

    for el in root.iter():
        name = local(el.tag)
        if name == "title" and not title and el.text:
            title = el.text.strip()
        elif name == "item":
            manifest[el.attrib["id"]] = (
                el.attrib.get("href", ""),
                el.attrib.get("media-type", ""),
            )
        elif name == "itemref":
            spine_ids.append(el.attrib["idref"])

    hrefs: list[str] = []
    for sid in spine_ids:
        if sid not in manifest:
            continue
        href, media = manifest[sid]
        if "html" not in media and not href.endswith((".xhtml", ".html", ".htm")):
            continue
        full = posixpath.normpath(posixpath.join(opf_dir, href)) if opf_dir else href
        hrefs.append(full)
    return title, hrefs


def _extract_chapter(html: str, chapter_index: int) -> tuple[str, list[Segment], str]:
    """解析单个 XHTML 文档，返回 (标题, segments, 带标记的模板 HTML)。"""
    soup = BeautifulSoup(html, "html.parser")
    segments: list[Segment] = []
    idx = 0
    for el in soup.find_all(_BLOCK_TAGS):
        # 跳过嵌套在另一个块级元素内的块（避免重复计数，如 blockquote 里的 p）
        if any(getattr(p, "name", None) in _BLOCK_TAGS for p in el.parents):
            continue
        text = el.get_text().strip()
        if not text:
            continue
        anchor = f"tn{chapter_index}_{idx}"
        el["data-tn-id"] = anchor
        kind = KIND_HEADING if el.name in _HEADING_TAGS else KIND_TEXT
        segments.append(Segment(index=idx, source=text, kind=kind, anchor=anchor))
        idx += 1

    # 标题：首个 heading 文本 → <title> → 兜底
    title = ""
    for s in segments:
        if s.kind == KIND_HEADING:
            title = s.source
            break
    if not title and soup.title and soup.title.string:
        title = soup.title.string.strip()
    if not title:
        title = f"第{chapter_index + 1}章"

    return title, segments, str(soup)


def read_epub(path: str, source_lang: str, target_lang: str) -> Document:
    with zipfile.ZipFile(path, "r") as zf:
        names = set(zf.namelist())
        opf_path = _find_opf_path(zf)
        book_title, hrefs = _parse_opf(zf, opf_path)

        chapters: list[Chapter] = []
        ci = 0
        for href in hrefs:
            if href not in names:
                continue
            html = zf.read(href).decode("utf-8", errors="replace")
            title, segments, template = _extract_chapter(html, ci)
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
        meta={"opf_path": opf_path},
    )
