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
from typing import TypeGuard
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, UnicodeDammit
from bs4.element import Comment, NavigableString, ProcessingInstruction, Tag

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
# 振假名写入 Segment.source：汉字〘假名〙。罕见括号便于提示词要求忽略，
# 且术语匹配时剥离（见 glossary.store._match_text）。模板里仍保留 <ruby>。
_RUBY_MARK_LEFT = "〘"
_RUBY_MARK_RIGHT = "〙"
_RUBY_MARK_RE = re.compile(r"〘[^〙]*〙")
_ANNOTATION_ID_ATTR = "data-tn-annotation-id"
_ANNOTATION_MARKER_ONLY = re.compile(r"^[\d\s*＊※†‡\[\]()〔〕（）{}↩↵←↑↓⤶.·:：\-]+$")
_ANNOTATION_HINT = re.compile(
    r"(?:^|[^a-z0-9])(?:note|noteref|footnote|endnote|fn|jpref|jpnote)"
    r"(?:[-_]?\d+)?(?:$|[^a-z0-9])",
    re.IGNORECASE,
)
_NOTE_TARGET_HINT = re.compile(
    r"(?:^|[^a-z0-9])(?:note|footnote|endnote|fn)(?:[-_]?\d+)?(?:$|[^a-z0-9])",
    re.IGNORECASE,
)
_SHORT_NOTE_IDENTITY = re.compile(r"^n[-_]?\d+$", re.IGNORECASE)
_NOTEREF_SEMANTICS = {"noteref", "doc-noteref"}
_BACKLINK_SEMANTICS = {"backlink", "doc-backlink"}
_NOTE_BODY_SEMANTICS = {"footnote", "endnote", "rearnote", "doc-footnote", "doc-endnote"}
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


def _semantic_tokens(node: Tag) -> set[str]:
    """返回 EPUB/ARIA/HTML 链接语义 token 的小写集合。"""
    tokens: set[str] = set()
    for key in ("epub:type", "role", "rel"):
        value = node.get(key)
        values = value if isinstance(value, list) else [value]
        for raw in values:
            if raw is None:
                continue
            for token in str(raw).split():
                normalized = token.strip().lower()
                if normalized:
                    tokens.add(normalized)
                    # 某些制作工具会写 ``z3998:footnote`` 一类前缀值。
                    tokens.add(normalized.rsplit(":", 1)[-1])
    return tokens


def _inside_note_body(node: Tag) -> bool:
    """判断节点是否位于显式 footnote/endnote 语义容器中。"""
    for parent in (node, *node.parents):
        if isinstance(parent, Tag) and _semantic_tokens(parent) & _NOTE_BODY_SEMANTICS:
            return True
    return False


def _has_note_identity(node: Tag) -> bool:
    """判断节点的 id/name/class 是否明确表示脚注正文。"""
    identity: list[str] = []
    for key in ("id", "name", "class"):
        value = node.get(key)
        if isinstance(value, list):
            identity.extend(str(item) for item in value)
        elif value is not None:
            identity.append(str(value))
    return bool(_NOTE_TARGET_HINT.search(" ".join(identity)))


def _has_short_note_identity(node: Tag) -> bool:
    """判断节点 id/name 是否为仅在强结构证据下采用的 ``n1`` 型注释名。"""
    return any(
        isinstance(node.get(key), str) and bool(_SHORT_NOTE_IDENTITY.fullmatch(str(node.get(key))))
        for key in ("id", "name")
    )


def _implicit_note_body_scope(node: Tag) -> Tag | None:
    """返回以稳定命名标识、但缺少 EPUB 语义的最近注释容器。"""
    for parent in (node, *node.parents):
        if (
            isinstance(parent, Tag)
            and parent.name in {"aside", "li", "dd", "p", "div", "section"}
            and _has_note_identity(parent)
        ):
            return parent
    return None


def _short_note_body_scope(node: Tag) -> Tag | None:
    """返回 ``n1`` 型容器；调用方还须提供角标或回链结构证据。"""
    for parent in (node, *node.parents):
        if (
            isinstance(parent, Tag)
            and parent.name in {"aside", "li", "dd", "p"}
            and _has_short_note_identity(parent)
        ):
            return parent
    return None


def _inside_implicit_note_body(node: Tag) -> bool:
    """判断节点是否位于以稳定命名标识、但缺少 EPUB 语义的注释容器。"""
    if _implicit_note_body_scope(node) is not None:
        return True
    short_scope = _short_note_body_scope(node)
    if short_scope is None or not _ANNOTATION_MARKER_ONLY.fullmatch(node.get_text("", strip=True)):
        return False
    before, after = _surrounding_text(short_scope, node)
    return bool(before.strip()) != bool(after.strip())


