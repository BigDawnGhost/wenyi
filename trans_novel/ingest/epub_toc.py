"""Parse EPUB TOCs and resolve links.
NCX/NAV defines logical structure while spine XHTML files are physical resources. One XHTML
may contain several TOC nodes, and a chapter may span several resources. Preserve node
order, hierarchy, raw href and fragment instead of collapsing early into an href-to-title
dictionary that loses same-file subtitles.
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
    """Structured TOC href.
    raw_href preserves the original value. resource_href is the ZIP member resolved relative
    to the TOC file. fragment is the percent-decoded anchor.
    """

    raw_href: str
    resource_href: str
    fragment: str
    external: bool = False

    @property
    def target_key(self) -> str:
        """Return a stable content-destination key, not a unique TOC node ID."""
        if not self.resource_href:
            return ""
        return f"{self.resource_href}#{self.fragment}" if self.fragment else self.resource_href


def resolve_epub_href(base_path: str, raw_href: str) -> ResolvedEpubHref:
    """Resolve an internal EPUB link against base_path without rewriting raw_href.
    Decode percent escapes with urllib.parse.unquote, preserving literal plus signs in
    filenames. Mark URLs with schemes or hosts as external and exclude them from chapter
    splitting and backfill lookup.
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
        # A fragment-only href targets the TOC file itself.
        resource = posixpath.normpath(base_path)
    if resource == ".":
        resource = ""
    return ResolvedEpubHref(raw, resource, fragment, False)


def _local(tag: str) -> str:
    """Remove the XML namespace and return the local tag name."""
    return tag.rsplit("}", 1)[-1]


def _direct_xml_child(element: ET.Element, name: str) -> ET.Element | None:
    """Return the first direct XML child with the requested local name."""
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
    """Construct a JSON-serializable TOC node record."""
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
    """Parse NCX navPoint elements in preorder, reading labels/links only from direct children."""
    root = ET.fromstring(data)
    nav_map = next((node for node in root.iter() if _local(node.tag) == "navMap"), None)
    if nav_map is None:
        return []
    entries: list[dict[str, Any]] = []

    def visit(node: ET.Element, depth: int, parent_index: int | None) -> None:
        """Recursively expand navPoint elements and record parent-child relationships."""
        node_index = len(entries)
        nav_label = _direct_xml_child(node, "navLabel")
        label_node = (
            next(
                (child for child in nav_label.iter() if _local(child.tag) == "text"),
                None,
            )
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
    """Return the first matching direct child of a BeautifulSoup node."""
    found = parent.find(name, recursive=False)
    return found if isinstance(found, Tag) else None


def nav_toc_scopes(soup: BeautifulSoup) -> list[Tag | BeautifulSoup]:
    """Find NAV scope, including older books without explicit epub:type="toc".
    Prefer an explicit EPUB3 TOC nav, then the first nav, then the first ordered list in the
    document. Share this rule between reader and writer so node_index remains identical
    during parsing and backfill.
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
    """Find the root ol within a NAV scope, tolerating descendant lookup if needed."""
    direct = scope.find("ol", recursive=False)
    if isinstance(direct, Tag):
        return direct
    found = scope.find("ol")
    return found if isinstance(found, Tag) else None


def _parse_nav(data: bytes, toc_path: str) -> list[dict[str, Any]]:
    """Parse EPUB3 NAV in ol/li preorder."""
    soup = BeautifulSoup(data, "html.parser")
    entries: list[dict[str, Any]] = []

    def visit_li(li: Tag, depth: int, parent_index: int | None) -> None:
        """Record each li's direct a/span, then recurse into child ol elements."""
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


def parse_toc_entries(zf: zipfile.ZipFile, toc_paths: list[str]) -> list[dict[str, Any]]:
    """Parse existing NCX/NAV files into ordered TOC nodes.
    Handle each TOC independently so a damaged compatibility NCX cannot block a valid
    primary NAV. Besides .ncx extensions, recognize NCX XML roots in .xml files.
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
