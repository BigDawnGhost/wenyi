"""EPUB 读取器（纯标准库 + BeautifulSoup）。

EPUB 即一个 zip：
  META-INF/container.xml → 指向 OPF
  OPF → manifest（资源清单）+ spine（阅读顺序）

读取时先按 spine 提取物理 XHTML 资源，再根据 NCX/NAV 的顶层目录锚点
切成逻辑 Chapter。因此 Chapter 与 XHTML 不再是一对一：切章之后，每个
Segment 的 ``resource_href`` 仍记录它所属的物理资源，writer 据此聚合回填。
"""

from __future__ import annotations

import os
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, UnicodeDammit
from bs4.element import Comment, NavigableString, Tag

from .epub_chapters import get_chapter_split_strategy
from .epub_toc import parse_toc_entries, resolve_epub_href
from .models import KIND_HEADING, KIND_TEXT, Chapter, Document, Segment

_CONTAINER = "META-INF/container.xml"
_BLOCK_TAGS = {
    "p",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "blockquote",
    "td",
    "th",
    "dt",
    "dd",
    "figcaption",
}
_BLOCK_CANDIDATE_TAGS = _BLOCK_TAGS | {"div"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_INLINE_META_KEY = "epub_inline"
_INLINE_ID_ATTR = "data-tn-inline-id"
_ANNOTATION_META_KEY = "epub_annotations"
_ANNOTATION_ID_ATTR = "data-tn-annotation-id"
_ANNOTATION_MARKER_ONLY = re.compile(r"^[\d\s*＊※†‡\[\]()〔〕（）{}↩↵←↑↓.·:：\-]+$")
_ANNOTATION_HINT = re.compile(
    r"(?:^|[^a-z0-9])(?:note|noteref|footnote|endnote|fn|jpref|jpnote|ref|key)"
    r"(?:[-_]?\d+)?(?:$|[^a-z0-9])",
    re.IGNORECASE,
)
_ATOMIC_INLINE_TAGS = {
    "audio",
    "canvas",
    "embed",
    "hr",
    "iframe",
    "img",
    "math",
    "object",
    "source",
    "svg",
    "video",
}

_LINE_WRAPPER_ATTR = "data-tn-line"


def _preserved_inline_roots(block: Tag) -> list[Tag]:
    """返回需要原样回填的非文本节点，并尽量保留其无文字包装标签。"""
    roots: list[Tag] = []
    seen: set[int] = set()
    for candidate in block.find_all(True):
        if candidate.has_attr(_ANNOTATION_ID_ATTR):
            # 注释根由 ``epub_annotations`` 单独恢复，不能再作为普通内联
            # 节点记录一份。其内部的图片等原子节点仍需独立记录，否则范围
            # 链接重建正文时会把这些节点一并清空。
            continue
        is_atomic = candidate.name in _ATOMIC_INLINE_TAGS
        is_empty_anchor = (
            candidate.name in {"a", "span"}
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
            and not parent.has_attr(_ANNOTATION_ID_ATTR)
            and not parent.get_text(strip=True)
        ):
            root = parent
            parent = root.parent
        if id(root) not in seen:
            seen.add(id(root))
            roots.append(root)
    return roots


def _is_internal_link(link: Tag) -> bool:
    """判断链接是否指向 EPUB 包内资源，而非 Web、邮件或脚本地址。"""
    raw_href = link.get("href")
    if not isinstance(raw_href, str) or not raw_href.strip():
        return False
    parsed = urlsplit(raw_href.strip())
    return not parsed.scheme and not parsed.netloc


def _nearest_marker_wrapper(link: Tag, block: Tag) -> Tag | None:
    """返回 link 与当前翻译块之间最近的语义上下标包装。

    除原生 ``sup``/``sub`` 外，一些 EPUB 用带明确 class 或内联样式的
    ``span`` 配合 CSS 实现角标。此处只接受清晰声明上下标的包装，普通
    ``span`` 仍会按正文处理。
    """

    def is_marker_wrapper(node: Tag) -> bool:
        if node.name in {"sup", "sub"}:
            return True
        if node.name != "span":
            return False
        classes = {
            str(value).strip().lower()
            for value in node.get_attribute_list("class")
            if str(value).strip()
        }
        if classes & {"sup", "super", "superscript", "sub", "subscript"}:
            return True
        style = node.get("style")
        return isinstance(style, str) and bool(
            re.search(r"(?:^|;)\s*vertical-align\s*:\s*(?:super|sub)\b", style, re.IGNORECASE)
        )

    parent = link.parent
    while isinstance(parent, Tag):
        if parent is block:
            return parent if is_marker_wrapper(parent) else None
        if is_marker_wrapper(parent):
            return parent
        parent = parent.parent
    return None


def _has_annotation_hint(link: Tag, marker: Tag, marker_text: str) -> bool:
    """根据 fragment 与语义属性判断编号是否确为注释，而非数学上标。"""
    decorated = bool(re.search(r"[^\d\s.·:\-]", marker_text))
    parsed = urlsplit(str(link.get("href", "")))
    attrs: list[str] = [parsed.fragment]
    for node in (link, marker):
        for key in ("id", "class", "role", "rel", "epub:type"):
            value = node.get(key)
            if isinstance(value, list):
                attrs.extend(str(item) for item in value)
            elif value is not None:
                attrs.append(str(value))
    hint = " ".join(attrs)
    short_numbered_fragment = bool(re.fullmatch(r"[a-zA-Z]?[\-_]?\d+", parsed.fragment))
    numbered_note_fragment = bool(
        re.search(
            r"(?:notes?|footnotes?|endnotes?|fn)[\-_]?\d+$",
            parsed.fragment,
            re.IGNORECASE,
        )
    )
    return (
        decorated
        or bool(_ANNOTATION_HINT.search(hint))
        or short_numbered_fragment
        or numbered_note_fragment
    )


def _range_marker_node(link: Tag) -> Tag | None:
    """识别范围链接末尾的高置信度注释号，避免误删语义上下标。

    ``H<sub>2</sub>O``、``CO<sub>2</sub>`` 和公式指数都是正文，不能因为
    使用 ``sup/sub`` 就从送译文本中删除。第一版只接受位于链接末尾、文字
    形似编号，并且 href/id/class/语义属性或装饰符提供注释线索的节点。
    """
    significant = [
        child
        for child in link.children
        if not (
            isinstance(child, NavigableString)
            and not isinstance(child, Comment)
            and not str(child).strip()
        )
    ]
    if not significant:
        return None
    candidate = significant[-1]
    if not isinstance(candidate, Tag) or candidate.name not in {"sup", "sub"}:
        return None
    marker_text = candidate.get_text("", strip=True)
    if not marker_text or not _ANNOTATION_MARKER_ONLY.fullmatch(marker_text):
        return None

    # 数字下标几乎总是化学式或数学正文；只有带括号、星号、箭头等明显
    # 注释装饰时才允许把 sub 当标记。
    decorated = bool(re.search(r"[^\d\s.·:\-]", marker_text))
    if candidate.name == "sub" and not decorated:
        return None
    return candidate if _has_annotation_hint(link, candidate, marker_text) else None


def _semantic_link_text(link: Tag, marker_node: Tag | None = None) -> str:
    """返回链接正文，只排除已确认的末尾注释号。"""
    parts: list[str] = []

    def collect(parent: Tag) -> None:
        for child in parent.children:
            if isinstance(child, Tag):
                if child is marker_node or child.name in {"rt", "rp"}:
                    continue
                collect(child)
            elif isinstance(child, NavigableString) and not isinstance(child, Comment):
                parts.append(str(child))

    collect(link)
    return re.sub(r"[ \t\r\n\f\v]+", " ", "".join(parts)).strip()


def _annotation_roots(block: Tag, anchor: str) -> dict[int, dict[str, object]]:
    """识别段内链接，给其 DOM 根节点编号并返回临时提取规格。"""
    # ``block`` 自身若是普通 a（典型为 ``li > a``），writer 替换其子文字时
    # 天然保留 href，无需再请求模型定位。只有内部还带 sup/sub 注释号时才
    # 记录自身，以免 clear() 一并删除标记结构。
    links: list[Tag] = []
    if block.name == "a" and block.has_attr("href") and block.find(["sup", "sub"]):
        links.append(block)
    links.extend(block.find_all("a", href=True))

    roots: dict[int, dict[str, object]] = {}
    ordinal = 0
    for link in links:
        if not _is_internal_link(link):
            continue

        marker_wrapper = _nearest_marker_wrapper(link, block)
        if marker_wrapper is not None:
            wrapper_text = marker_wrapper.get_text("", strip=True)
            if not _has_annotation_hint(link, marker_wrapper, wrapper_text):
                marker_wrapper = None
        range_marker = None if marker_wrapper is not None else _range_marker_node(link)
        semantic_text = _semantic_link_text(link, range_marker)
        if not semantic_text and marker_wrapper is None:
            # 纯图片链接及空锚点没有需要跨语言定位的正文。让既有原子内联
            # 机制原样保留整个 ``a`` 外壳，避免把图片误当脚注并清空。
            continue
        marker_only = bool(
            _ANNOTATION_MARKER_ONLY.fullmatch(semantic_text)
            and _has_annotation_hint(link, link, semantic_text)
        )
        mode = "point" if marker_wrapper is not None or marker_only else "range"
        root = marker_wrapper if mode == "point" and marker_wrapper is not None else link

        # 一个结构根只记录一次。规范 XHTML 中不会嵌套 a，但此防线可避免
        # 损坏文档让同一 sup/sub 被多个链接重复编号。
        if id(root) in roots:
            continue

        annotation_id = f"{anchor}_annotation_{ordinal}"
        ordinal += 1
        if mode == "point":
            marker_text = root.get_text("", strip=True)
        else:
            marker_text = range_marker.get_text("", strip=True) if range_marker is not None else ""
        root[_ANNOTATION_ID_ATTR] = annotation_id
        roots[id(root)] = {
            "id": annotation_id,
            "mode": mode,
            "marker_text": marker_text,
            "marker_node_ids": {id(range_marker)} if range_marker is not None else set(),
            "root": root,
        }
    return roots


def _normalize_html_text(
    raw_text: str,
    offsets: list[int],
) -> tuple[str, list[int]]:
    """折叠 HTML 排版空白，并把原始字符边界映射到规范化文本。"""
    output: list[str] = []
    boundary_map = [0] * (len(raw_text) + 1)
    for index, char in enumerate(raw_text):
        boundary_map[index] = len(output)
        if char in " \t\r\n\f\v":
            if not output or output[-1] != " ":
                output.append(" ")
        else:
            output.append(char)
    boundary_map[len(raw_text)] = len(output)

    collapsed = "".join(output)
    leading = len(collapsed) - len(collapsed.lstrip())
    text = collapsed.strip()
    mapped = [
        min(max(boundary_map[min(max(offset, 0), len(raw_text))] - leading, 0), len(text))
        for offset in offsets
    ]
    return text, mapped


def _segment_content(
    block: Tag,
    anchor: str,
    annotation_roots: dict[int, dict[str, object]] | None = None,
) -> tuple[str, dict[str, object]]:
    """提取可翻译文本，并给内联非文本节点写入稳定 ID 和位置元数据。

    XHTML 源码中的排版空白按浏览器规则折叠。``br`` 已在选择翻译
    目标时拆成独立视觉行，因此不会进入单个 Segment 的文本。
    """
    annotations = annotation_roots or {}
    marker_node_ids: set[int] = set()
    for annotation in annotations.values():
        raw_marker_ids = annotation.get("marker_node_ids")
        if isinstance(raw_marker_ids, set):
            marker_node_ids.update(
                marker_id for marker_id in raw_marker_ids if isinstance(marker_id, int)
            )
    roots = _preserved_inline_roots(block)
    root_ids = {id(node) for node in roots}
    text_parts: list[str] = []
    preserved_nodes: list[tuple[Tag, int]] = []
    annotation_events: dict[str, tuple[int, int]] = {}
    raw_length = 0

    def append_text(value: str) -> None:
        """追加原始文字，并维护 DOM 边界对应的字符位置。"""
        nonlocal raw_length
        text_parts.append(value)
        raw_length += len(value)

    def walk(parent: Tag, *, inside_range: bool = False) -> None:
        """递归收集正文文本节点，并记录需保留节点的源文偏移。"""
        for child in parent.children:
            if isinstance(child, Tag):
                if child.name in {"rt", "rp"}:
                    # 振假名与不支持 ruby 时显示的备用括号都不是正文；
                    # 保留在模板中，但不要把 ``漢字（かんじ）`` 拆成
                    # 可翻译源文里的 ``漢字（）``。
                    continue
                if inside_range and id(child) in marker_node_ids:
                    # range 链接的注释号属于结构标记，不进入待译文字。
                    continue
                annotation = annotations.get(id(child))
                if annotation is not None:
                    annotation_id = str(annotation["id"])
                    start = raw_length
                    if annotation["mode"] == "range":
                        walk(child, inside_range=True)
                    annotation_events[annotation_id] = (start, raw_length)
                if id(child) in root_ids:
                    preserved_nodes.append((child, raw_length))
                elif annotation is not None:
                    continue
                else:
                    walk(child, inside_range=inside_range)
            elif isinstance(child, NavigableString) and not isinstance(child, Comment):
                value = str(child)
                if (
                    inside_range
                    and isinstance(child.next_sibling, Tag)
                    and id(child.next_sibling) in marker_node_ids
                ):
                    # range 注释号通常位于链接末尾。源码为缩进而留在
                    # sup/sub 前的换行不是正文，去掉标记时也去掉该尾空白。
                    value = value.rstrip(" \t\r\n\f\v")
                if inside_range and not value.strip():
                    previous = child.previous_sibling
                    has_later_text = any(
                        sibling.get_text(strip=True)
                        if isinstance(sibling, Tag)
                        else isinstance(sibling, NavigableString) and bool(str(sibling).strip())
                        for sibling in child.next_siblings
                    )
                    if (
                        isinstance(previous, Tag)
                        and id(previous) in marker_node_ids
                        and not has_later_text
                    ):
                        # 注释号之后、range 链接闭合前的缩进同样不是正文。
                        continue
                append_text(value)

    block_annotation = annotations.get(id(block))
    if block_annotation is not None:
        annotation_id = str(block_annotation["id"])
        if block_annotation["mode"] == "range":
            walk(block, inside_range=True)
        annotation_events[annotation_id] = (0, raw_length)
    else:
        walk(block)

    raw_text = "".join(text_parts)
    event_offsets = [offset for _node, offset in preserved_nodes]
    ordered_annotations = list(annotations.values())
    for annotation in ordered_annotations:
        start, end = annotation_events.get(str(annotation["id"]), (raw_length, raw_length))
        event_offsets.extend((start, end))
    text, normalized_offsets = _normalize_html_text(raw_text, event_offsets)
    if not text:
        return "", {}

    source_length = len(text)
    nodes: list[dict[str, object]] = []
    offset_cursor = 0
    for index, (node, _raw_offset) in enumerate(preserved_nodes):
        inline_id = f"{anchor}_inline_{index}"
        offset = normalized_offsets[offset_cursor]
        offset_cursor += 1
        placement = "before" if offset == 0 else "after" if offset == source_length else "inline"
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
    annotation_items: list[dict[str, object]] = []
    for annotation in ordered_annotations:
        start = normalized_offsets[offset_cursor]
        end = normalized_offsets[offset_cursor + 1]
        offset_cursor += 2
        annotation_items.append(
            {
                "id": annotation["id"],
                "mode": annotation["mode"],
                "source_start": start,
                "source_end": end,
                "source_text": text[start:end],
                "marker_text": annotation["marker_text"],
            }
        )
    if annotation_items:
        meta[_ANNOTATION_META_KEY] = {
            "version": 1,
            "source_length": source_length,
            "items": annotation_items,
        }
    return text, meta


def _has_meaningful_descendant_block(element: Tag) -> bool:
    """块内若已有更细粒度的正文块，则外层只作为布局容器保留。"""
    return any(
        descendant.get_text(strip=True) for descendant in element.find_all(_BLOCK_CANDIDATE_TAGS)
    )


def _list_item_link_target(element: Tag) -> Tag | None:
    """返回列表项自己的直接链接标签，避免回填时清空 ``li`` 和子列表。"""
    link = element.find("a", recursive=False)
    return link if isinstance(link, Tag) and link.get_text(strip=True) else None


def _split_direct_break_lines(element: Tag, soup: BeautifulSoup) -> list[Tag]:
    """把直接 ``br`` 分隔的可见行包装为独立翻译目标，原 ``br`` 不动。"""
    children = list(element.children)
    if not any(isinstance(child, Tag) and child.name == "br" for child in children):
        return [element]

    runs: list[list[Tag | NavigableString]] = [[]]
    for child in children:
        if isinstance(child, Tag) and child.name == "br":
            runs.append([])
        elif isinstance(child, (Tag, NavigableString)):
            runs[-1].append(child)

    targets: list[Tag] = []
    for run in runs:
        has_text = any(
            node.get_text(strip=True)
            if isinstance(node, Tag)
            else not isinstance(node, Comment) and bool(str(node).strip())
            for node in run
        )
        if not has_text:
            continue
        wrapper = soup.new_tag("span")
        wrapper[_LINE_WRAPPER_ATTR] = "true"
        run[0].insert_before(wrapper)
        for node in run:
            wrapper.append(node.extract())
        targets.append(wrapper)
    return targets


def _translation_targets(
    soup: BeautifulSoup,
    *,
    skip_navigation: bool,
) -> list[Tag]:
    """按文档顺序选择可安全替换内容的最细粒度 EPUB 节点。

    含子正文块的 ``div``/``blockquote`` 等仅作为容器保留；``li`` 的
    直接链接文字单独成为翻译目标，从而同时保留列表层级和 ``href``。
    """
    targets: list[Tag] = []
    for element in soup.find_all(_BLOCK_CANDIDATE_TAGS):
        if skip_navigation and _inside_navigation_list(element):
            continue

        has_descendant_block = _has_meaningful_descendant_block(element)
        if element.name == "li":
            link = _list_item_link_target(element)
            if link is not None:
                targets.extend(_split_direct_break_lines(link, soup))
            if link is not None or has_descendant_block:
                continue

        if has_descendant_block:
            continue
        targets.extend(_split_direct_break_lines(element, soup))
    return targets


def _find_opf_path(zf: zipfile.ZipFile) -> str:
    """从 container.xml 解析 EPUB 包文档的 zip 内路径。"""
    data = zf.read(_CONTAINER)
    root = ET.fromstring(data)
    # container.xml 用了默认命名空间，按 localname 匹配
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == "rootfile":
            path = el.attrib.get("full-path", "").strip()
            if path:
                return path
    raise ValueError("EPUB 损坏：container.xml 未找到有效的 rootfile full-path")


def _zip_href(base_path: str, href: str) -> str:
    """Resolve an EPUB-relative href to a normalized zip member path."""
    return resolve_epub_href(base_path, href).resource_href


def _parse_opf(zf: zipfile.ZipFile, opf_path: str) -> tuple[str, list[str], list[str]]:
    """返回 (书名, spine 顺序的 XHTML zip 路径列表, TOC/NAV 文件路径列表)。"""
    root = ET.fromstring(zf.read(opf_path))

    def local(tag: str) -> str:
        """去掉 XML 命名空间并返回标签本地名。"""
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
            item_id = el.attrib.get("id", "").strip()
            if not item_id:
                continue
            manifest[item_id] = (
                el.attrib.get("href", ""),
                el.attrib.get("media-type", ""),
                el.attrib.get("properties", ""),
            )
        elif name == "itemref":
            idref = el.attrib.get("idref", "").strip()
            if idref:
                spine_ids.append(idref)
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
        resolved_href = _zip_href(opf_path, href)
        if resolved_href and resolved_href not in hrefs:
            # 同一物理资源可被 spine 重复引用，但 zip 中仍只有一份
            # XHTML；只标注一次，避免生成无法回填的第二套锚点。
            hrefs.append(resolved_href)

    # EPUB3 NAV 是主目录；没有 NAV 时优先使用 spine.toc 指定的
    # EPUB2 NCX。其它目录仍保留供标题回填，但不与主目录混合切章。
    nav_ids = [
        item_id for item_id, (_href, _media, props) in manifest.items() if "nav" in props.split()
    ]
    ncx_ids = [
        item_id
        for item_id, (_href, media, _props) in manifest.items()
        if media == "application/x-dtbncx+xml"
    ]
    ordered_toc_ids = nav_ids + toc_ids + ncx_ids
    toc_paths: list[str] = []
    for item_id in ordered_toc_ids:
        if item_id not in manifest:
            continue
        href = _zip_href(opf_path, manifest[item_id][0])
        if href and href not in toc_paths:
            toc_paths.append(href)
    return title, hrefs, toc_paths


def _decode_markup(data: bytes) -> str:
    """按 XML/HTML 声明与字节特征解码 XHTML，最后才使用 UTF-8 替换兜底。"""
    decoded = UnicodeDammit(data).unicode_markup
    return decoded if decoded is not None else data.decode("utf-8", errors="replace")


def _looks_like_internal_title(title: str, href: str, book_title: str = "") -> bool:
    """判断 XHTML title 是否只是内部文件名或重复的全书书名。"""
    base = posixpath.basename(href).rsplit(".", 1)[0]
    stripped = title.strip()
    return (bool(base) and stripped == base) or (
        bool(book_title) and stripped == book_title.strip()
    )


def annotate_epub_resource(
    html: str,
    resource_index: int,
    href: str,
    *,
    book_title: str = "",
    skip_navigation: bool = False,
) -> tuple[str, list[Segment], str]:
    """标注单个物理 XHTML，返回标题、Segment 和可回填模板。

    锚点使用物理资源序号而非最终 Chapter 序号，因此即使改用其它
    逻辑切章策略，writer 重建模板时仍能生成相同的 ``data-tn-id``。
    """
    soup = BeautifulSoup(html, "html.parser")
    segments: list[Segment] = []
    first_heading: Tag | None = None
    heading_title_parts: list[str] = []
    idx = 0
    for el in _translation_targets(soup, skip_navigation=skip_navigation):
        anchor = f"tn{resource_index}_{idx}"
        annotations = _annotation_roots(el, anchor)
        protected_annotation_nodes: set[int] = set()
        range_annotation_roots: set[int] = set()
        for annotation in annotations.values():
            root = annotation.get("root")
            if not isinstance(root, Tag):
                continue
            protected_annotation_nodes.add(id(root))
            if annotation.get("mode") == "point":
                protected_annotation_nodes.update(id(node) for node in root.find_all(True))
                continue
            range_annotation_roots.add(id(root))
            raw_marker_ids = annotation.get("marker_node_ids")
            marker_ids = raw_marker_ids if isinstance(raw_marker_ids, set) else set()
            for node in root.find_all(True):
                if id(node) in marker_ids or any(
                    id(parent) in marker_ids for parent in node.parents if parent is not root
                ):
                    protected_annotation_nodes.add(id(node))
        # 带文字的内联 id/name 包装会在回填纯译文时被拍平。先把它
        # 改成同位置的空锚点，便可复用现有内联非文本节点恢复机制。
        for descendant in list(el.find_all(True)):
            if not descendant.get_text(strip=True):
                continue
            if id(descendant) in protected_annotation_nodes:
                # point 根、range 根及其已确认的注释号必须保留属性；range
                # 内其它语义包装仍按普通规则把 id/name 迁成空锚点，writer
                # 才能在清空源文节点后恢复这些跳转目标。
                continue
            anchor_attrs = {
                key: descendant.attrs.pop(key) for key in ("id", "name") if key in descendant.attrs
            }
            if anchor_attrs:
                # HTML 不允许 a 内再嵌套 a；范围链接内部的跳转目标改用
                # 等价的空 span，保留 id/name 而不破坏外层链接结构。
                inside_range_link = any(
                    id(parent) in range_annotation_roots for parent in descendant.parents
                )
                marker = soup.new_tag("span" if inside_range_link else "a")
                marker.attrs.update(anchor_attrs)
                descendant.insert_before(marker)

        text, meta = _segment_content(el, anchor, annotations)
        if not text:
            continue
        el["data-tn-id"] = anchor
        kind = (
            KIND_HEADING
            if el.name in _HEADING_TAGS or el.find_parent(_HEADING_TAGS) is not None
            else KIND_TEXT
        )
        if kind == KIND_HEADING:
            heading = el if el.name in _HEADING_TAGS else el.find_parent(_HEADING_TAGS)
            if isinstance(heading, Tag):
                if first_heading is None:
                    first_heading = heading
                if heading is first_heading:
                    heading_title_parts.append(text)
        segments.append(
            Segment(
                index=idx,
                source=text,
                kind=kind,
                anchor=anchor,
                resource_href=href,
                meta=meta,
            )
        )
        idx += 1

    # 物理资源的备用标题：首个 heading → 非内部文件名/书名的
    # <title> → 无标题。逻辑章标题在后续切章时直接取完整 TOC 节点。
    # 一些 EPUB 把 XHTML 文件名写进 <title>，如 cUH.xhtml 的 <title>cUH</title>，
    # 或把全书书名写进每个 <title>，这不是读者可见章节标题，不能进入目录或标题翻译。
    title = " ".join(heading_title_parts)
    if not title and soup.title and soup.title.string:
        candidate = soup.title.string.strip()
        if not _looks_like_internal_title(candidate, href, book_title):
            title = candidate

    return title, segments, str(soup)


def _inside_navigation_list(element: Tag) -> bool:
    """判断块元素是否属于 EPUB3 ``nav`` 的目录列表结构。

    这里只保护 ``li`` 及其内部块，避免普通回填清空链接和嵌套 ``ol``；
    位于 ``nav`` 内但不属于列表的可见标题/说明文字仍应进入翻译流程。
    """
    inside_nav = False
    inside_list_item = element.name == "li"
    for parent in element.parents:
        if not isinstance(parent, Tag):
            continue
        if parent.name == "li":
            inside_list_item = True
        elif parent.name == "nav":
            inside_nav = True
            break
    return inside_nav and inside_list_item


def _fragment_anchor_map(template: str) -> dict[str, str | None]:
    """把 XHTML 中的 id/name 定位到 Segment 锚点。

    值为 ``None`` 表示 ID 确实存在，但它位于该资源最后一个
    可翻译块之后；这与“fragment 根本不存在”必须区分。
    """
    soup = BeautifulSoup(template, "html.parser")
    mapping: dict[str, str | None] = {}
    for node in soup.find_all(True):
        identifiers = [node.get("id"), node.get("name")]
        if not any(isinstance(value, str) and value for value in identifiers):
            continue
        block = (
            node if node.has_attr("data-tn-id") else node.find_parent(attrs={"data-tn-id": True})
        )
        if not isinstance(block, Tag):
            block = node.find_next(attrs={"data-tn-id": True})
        raw_anchor = block.get("data-tn-id") if isinstance(block, Tag) else None
        anchor = raw_anchor if isinstance(raw_anchor, str) and raw_anchor else None
        for value in identifiers:
            if isinstance(value, str) and value:
                mapping.setdefault(value, anchor)
    return mapping


def _logical_chapters(
    resources: list[dict[str, object]],
    toc_entries: list[dict[str, object]],
) -> tuple[list[Chapter], str, str]:
    """按当前策略把物理资源流切成逻辑 Chapter。

    无可用目录边界时回退为每个非空 spine XHTML 一章，与历来行为
    一致。如首个目录边界前仍有正文，它会成为独立前置章，不丢内容。
    """
    all_segments: list[Segment] = []
    anchor_positions: dict[str, int] = {}
    resource_starts: dict[str, int] = {}
    resource_by_href: dict[str, dict[str, object]] = {}
    for resource in resources:
        href = str(resource["href"])
        resource_by_href[href] = resource
        resource_starts[href] = len(all_segments)
        raw_segments = resource.get("segments")
        segments = raw_segments if isinstance(raw_segments, list) else []
        for segment in segments:
            if not isinstance(segment, Segment):
                continue
            if segment.anchor:
                anchor_positions[segment.anchor] = len(all_segments)
            all_segments.append(segment)
    for raw_entry in toc_entries:
        entry = raw_entry
        href = entry.get("resource_href")
        if not isinstance(href, str) or href not in resource_starts:
            continue
        fragment = entry.get("fragment")
        has_fragment = isinstance(fragment, str) and bool(fragment)
        resource = resource_by_href[href]
        raw_fragment_map = resource.get("fragment_anchors")
        fragment_map = raw_fragment_map if isinstance(raw_fragment_map, dict) else {}
        if has_fragment and fragment not in fragment_map:
            # 损坏的 fragment 不能悄悄退回到资源开头，否则会在
            # 错误位置切章，并把首个 heading 的译文写给错误目录项。
            continue
        segment_anchor = fragment_map.get(fragment) if has_fragment else None
        if not has_fragment:
            raw_segments = resource.get("segments")
            resource_segments = raw_segments if isinstance(raw_segments, list) else []
            first = next(
                (segment for segment in resource_segments if isinstance(segment, Segment)),
                None,
            )
            segment_anchor = first.anchor if first is not None else None
        if isinstance(segment_anchor, str) and segment_anchor in anchor_positions:
            entry["segment_anchor"] = segment_anchor
            entry["boundary_position"] = anchor_positions[segment_anchor]
        elif has_fragment:
            raw_segments = resource.get("segments")
            segment_count = (
                sum(isinstance(segment, Segment) for segment in raw_segments)
                if isinstance(raw_segments, list)
                else 0
            )
            # fragment 存在但位于最后一个文本块之后。
            entry["boundary_position"] = resource_starts[href] + segment_count
        else:
            # 无文字标题页也是有效目录边界：它会在流中占据当前
            # 位置，后续 spine 正文因此仍能归入该逻辑章。
            entry["boundary_position"] = resource_starts[href]

    # NAV <span> 或宽容 NCX 可以用无 href/content 的节点表示“部”。
    # 这类分组节点继承第一个可定位后代的边界，但不继承
    # segment_anchor，以免把子章 heading 的译文误当成分组标题译文。
    toc_paths = {
        str(entry.get("toc_path"))
        for entry in toc_entries
        if isinstance(entry.get("toc_path"), str) and entry.get("toc_path")
    }
    for toc_path in toc_paths:
        path_entries = [entry for entry in toc_entries if entry.get("toc_path") == toc_path]
        children: dict[int, list[dict[str, object]]] = {}
        for entry in path_entries:
            parent_index = entry.get("parent_index")
            if isinstance(parent_index, int):
                children.setdefault(parent_index, []).append(entry)
        for entry in reversed(path_entries):
            if isinstance(entry.get("boundary_position"), int):
                continue
            if entry.get("raw_href"):
                # 只有无链接的结构分组可以继承子节点；已显式给出
                # 但无法解析的链接属于损坏数据，不应被悄悄改成别的目标。
                continue
            node_index = entry.get("node_index")
            if not isinstance(node_index, int):
                continue
            descendant = next(
                (
                    child
                    for child in children.get(node_index, [])
                    if isinstance(child.get("boundary_position"), int)
                ),
                None,
            )
            if descendant is not None:
                entry["boundary_position"] = descendant["boundary_position"]
                entry["inherited_boundary_from"] = descendant.get("entry_id")

    strategy = get_chapter_split_strategy()
    ordered_toc_paths = list(
        dict.fromkeys(
            str(entry.get("toc_path"))
            for entry in toc_entries
            if isinstance(entry.get("toc_path"), str) and entry.get("toc_path")
        )
    )
    canonical_toc_path = ""
    boundaries: list[dict[str, object]] = []
    for toc_path in ordered_toc_paths:
        candidates = strategy.select(
            [entry for entry in toc_entries if entry.get("toc_path") == toc_path]
        )
        if candidates:
            # EPUB3 NAV 仍由 _parse_opf 排在 NCX 前；仅当较优先目录
            # 完全无法提供章边界时，才退到下一份可用目录。
            canonical_toc_path = toc_path
            boundaries = candidates
            break

    def boundary_position(entry: dict[str, object]) -> int:
        """返回已由切章策略验证过的整数边界位置。"""
        value = entry.get("boundary_position")
        if not isinstance(value, int):
            raise ValueError("EPUB chapter boundary is missing an integer position")
        return value

    boundaries.sort(key=boundary_position)

    if not boundaries:
        chapters: list[Chapter] = []
        for resource in resources:
            raw_segments = resource.get("segments")
            segments = (
                [s for s in raw_segments if isinstance(s, Segment)]
                if isinstance(raw_segments, list)
                else []
            )
            if not segments:
                continue
            for index, segment in enumerate(segments):
                segment.index = index
            chapters.append(
                Chapter(
                    index=len(chapters),
                    title=str(resource.get("title") or ""),
                    segments=segments,
                    href=str(resource.get("href") or "") or None,
                    template=None,
                    meta={"epub_split_strategy": "spine-fallback"},
                )
            )
        return chapters, "spine-fallback", canonical_toc_path

    slices: list[tuple[int, int, dict[str, object] | None]] = []
    first_position = boundary_position(boundaries[0])
    if first_position > 0:
        slices.append((0, first_position, None))
    for index, boundary in enumerate(boundaries):
        start = boundary_position(boundary)
        end = (
            boundary_position(boundaries[index + 1])
            if index + 1 < len(boundaries)
            else len(all_segments)
        )
        if end > start:
            slices.append((start, end, boundary))

    chapters = []
    for start, end, boundary in slices:
        segments = all_segments[start:end]
        for index, segment in enumerate(segments):
            segment.index = index
        if boundary is not None:
            title = str(boundary.get("title") or "")
            toc_entry_id = boundary.get("entry_id")
            first_href = segments[0].resource_href or str(boundary.get("resource_href") or "")
        else:
            first_href = segments[0].resource_href or ""
            title = segments[0].source if segments[0].kind == KIND_HEADING else ""
            toc_entry_id = None
        meta: dict[str, object] = {"epub_split_strategy": strategy.name}
        if isinstance(toc_entry_id, str):
            meta["toc_entry_id"] = toc_entry_id
        chapters.append(
            Chapter(
                index=len(chapters),
                title=title,
                segments=segments,
                href=first_href or None,
                template=None,
                meta=meta,
            )
        )
    return chapters, strategy.name, canonical_toc_path


def read_epub(path: str, source_lang: str, target_lang: str) -> Document:
    """按 spine 读取物理资源，再按顶层目录锚点生成逻辑章节。"""
    with zipfile.ZipFile(path, "r") as zf:
        names = set(zf.namelist())
        opf_path = _find_opf_path(zf)
        book_title, hrefs, toc_paths = _parse_opf(zf, opf_path)
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
                    "fragment_anchors": _fragment_anchor_map(template),
                }
            )
        chapters, split_strategy, split_toc_path = _logical_chapters(resources, toc_entries)
        # XHTML 模板和内联布局都可从原始 EPUB 确定性重建，不写入运行状态。
        # Segment.meta 中其它格式或后续阶段添加的信息仍原样保留。
        for chapter in chapters:
            chapter.template = None
            for segment in chapter.segments:
                segment.meta.pop(_INLINE_META_KEY, None)

    return Document(
        title=book_title or os.path.splitext(os.path.basename(path))[0],
        source_lang=source_lang,
        target_lang=target_lang,
        fmt="epub",
        source_path=os.path.abspath(path),
        chapters=chapters,
        meta={
            "epub_schema": 4,
            "opf_path": opf_path,
            "toc_paths": toc_paths,
            "toc_entries": toc_entries,
            "epub_resources": [
                {"index": resource["index"], "href": resource["href"]} for resource in resources
            ],
            "epub_split_strategy": split_strategy,
            "epub_split_toc_path": split_toc_path,
        },
    )
