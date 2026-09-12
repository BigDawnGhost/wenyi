"""Public EPUB navigation contracts for directory parsing and link resolution.

NCX/NAV 是逻辑目录，spine 中的 XHTML 是物理资源：一个 XHTML
可以包含多个目录节点，一个逻辑章节也可以跨越多个 XHTML。本模块
保留每个目录节点的顺序、层级、原始 href 和 fragment，避免过早压成
``href -> title`` 字典后丢失同文件的子标题。

``select_boundaries`` 是唯一的切章策略：依次试算各候选目录层级对应切片的
字符数中位数，选择达到 ``MIN_CHAPTER_CHARS`` 的最深层级；该模块不设置策略
注册表（YAGNI，本地设计决策）。``select_top_level_boundaries`` 是固定选择
depth == 0 的特例，供既有调用和测试沿用。
"""

from __future__ import annotations

import posixpath
import statistics
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag
from lxml import etree

from trans_novel.epub.archive import read_member


@dataclass(frozen=True)
class ResolvedEpubHref:
    """目录 href 的结构化解析结果。

    ``raw_href`` 仅用于原样保留；``resource_href`` 是已相对目录文件
    解析的 zip 成员路径；``fragment`` 是已百分号解码的锚点。
    """

    raw_href: str
    resource_href: str
    fragment: str
    external: bool = False

    @property
    def target_key(self) -> str:
        """返回内容目标的稳定键；它不是目录节点的唯一 ID。"""
        if not self.resource_href:
            return ""
        return f"{self.resource_href}#{self.fragment}" if self.fragment else self.resource_href


def resolve_epub_href(base_path: str, raw_href: str) -> ResolvedEpubHref:
    """相对 ``base_path`` 解析 EPUB 内部链接，同时不改写原始 href。

    百分号编码使用 :func:`urllib.parse.unquote` 解码，故 ``+`` 仍是文件名
    中的加号，不会被错误当作空格。带 scheme/host 的 URL 被标记为外部
    链接，不参与章节切分或回填定位。
    """
    raw = raw_href or ""
    parsed = urlsplit(raw)
    external = bool(parsed.scheme or parsed.netloc)
    fragment = unquote(parsed.fragment)
    if external:
        return ResolvedEpubHref(raw, "", fragment, True)

    decoded_path = unquote(parsed.path)
    if decoded_path.startswith("/"):
        resource = posixpath.normpath(decoded_path.lstrip("/"))
    elif decoded_path:
        base_dir = posixpath.dirname(base_path)
        resource = posixpath.normpath(posixpath.join(base_dir, decoded_path))
    else:
        # ``#fragment`` 指向目录文件自身。
        resource = posixpath.normpath(base_path)
    if resource == ".":
        resource = ""
    return ResolvedEpubHref(raw, resource, fragment, False)


def _local(tag: str) -> str:
    """去掉 XML 命名空间并返回标签本地名。"""
    return tag.rsplit("}", 1)[-1]


def _direct_xml_child(element: ET.Element, name: str) -> ET.Element | None:
    """返回指定本地名的第一个直接 XML 子元素。"""
    return next((child for child in element if _local(child.tag) == name), None)


def _entry(
    *,
    toc_path: str,
    node_index: int,
    node_id: str,
    parent_index: int | None,
    depth: int,
    kind: str,
    title: str,
    raw_href: str,
) -> dict[str, Any]:
    """构造一个可 JSON 序列化的目录节点记录。"""
    resolved = resolve_epub_href(toc_path, raw_href) if raw_href else None
    resource_href = resolved.resource_href if resolved else ""
    fragment = resolved.fragment if resolved else ""
    return {
        "entry_id": f"{toc_path}:{node_index}",
        "toc_path": toc_path,
        "node_index": node_index,
        "node_id": node_id,
        "parent_index": parent_index,
        "depth": depth,
        "kind": kind,
        "title": title,
        "raw_href": raw_href,
        "resource_href": resource_href,
        "fragment": fragment,
        "target_key": resolved.target_key if resolved else "",
        "external": resolved.external if resolved else False,
    }