def _is_text_string(node: object) -> TypeGuard[NavigableString]:
    """真正的文本节点；XML 处理指令（如 ``<?pagebreak number="69"?>``）不算正文。"""
    return isinstance(node, NavigableString) and not isinstance(
        node, (Comment, ProcessingInstruction)
    )


def _hoist_processing_instructions(el: Tag) -> None:
    """把块内 PI 挪到块前当兄弟，避免 writer ``clear()`` 清掉页码等标记。"""
    if el.parent is None:
        return
    extracted = [
        node.extract() for node in list(el.descendants) if isinstance(node, ProcessingInstruction)
    ]
    for node in extracted:
        el.insert_before(node)


def _surrounding_text(block: Tag, node: Tag) -> tuple[str, str]:
    """返回节点在当前翻译块之前和之后的可见文字，用于识别段首回链。"""

    def text_of(value: object) -> str:
        if _is_text_string(value):
            return str(value)
        if isinstance(value, Tag):
            return value.get_text(" ", strip=False)
        return ""

    before: list[str] = []
    after: list[str] = []
    cursor: Tag = node
    while cursor is not block and isinstance(cursor.parent, Tag):
        before.extend(text_of(sibling) for sibling in cursor.previous_siblings)
        after.extend(text_of(sibling) for sibling in cursor.next_siblings)
        cursor = cursor.parent
    return "".join(before), "".join(after)


def _annotation_relation(
    link: Tag,
    block: Tag,
    *,
    marker_wrapper: Tag | None,
    range_marker: Tag | None,
    marker_only: bool,
) -> str:
    """把需保结构的内部链接区分为正向注释、回链和普通链接。"""
    semantics = _semantic_tokens(link)
    if semantics & _BACKLINK_SEMANTICS:
        return "backlink"
    if semantics & _NOTEREF_SEMANTICS:
        return "noteref"
    if _inside_note_body(link):
        return "backlink"
    if marker_only and _inside_implicit_note_body(link):
        return "backlink"
    if marker_only and re.fullmatch(r"[↩↵←↑↓⤶\s]+", link.get_text("", strip=True)):
        return "backlink"

    # 常见脚注正文以一个裸编号回到正文，随后才是解释文字。它没有 sup/sub
    # 包装，且位于块首；保守地将其视为 backlink，避免把正文反向注入注释。
    if marker_wrapper is None and marker_only and link is not block:
        before, after = _surrounding_text(block, link)
        if not before.strip() and after.strip():
            return "backlink"

    if marker_wrapper is not None or range_marker is not None or marker_only:
        return "noteref"
    return "internal_link"


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


def _has_annotation_hint(
    link: Tag,
    marker: Tag,
    marker_text: str,
    *,
    allow_short_n: bool = False,
) -> bool:
    """根据明确注释线索判断编号是否确为注释，而非普通内部跳转。"""
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
    numbered_note_fragment = bool(
        re.search(
            r"(?:notes?|footnotes?|endnotes?|fn)[\-_]?\d+$",
            parsed.fragment,
            re.IGNORECASE,
        )
    )
    short_n_fragment = allow_short_n and bool(_SHORT_NOTE_IDENTITY.fullmatch(parsed.fragment))
    return (
        decorated
        or bool(_ANNOTATION_HINT.search(hint))
        or numbered_note_fragment
        or short_n_fragment
    )


