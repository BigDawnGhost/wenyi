"""EPUB 目录解析与链接定位。

NCX/NAV 是逻辑目录，spine 中的 XHTML 是物理资源：一个 XHTML
可以包含多个目录节点，一个目录章也可以跨越多个 XHTML。本模块
保留每个目录节点的顺序、层级、原始 href 和 fragment，避免过早压成
``href -> title`` 字典后丢失同文件的子标题。
"""

from __future__ import annotations

import posixpath
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag


@dataclass(frozen=True)
class ResolvedEpubHref:
    """一个目录 href 的结构化结果。

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
        return (
            f"{self.resource_href}#{self.fragment}"
            if self.fragment
            else self.resource_href
        )


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
    """按 preorder 解析 NCX navPoint，只在当前节点直接子树取标签/链接。"""
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
        if "toc"
        in (str(nav.get("epub:type") or nav.get("type") or "")).split()
    ]
    if typed:
        return typed
    untyped = [nav for nav in soup.find_all("nav") if isinstance(nav, Tag)]
    return [untyped[0]] if untyped else [soup]


def nav_root_list(scope: Tag | BeautifulSoup) -> Tag | None:
    """返回一个 NAV 范围内的根 ``ol``，必要时宽容查找后代节点。"""
    direct = scope.find("ol", recursive=False)
    if isinstance(direct, Tag):
        return direct
    found = scope.find("ol")
    return found if isinstance(found, Tag) else None


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


def parse_toc_entries(
    zf: zipfile.ZipFile, toc_paths: list[str]
) -> list[dict[str, Any]]:
    """解析所有已存在的 NCX/NAV 文件，返回有序目录节点。

    每份目录独立容错：损坏的兼容 NCX 不应阻断有效的主 NAV。除常见
    ``.ncx`` 后缀外，也会根据 XML 根节点识别媒体类型为 NCX 的 ``.xml``。
    """
    names = set(zf.namelist())
    entries: list[dict[str, Any]] = []
    for toc_path in toc_paths:
        if toc_path not in names:
            continue
        data = zf.read(toc_path)
        is_ncx = toc_path.lower().endswith(".ncx")
        if not is_ncx:
            try:
                root = ET.fromstring(data)
                is_ncx = _local(root.tag).lower() == "ncx" or any(
                    _local(node.tag) == "navMap" for node in root.iter()
                )
            except ET.ParseError:
                is_ncx = False
        try:
            parsed = _parse_ncx(data, toc_path) if is_ncx else _parse_nav(data, toc_path)
        except (ET.ParseError, ValueError):
            continue
        entries.extend(parsed)
    return entries