def _parse_ncx(data: bytes, toc_path: str) -> list[dict[str, Any]]:
    """按 preorder 解析 NCX navPoint，只从当前节点的直接子元素读取标签和链接。"""
    root = ET.fromstring(data)
    nav_map = next((node for node in root.iter() if _local(node.tag) == "navMap"), None)
    if nav_map is None:
        return []
    entries: list[dict[str, Any]] = []

    def visit(node: ET.Element, depth: int, parent_index: int | None) -> None:
        """递归展开 navPoint，并记录父子关系。"""
        node_index = len(entries)
        nav_label = _direct_xml_child(node, "navLabel")
        label_node = (
            next((child for child in nav_label.iter() if _local(child.tag) == "text"), None)
            if nav_label is not None
            else None
        )
        content = _direct_xml_child(node, "content")
        title = "".join(label_node.itertext()).strip() if label_node is not None else ""
        raw_href = content.attrib.get("src", "") if content is not None else ""
        entries.append(
            _entry(
                toc_path=toc_path,
                node_index=node_index,
                node_id=node.attrib.get("id", ""),
                parent_index=parent_index,
                depth=depth,
                kind="ncx",
                title=title,
                raw_href=raw_href,
            )
        )
        for child in node:
            if _local(child.tag) == "navPoint":
                visit(child, depth + 1, node_index)

    for child in nav_map:
        if _local(child.tag) == "navPoint":
            visit(child, 0, None)
    return entries


def _direct_tag(parent: Tag, name: str) -> Tag | None:
    """返回 BeautifulSoup 节点的第一个指定直接子标签。"""
    found = parent.find(name, recursive=False)
    return found if isinstance(found, Tag) else None


def nav_toc_scopes(soup: BeautifulSoup) -> list[Tag | BeautifulSoup]:
    """返回 NAV 目录搜索范围，兼容缺少 ``epub:type="toc"`` 的旧书。

    标准 EPUB3 优先使用显式 TOC nav；非规范文件则选择第一块 nav，连
    nav 都没有时才在整份文档内寻找首个有序列表。reader 与 writer 共用
    此规则，保证 ``node_index`` 在解析和回填阶段完全一致。
    """
    typed = [
        nav
        for nav in soup.find_all("nav")
        if "toc" in (str(nav.get("epub:type") or nav.get("type") or "")).split()
    ]
    if typed:
        return typed
    untyped = [nav for nav in soup.find_all("nav") if isinstance(nav, Tag)]
    return [untyped[0]] if untyped else [soup]


def nav_root_list(scope: Tag | BeautifulSoup) -> Tag | None:
    """返回 NAV 范围内的根 ``ol``；找不到直接子节点时，再向下查找。"""
    direct = scope.find("ol", recursive=False)
    if isinstance(direct, Tag):
        return direct
    found = scope.find("ol")
    return found if isinstance(found, Tag) else None


def _attr_local(element: etree._Element, name: str) -> str:
    for key, value in element.attrib.items():
        if key.rsplit("}", 1)[-1].split(":", 1)[-1] == name:
            return value
    return ""


def nav_toc_roots_lxml(root: etree._Element) -> list[etree._Element]:
    """Match ``nav_toc_scopes``/``nav_root_list`` in lxml."""
    navs = [
        node
        for node in root.iter()
        if isinstance(node.tag, str) and node.tag.rsplit("}", 1)[-1].lower() == "nav"
    ]
    typed = [node for node in navs if "toc" in _attr_local(node, "type").split()]
    scopes = typed or navs[:1] or [root]
    roots: list[etree._Element] = []
    for scope in scopes:
        direct = next(
            (
                child
                for child in scope
                if isinstance(child.tag, str) and child.tag.rsplit("}", 1)[-1].lower() == "ol"
            ),
            None,
        )
        if direct is None:
            direct = next(
                (
                    node
                    for node in scope.iter()
                    if isinstance(node.tag, str) and node.tag.rsplit("}", 1)[-1].lower() == "ol"
                ),
                None,
            )
        if direct is not None:
            roots.append(direct)
    return roots


