"""Rewrite EPUB OPF metadata and exact NAV/NCX titles while preserving hierarchy."""

from __future__ import annotations

from bs4 import BeautifulSoup
from bs4.element import Tag
from bs4.exceptions import ParserRejectedMarkup

from wenyi_core.ingest.epub_toc import nav_root_list, nav_toc_scopes


def _attr_str(value: object) -> str:
    """Safely narrow a BeautifulSoup attribute to a string."""
    return value if isinstance(value, str) else ""


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


def _is_nav(data: bytes) -> bool:
    """Detect EPUB3 TOC navigation in an HTML resource (nav epub:type=toc)."""
    return b"<nav" in data and (
        b'epub:type="toc"' in data
        or b"epub:type='toc'" in data
        or b'type="toc"' in data
        or b'role="doc-toc"' in data
    )