def _range_marker_node(link: Tag) -> Tag | None:
    """识别范围链接末尾的高置信度注释号，避免误删语义上下标。

    ``H<sub>2</sub>O``、``CO<sub>2</sub>`` 和公式指数都是正文，不能因为
    使用 ``sup/sub`` 就从送译文本中删除。第一版只接受位于链接末尾、文字
    形似编号，并且 href/id/class/语义属性或装饰符提供注释线索的节点。
    """
    significant = [
        child for child in link.children if not (_is_text_string(child) and not str(child).strip())
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
    return (
        candidate
        if _has_annotation_hint(link, candidate, marker_text, allow_short_n=True)
        else None
    )


def _semantic_link_text(link: Tag, marker_node: Tag | None = None) -> str:
    """返回链接正文，只排除已确认的末尾注释号。"""
    parts: list[str] = []

    def collect(parent: Tag) -> None:
        for child in parent.children:
            if isinstance(child, Tag):
                if child is marker_node or child.name in {"rt", "rp"}:
                    continue
                collect(child)
            elif _is_text_string(child):
                parts.append(str(child))

    collect(link)
    return re.sub(r"[ \t\r\n\f\v]+", " ", "".join(parts)).strip()


def _annotation_roots(
    block: Tag,
    anchor: str,
    resource_href: str,
) -> dict[int, dict[str, object]]:
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
            if not _has_annotation_hint(
                link,
                marker_wrapper,
                wrapper_text,
                allow_short_n=True,
            ):
                marker_wrapper = None
        range_marker = None if marker_wrapper is not None else _range_marker_node(link)
        semantic_text = _semantic_link_text(link, range_marker)
        if not semantic_text and marker_wrapper is None:
            # 纯图片链接及空锚点没有需要跨语言定位的正文。让既有原子内联
            # 机制原样保留整个 ``a`` 外壳，避免把图片误当脚注并清空。
            continue
        marker_shaped = bool(_ANNOTATION_MARKER_ONLY.fullmatch(semantic_text))
        marker_only = bool(
            marker_shaped
            and (
                _has_annotation_hint(link, link, semantic_text) or _inside_implicit_note_body(link)
            )
        )
        mode = "point" if marker_wrapper is not None or marker_only else "range"
        root = marker_wrapper if mode == "point" and marker_wrapper is not None else link
        raw_href = str(link.get("href") or "")
        resolved = resolve_epub_href(resource_href, raw_href)
        relation = _annotation_relation(
            link,
            block,
            marker_wrapper=marker_wrapper,
            range_marker=range_marker,
            marker_only=marker_only,
        )

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
            "raw_href": raw_href,
            "target_key": resolved.target_key,
            "relation": relation,
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
                if child.name == "ruby":
                    # 汉字进正文；假名写成 〘…〙 紧随其后，供翻译/审校消歧。
                    base_start = raw_length
                    walk(child, inside_range=inside_range)
                    reading = "".join(rt.get_text() for rt in child.find_all("rt")).strip()
                    if reading and raw_length > base_start:
                        append_text(f"{_RUBY_MARK_LEFT}{reading}{_RUBY_MARK_RIGHT}")
                    continue
                if child.name in {"rt", "rp"}:
                    # 振假名节点本身不进正文（已在 ruby 分支写成 〘…〙）；
                    # 模板仍保留 <rt>/<rp> 供双语导出。
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
            elif _is_text_string(child):
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
                        else _is_text_string(sibling) and bool(str(sibling).strip())
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
                "raw_href": annotation["raw_href"],
                "target_key": annotation["target_key"],
                "relation": annotation["relation"],
            }
        )
    if annotation_items:
        meta[_ANNOTATION_META_KEY] = {
            "version": 1,
            "source_length": source_length,
            "items": annotation_items,
        }
    return text, meta


def strip_ruby_markers(text: str) -> str:
    """去掉正文注音标记 ``〘…〙``；术语匹配与译文误抄兜底共用。"""
    if not text or _RUBY_MARK_LEFT not in text:
        return text
    return _RUBY_MARK_RE.sub("", text)


def _has_meaningful_descendant_block(element: Tag) -> bool:
    """块内若已有更细粒度的正文块，则外层只作为布局容器保留。"""
    return any(
        descendant.get_text(strip=True) for descendant in element.find_all(_BLOCK_CANDIDATE_TAGS)
    )


def _list_item_link_target(element: Tag) -> Tag | None:
    """当直接链接是列表项唯一正文时返回它，避免清空 ``li`` 和子列表。"""
    link = element.find("a", recursive=False)
    if not isinstance(link, Tag) or not link.get_text(strip=True):
        return None
    for child in element.children:
        if child is link:
            continue
        if isinstance(child, Tag):
            # 子列表/子正文块由自己的叶节点负责；它们不属于当前 li 的正文。
            if child.name in _BLOCK_CANDIDATE_TAGS or child.name in {"ul", "ol", "dl"}:
                continue
            if child.get_text(strip=True):
                return None
            continue
        if _is_text_string(child):
            if str(child).strip():
                return None
            continue
        # Comment / ProcessingInstruction 等非正文节点忽略
    return link