def _parse_nav(data: bytes, toc_path: str) -> list[dict[str, Any]]:
    """按 ``ol/li`` preorder 解析 EPUB3 NAV 目录。"""
    soup = BeautifulSoup(data, "html.parser")
    entries: list[dict[str, Any]] = []

    def visit_li(li: Tag, depth: int, parent_index: int | None) -> None:
        """记录 li 的直接 a/span 标签，然后递归其子 ol。"""
        label = _direct_tag(li, "a") or _direct_tag(li, "span")
        current_parent = parent_index
        if label is not None:
            node_index = len(entries)
            raw_href = str(label.get("href") or "") if label.name == "a" else ""
            entries.append(
                _entry(
                    toc_path=toc_path,
                    node_index=node_index,
                    node_id=str(li.get("id") or ""),
                    parent_index=parent_index,
                    depth=depth,
                    kind="nav",
                    title=label.get_text(" ", strip=True),
                    raw_href=raw_href,
                )
            )
            current_parent = node_index
        child_ol = _direct_tag(li, "ol")
        if child_ol is not None:
            for child in child_ol.find_all("li", recursive=False):
                if isinstance(child, Tag):
                    visit_li(child, depth + 1, current_parent)

    for scope in nav_toc_scopes(soup):
        root_ol = nav_root_list(scope)
        if root_ol is None:
            continue
        for li in root_ol.find_all("li", recursive=False):
            if isinstance(li, Tag):
                visit_li(li, 0, None)
    return entries


def parse_toc_entries(zf: zipfile.ZipFile, toc_kinds: dict[str, str]) -> list[dict[str, Any]]:
    """按包模型声明的目录身份解析；单份目录损坏不阻止其他目录。"""
    entries: list[dict[str, Any]] = []
    for toc_path, kind in toc_kinds.items():
        data = read_member(zf, zf.getinfo(toc_path))
        try:
            parsed = _parse_ncx(data, toc_path) if kind == "ncx" else _parse_nav(data, toc_path)
        except (ET.ParseError, ValueError):
            continue
        entries.extend(parsed)
    return entries


def parse_nav_landmarks(zf: zipfile.ZipFile, nav_paths: list[str]) -> list[dict[str, Any]]:
    """提取 EPUB3 landmark 链接语义，但不把它们加入目录切章边界。"""
    landmarks: list[dict[str, Any]] = []
    for nav_path in nav_paths:
        soup = BeautifulSoup(read_member(zf, zf.getinfo(nav_path)), "html.parser")
        for nav in soup.find_all("nav"):
            nav_type = str(nav.get("epub:type") or nav.get("type") or "").strip()
            if "landmarks" not in nav_type.split():
                continue
            for link in nav.find_all("a", href=True):
                raw_href = str(link.get("href") or "")
                resolved = resolve_epub_href(nav_path, raw_href)
                if resolved.external or not resolved.resource_href:
                    continue
                landmarks.append(
                    {
                        "nav_type": nav_type,
                        "type": str(link.get("epub:type") or link.get("type") or "").strip(),
                        "role": str(link.get("role") or "").strip(),
                        "raw_href": raw_href,
                        "resource_href": resolved.resource_href,
                        "fragment": resolved.fragment,
                    }
                )
    return landmarks


MIN_CHAPTER_CHARS = 3000
"""章节切片字符数中位数的下限。对 8 本真实 EPUB 的实测结果显示：
采用“部—章”两级目录的书（Thinking Fast and Slow、Price of Time、
Chip War、Atomic Habits、Wedding People）在 depth 1 时，切片字符数
中位数为 9.6k–31.5k；目录扁平的书（How Not to Get Rich、Psychology
of Money）在 depth 0 时约为 11k；两者均远高于该值。采用三级目录的
中文书《经济运行的逻辑》在 depth 2 时仅为 0.8k，切片明显过碎。因此，
以 3000 个字符作为正常章节与过碎切片的分界。
"""