def _split_direct_break_lines(element: Tag, soup: BeautifulSoup) -> list[Tag]:
    """把直接 ``br`` 分隔的可见行包装为独立翻译目标，原 ``br`` 不动。"""
    children = list(element.children)
    if not any(isinstance(child, Tag) and child.name == "br" for child in children):
        return [element]

    runs: list[list[Tag | NavigableString]] = [[]]
    for child in children:
        if isinstance(child, Tag) and child.name == "br":
            runs.append([])
        elif isinstance(child, Tag):
            runs[-1].append(child)
        elif _is_text_string(child):
            # PI/Comment 留在 runs 外当兄弟，导出时不被 clear()
            runs[-1].append(child)

    targets: list[Tag] = []
    for run in runs:
        has_text = any(
            node.get_text(strip=True)
            if isinstance(node, Tag)
            else _is_text_string(node) and bool(str(node).strip())
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


def _manifest_xhtml_hrefs(zf: zipfile.ZipFile, opf_path: str) -> list[str]:
    """返回 OPF manifest 中全部 XHTML/HTML 资源，用于解析非 spine 注释。"""
    root = ET.fromstring(zf.read(opf_path))
    hrefs: list[str] = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "item":
            continue
        raw_href = element.attrib.get("href", "").strip()
        media_type = element.attrib.get("media-type", "").strip().lower()
        path = urlsplit(raw_href).path.lower()
        if "html" not in media_type and not path.endswith((".xhtml", ".html", ".htm")):
            continue
        href = _zip_href(opf_path, raw_href)
        if href and href not in hrefs:
            hrefs.append(href)
    return hrefs


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
        annotations = _annotation_roots(el, anchor, href)
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
        if (
            annotations
            and all(annotation.get("mode") == "point" for annotation in annotations.values())
            and _ANNOTATION_MARKER_ONLY.fullmatch(text)
        ):
            continue
        if not text:
            continue
        el["data-tn-id"] = anchor
        # 页码等 PI 挪到块前，回填 clear 目标块时仍保留在模板中。
        _hoist_processing_instructions(el)
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


def _fragment_nodes(soup: BeautifulSoup, fragment: str) -> list[Tag]:
    """返回 fragment 精确命中的唯一 DOM 节点列表，保留重复 ID 供判歧义。"""
    nodes: list[Tag] = []
    seen: set[int] = set()
    for node in soup.find_all(True):
        if node.get("id") != fragment and node.get("name") != fragment:
            continue
        if id(node) not in seen:
            seen.add(id(node))
            nodes.append(node)
    return nodes


def _semantic_note_scope(node: Tag) -> Tag | None:
    """返回包含目标锚点的最近显式 footnote/endnote 语义容器。"""
    for candidate in (node, *node.parents):
        if isinstance(candidate, Tag) and _semantic_tokens(candidate) & _NOTE_BODY_SEMANTICS:
            return candidate
    return None


def _note_context_scope(node: Tag, fragment: str) -> tuple[Tag, bool]:
    """选择注释正文的 DOM 范围，并返回目标是否有明确注释身份。"""
    semantic = _semantic_note_scope(node)
    if semantic is not None:
        return semantic, True

    implicit = _implicit_note_body_scope(node)
    if implicit is not None and (
        implicit.has_attr("data-tn-id") or implicit.find(True, attrs={"data-tn-id": True})
    ):
        return implicit, True

    short_scope = _short_note_body_scope(node)
    if short_scope is not None and (
        short_scope.has_attr("data-tn-id") or short_scope.find(True, attrs={"data-tn-id": True})
    ):
        # ``n1`` 本身不足以升级普通链接，但已确认的角标 noteref 可用它
        # 取得整条列表注释，而不是只取编号锚点。
        return short_scope, False

    # 无语义标注的旧 EPUB 常把一条多段脚注包在 li/dd/aside 中；带明确
    # note/fn 身份的 div/section 也可安全收集其全部正文块。
    has_note_identity = bool(_NOTE_TARGET_HINT.search(fragment)) or _has_note_identity(node)
    if node.name in {"aside", "li", "dd"} or (
        node.name in {"div", "section"} and has_note_identity
    ):
        if node.has_attr("data-tn-id") or node.find(True, attrs={"data-tn-id": True}):
            return node, False

    if node.has_attr("data-tn-id"):
        return node, False
    parent = node.find_parent(attrs={"data-tn-id": True})
    if isinstance(parent, Tag):
        return parent, False
    following = node.find_next(attrs={"data-tn-id": True})
    return (following, False) if isinstance(following, Tag) else (node, False)


def _scope_segment_anchors(scope: Tag) -> list[str]:
    """按 DOM 顺序返回注释范围内的翻译块锚点。"""
    candidates = [scope] if scope.has_attr("data-tn-id") else []
    candidates.extend(scope.find_all(True, attrs={"data-tn-id": True}))
    anchors: list[str] = []
    for candidate in candidates:
        raw_anchor = candidate.get("data-tn-id")
        if isinstance(raw_anchor, str) and raw_anchor and raw_anchor not in anchors:
            anchors.append(raw_anchor)
    return anchors


def _build_epub_annotation_contexts(
    reference_resources: list[dict[str, object]],
    lookup_resources: list[dict[str, object]],
) -> dict[str, object]:
    """解析正向注释引用，建立去重、不可变的源文上下文索引。"""
    lookup_by_href = {
        str(resource.get("href")): resource
        for resource in lookup_resources
        if isinstance(resource.get("href"), str) and resource.get("href")
    }
    soups: dict[str, BeautifulSoup] = {}
    sources_by_href: dict[str, dict[str, str]] = {}
    for href, resource in lookup_by_href.items():
        template = resource.get("template")
        if isinstance(template, str):
            soups[href] = BeautifulSoup(template, "html.parser")
        raw_segments = resource.get("segments")
        segments = raw_segments if isinstance(raw_segments, list) else []
        sources_by_href[href] = {
            segment.anchor: segment.source
            for segment in segments
            if isinstance(segment, Segment) and isinstance(segment.anchor, str) and segment.anchor
        }

    contexts: dict[str, dict[str, object]] = {}
    for resource in reference_resources:
        raw_segments = resource.get("segments")
        segments = raw_segments if isinstance(raw_segments, list) else []
        for segment in segments:
            if not isinstance(segment, Segment):
                continue
            raw_annotations = segment.meta.get(_ANNOTATION_META_KEY)
            annotations = raw_annotations if isinstance(raw_annotations, dict) else {}
            raw_items = annotations.get("items")
            items = raw_items if isinstance(raw_items, list) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                raw_href = item.get("raw_href")
                if not isinstance(raw_href, str):
                    continue
                resolved = resolve_epub_href(segment.resource_href or "", raw_href)
                if (
                    resolved.external
                    or not resolved.resource_href
                    or not resolved.fragment
                    or resolved.target_key != item.get("target_key")
                ):
                    continue
                target_soup = soups.get(resolved.resource_href)
                if target_soup is None:
                    continue
                target_nodes = _fragment_nodes(target_soup, resolved.fragment)
                if len(target_nodes) != 1:
                    # 重复 id/name 的损坏文档无法确定引用所有权，宁可不注入。
                    continue
                scope, semantic_note = _note_context_scope(target_nodes[0], resolved.fragment)
                relation = item.get("relation")
                if relation == "internal_link" and semantic_note:
                    relation = "noteref"
                    item["relation"] = relation
                if relation != "noteref":
                    continue

                anchors = _scope_segment_anchors(scope)
                source_map = sources_by_href.get(resolved.resource_href, {})
                anchors = [anchor for anchor in anchors if anchor in source_map]
                if not anchors:
                    continue
                # 自指链接不提供新信息，也不能作为自己的注释定义。
                if (
                    resolved.resource_href == segment.resource_href
                    and isinstance(segment.anchor, str)
                    and anchors == [segment.anchor]
                ):
                    continue
                source_blocks = [
                    source_map[anchor] for anchor in anchors if source_map[anchor].strip()
                ]
                if not source_blocks:
                    continue
                contexts.setdefault(
                    resolved.target_key,
                    {
                        "target_key": resolved.target_key,
                        "resource_href": resolved.resource_href,
                        "fragment": resolved.fragment,
                        "source_blocks": source_blocks,
                        "segment_anchors": anchors,
                    },
                )
    return {"version": 1, "contexts": contexts}


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


def peek_epub_title(path: str) -> str:
    """只读 OPF 取书名，不逐资源 annotate；供定位已有状态目录使用。

    只解析 container.xml 和 OPF 两个小 XML，不触碰任何 XHTML 正文，因此远比
    ``read_epub`` 便宜。与 ``read_epub`` 计算 ``Document.title`` 的规则保持一致（OPF 缺
    标题时退回文件名词干），否则定位到的状态目录会和 ``prepare`` 时创建的对不上。
    """

    with zipfile.ZipFile(path, "r") as zf:
        opf_path = _find_opf_path(zf)
        book_title, _hrefs, _toc_paths = _parse_opf(zf, opf_path)
    return book_title or os.path.splitext(os.path.basename(path))[0]


def read_epub(path: str, source_lang: str, target_lang: str) -> Document:
    """按 spine 读取物理资源，再按顶层目录锚点生成逻辑章节。"""
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
                    "fragment_anchors": _fragment_anchor_map(template),
                }
            )

        # 注释正文有时只列在 manifest、没有 spine itemref。它不进入正式
        # Chapter/回填资源，但仍可作为引用段的不可变源文辅助上下文。
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