def _dedupe_boundaries_by_position(
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按 ``boundary_position`` 去重候选边界节点。

    只保留 ``boundary_position`` 为非负 int 的节点。同一位置冲突时
    优先保留带 ``segment_anchor`` 的节点：空标题页与下一个真实章节
    可能对应同一个 Segment 位置，此时优先用带正文锚点的真实章节作为
    边界；若连续空资源均无正文可切分，则采用更靠近后续正文的目录
    节点。
    """
    by_position: dict[int, dict[str, Any]] = {}
    for entry in entries:
        position = entry.get("boundary_position")
        if not isinstance(position, int) or position < 0:
            continue
        previous = by_position.get(position)
        if previous is None:
            by_position[position] = entry
            continue
        previous_is_anchored = bool(previous.get("segment_anchor"))
        current_is_anchored = bool(entry.get("segment_anchor"))
        if (current_is_anchored and not previous_is_anchored) or (
            not current_is_anchored and not previous_is_anchored
        ):
            by_position[position] = entry
    return list(by_position.values())


def select_top_level_boundaries(toc_entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从已定位的目录节点中选出章边界（等价于上游 TopLevelTocStrategy.select）。

    只使用 ``depth == 0`` 的非外部节点，去重规则见
    ``_dedupe_boundaries_by_position``。其行为等同于 ``select_boundaries``
    固定选择 depth == 0。
    """
    candidates = [
        entry for entry in toc_entries if entry.get("depth") == 0 and not entry.get("external")
    ]
    return _dedupe_boundaries_by_position(candidates)


def _slice_median_chars(positions: list[int], segment_lengths: list[int]) -> float:
    """给定一组边界位置，根据累积字符数计算各非空切片的字符数中位数。

    若首个边界之前仍有正文，则隐式补上位置为 0 的边界，并将这部分正文
    计入模拟切片，使中位数能反映实际切章后的章节粒度。
    """
    bounds = sorted(set(positions))
    if not bounds:
        return 0.0
    if bounds[0] > 0:
        bounds = [0, *bounds]
    total = len(segment_lengths)
    prefix = [0] * (total + 1)
    for index, length in enumerate(segment_lengths):
        prefix[index + 1] = prefix[index] + length
    chars = [
        prefix[end] - prefix[start]
        for start, end in zip(bounds, [*bounds[1:], total], strict=False)
        if end > start
    ]
    return statistics.median(chars) if chars else 0.0


def select_boundaries(
    toc_entries: list[dict[str, Any]], segment_lengths: list[int]
) -> tuple[list[dict[str, Any]], int]:
    """按切片粒度自动选择目录层级，返回 (选中的边界节点, 选定的 depth)。

    先收集所有可定位节点中互不重复的 depth，并按升序试算；可定位节点
    须满足 ``boundary_position`` 为非负 int 且 external 为假。对每个
    候选深度 d，将 ``depth <= d`` 的边界去重后模拟切片，选择切片字符数
    中位数达到 ``MIN_CHAPTER_CHARS`` 的最深层级。若所有层级均未达标，
    则退回最浅层级，与历史上固定使用 depth == 0 的行为一致。
    """
    locatable_depths = sorted(
        {
            entry.get("depth")
            for entry in toc_entries
            if isinstance(entry.get("depth"), int)
            and not entry.get("external")
            and isinstance(entry.get("boundary_position"), int)
            and entry.get("boundary_position") >= 0
        }
    )
    if not locatable_depths:
        return [], 0

    def boundaries_at_depth(depth: int) -> list[dict[str, Any]]:
        candidates = [
            entry
            for entry in toc_entries
            if isinstance(entry.get("depth"), int)
            and entry["depth"] <= depth
            and not entry.get("external")
        ]
        return _dedupe_boundaries_by_position(candidates)

    selected_depth = locatable_depths[0]
    for depth in locatable_depths:
        boundaries = boundaries_at_depth(depth)
        positions = [int(entry["boundary_position"]) for entry in boundaries]
        median_chars = _slice_median_chars(positions, segment_lengths)
        if median_chars >= MIN_CHAPTER_CHARS:
            selected_depth = depth

    return boundaries_at_depth(selected_depth), selected_depth


__all__ = [
    "MIN_CHAPTER_CHARS",
    "ResolvedEpubHref",
    "nav_root_list",
    "nav_toc_roots_lxml",
    "nav_toc_scopes",
    "parse_nav_landmarks",
    "parse_toc_entries",
    "resolve_epub_href",
    "select_boundaries",
    "select_top_level_boundaries",
]
