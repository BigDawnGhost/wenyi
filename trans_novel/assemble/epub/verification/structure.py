"""EPUB document structure, navigation, links, and footnote checks."""

from __future__ import annotations

import hashlib
import re
from collections import Counter

from bs4 import BeautifulSoup
from bs4.element import Tag
from lxml import etree

from trans_novel.assemble.epub.verification import archive_model
from trans_novel.epub.navigation import nav_toc_scopes

HTML_MEDIA = archive_model.HTML_MEDIA
NCX_MEDIA = archive_model.NCX_MEDIA
MAX_MEMBER_BYTES = archive_model.MAX_MEMBER_BYTES
EXTERNAL_SCHEMES = {"http", "https", "mailto", "data"}
INTERNAL_ATTRIBUTES = {"data-tn-id", "data-tn-inline-id", "data-tn-line"}
BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "td", "th", "dt", "dd"}
BLOCK_CANDIDATE_TAGS = BLOCK_TAGS | {"div"}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def document_root(soup: BeautifulSoup) -> Tag | None:
    for child in soup.contents:
        if isinstance(child, Tag):
            return child
    return None


def scheme_detail(scheme: str) -> str:
    normalized = scheme.strip().lower()
    if re.fullmatch(r"[a-z][a-z0-9+.-]{0,15}", normalized):
        return normalized
    return "scheme:" + hashlib.sha256(scheme.encode("utf-8", "replace")).hexdigest()[:16]


def html_soup(data: bytes, media: str) -> tuple[BeautifulSoup, bool]:
    """Parse according to media type; only fatal HTML diagnostics invalidate it."""
    if media == "text/html":
        parser = etree.HTMLParser(recover=True)
        valid = True
        try:
            etree.fromstring(data, parser)
            valid = not any(entry.level_name == "FATAL" for entry in parser.error_log)
        except (UnicodeError, etree.XMLSyntaxError, ValueError, TypeError):
            valid = False
        if any(byte == 0 or (byte < 32 and byte not in {9, 10, 13}) for byte in data):
            valid = False
        return BeautifulSoup(data, "html.parser"), valid
    parser = etree.XMLParser(recover=False)
    try:
        etree.fromstring(data, parser)
        valid = True
    except (UnicodeError, etree.XMLSyntaxError, ValueError, TypeError):
        valid = False
    return BeautifulSoup(data, "xml"), valid


def check_nav_semantics(
    soup: BeautifulSoup,
    path: str,
    failures: list[dict[str, str]],
    checked: dict[str, int],
    *,
    allow_typeless: bool = False,
) -> None:
    checked["nav"] += 1
    root = document_root(soup)
    if root is None or root.name != "html":
        failures.append(archive_model.item("nav", "nav_root_invalid", path, "html"))
        return
    candidate_scopes = nav_toc_scopes(soup)
    scopes = [
        nav
        for nav in candidate_scopes
        if isinstance(nav, Tag)
        and nav.name == "nav"
        and "toc" in (str(nav.get("epub:type") or nav.get("type") or "")).split()
    ]
    if not scopes and allow_typeless:
        scopes = [
            nav
            for nav in candidate_scopes
            if isinstance(nav, Tag)
            and nav.name == "nav"
            and not nav.get("epub:type")
            and not nav.get("type")
        ][:1]
    if not scopes:
        failures.append(archive_model.item("nav", "nav_toc_missing", path, "toc"))
        return
    for nav in scopes:
        root_list = nav.find("ol")
        if not isinstance(root_list, Tag):
            failures.append(archive_model.item("nav", "nav_root_list_missing", path, "ol"))
            continue
        items = [
            child for child in root_list.find_all("li", recursive=False) if isinstance(child, Tag)
        ]
        if not items:
            failures.append(archive_model.item("nav", "nav_root_list_empty", path, "li"))
            continue
        has_target = False
        for item in items:
            label = item.find(["a", "span"], recursive=False)
            if not isinstance(label, Tag):
                failures.append(
                    archive_model.item("nav", "nav_item_label_missing", path, "a_or_span")
                )
                continue
            if label.name == "a":
                href = str(label.get("href") or "").strip()
                if not href:
                    failures.append(archive_model.item("nav", "nav_target_missing", path, "href"))
                else:
                    has_target = True
        if not has_target and not root_list.find("a"):
            failures.append(archive_model.item("nav", "nav_target_missing", path, "href"))


def check_ncx_semantics(
    soup: BeautifulSoup,
    path: str,
    failures: list[dict[str, str]],
    checked: dict[str, int],
) -> None:
    checked["nav"] += 1
    root = document_root(soup)
    if root is None or root.name != "ncx":
        failures.append(archive_model.item("nav", "ncx_root_invalid", path, "ncx"))
        return
    nav_map = root.find("navMap", recursive=False)
    if not isinstance(nav_map, Tag):
        failures.append(archive_model.item("nav", "ncx_navmap_missing", path, "navMap"))
        return
    points = [point for point in nav_map.find_all("navPoint") if isinstance(point, Tag)]
    if not points:
        failures.append(archive_model.item("nav", "ncx_navpoint_missing", path, "navPoint"))
        return
    for point in points:
        content = point.find("content", recursive=False)
        if not isinstance(content, Tag) or not str(content.get("src") or "").strip():
            failures.append(archive_model.item("nav", "ncx_content_missing", path, "src"))


def ids(soup: BeautifulSoup) -> set[str]:
    result: set[str] = set()
    for tag in soup.find_all(True):
        for attr in ("id", "name"):
            value = tag.get(attr)
            if isinstance(value, str) and value:
                result.add(value)
    return result


def identifier_map(soup: BeautifulSoup) -> dict[str, Counter[str]]:
    result: dict[str, Counter[str]] = {"id": Counter(), "name": Counter()}
    for tag in soup.find_all(True):
        for attr in result:
            value = tag.get(attr)
            if isinstance(value, str) and value:
                result[attr][value] += 1
    return result


def check_nesting(soup: BeautifulSoup, failures: list[dict[str, str]], path: str) -> None:
    allowed: dict[str, set[str]] = {
        "ul": {"li", "script", "template"},
        "ol": {"li", "script", "template"},
        "table": {
            "caption",
            "col",
            "colgroup",
            "thead",
            "tbody",
            "tfoot",
            "tr",
            "script",
            "template",
        },
        "thead": {"tr", "script", "template"},
        "tbody": {"tr", "script", "template"},
        "tfoot": {"tr", "script", "template"},
        "tr": {"th", "td", "script", "template"},
    }
    for parent_name, names in allowed.items():
        for parent in soup.find_all(parent_name):
            for child in parent.find_all(recursive=False):
                if isinstance(child, Tag) and child.name not in names:
                    failures.append(
                        archive_model.item(
                            "parse", "illegal_nesting", path, f"{parent_name}>{child.name}"
                        )
                    )


def check_document_features(
    soup: BeautifulSoup,
    path: str,
    failures: list[dict[str, str]],
    checked: dict[str, int],
    *,
    content: bool = True,
) -> dict[str, Counter[str]]:
    identifiers = identifier_map(soup)
    for counts in identifiers.values():
        for value, count in sorted(counts.items()):
            checked["anchors"] += 1
            if count > 1:
                failures.append(archive_model.item("anchors", "duplicate_anchor", path, value))
    check_nesting(soup, failures, path)
    for tag in soup.find_all(True):
        checked["placeholders"] += 1
        if any(attribute in tag.attrs for attribute in INTERNAL_ATTRIBUTES):
            failures.append(
                archive_model.item("placeholders", "internal_attribute", path, tag.name)
            )
    if content:
        body = soup.body
        meaningful = bool(body and body.get_text(" ", strip=True))
        if body is not None and not meaningful:
            for tag in body.find_all(["img", "svg", "audio", "video", "object", "embed"]):
                if tag.name == "svg" or any(
                    tag.get(attr) for attr in ("src", "data", "href", "xlink:href")
                ):
                    meaningful = True
                    break
        if body is None or not meaningful:
            failures.append(archive_model.item("parse", "empty_content", path, "empty_body"))
    return identifiers


def external_warning(value: str, scheme: str, category: str = "internal_links") -> dict[str, str]:
    identifier = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16]
    return archive_model.item(category, "external_skipped", "<reference>", f"{scheme}:{identifier}")


def unsupported_scheme_detail(value: str, scheme: str) -> str:
    identifier = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:16]
    return f"{scheme_detail(scheme)}:{identifier}"[:64]


def check_links(
    soup: BeautifulSoup,
    path: str,
    archive: set[str],
    ids_by_path: dict[str, set[str]],
    failures: list[dict[str, str]],
    warnings: list[dict[str, str]],
    checked: dict[str, int],
    *,
    category: str = "internal_links",
) -> Counter[tuple[str, str, str, str, str]]:
    graph: Counter[tuple[str, str, str, str, str]] = Counter()
    for tag in soup.find_all(True):
        for attr in ("href", "src", "xlink:href"):
            value = tag.get(attr)
            if not isinstance(value, str) or not value:
                continue
            checked[category] += 1
            target, fragment, external = archive_model.resolve(path, value)
            if external is not None:
                if external == "unsafe":
                    failures.append(
                        archive_model.item(category, "unsafe_reference", path, "unsafe")
                    )
                elif external in EXTERNAL_SCHEMES:
                    warnings.append(external_warning(value, external, category))
                else:
                    failures.append(
                        archive_model.item(
                            category,
                            "unsupported_scheme",
                            "<reference>",
                            unsupported_scheme_detail(value, external),
                        )
                    )
                continue
            if target is None:
                failures.append(archive_model.item(category, "unsafe_reference", path, "unsafe"))
                continue
            graph[(path, tag.name or "", attr, target, fragment or "")] += 1
            if target not in archive:
                failures.append(archive_model.item(category, "missing_resource", path, target))
            elif fragment is not None and fragment not in ids_by_path.get(target, set()):
                failures.append(archive_model.item("anchors", "missing_fragment", path, "missing"))
    return graph


def check_footnotes(
    soups: dict[str, BeautifulSoup],
    ids_by_path: dict[str, set[str]],
    failures: list[dict[str, str]],
    warnings: list[dict[str, str]],
    checked: dict[str, int],
) -> None:
    """Require a noteref/footnote target and a backlink to its own marker."""
    for path, soup in soups.items():
        for anchor in soup.find_all("a"):
            href = anchor.get("href")
            epub_type = str(anchor.get("epub:type", ""))
            if not isinstance(href, str):
                continue
            href_path = href.partition("#")[0].partition("?")[0]
            if "noteref" not in epub_type and "footnote" not in href_path.lower():
                continue
            checked["footnotes"] += 1
            target, fragment, external = archive_model.resolve(path, href)
            if external is not None:
                if external in EXTERNAL_SCHEMES:
                    warnings.append(
                        archive_model.item("footnotes", "external_skipped", "<reference>", external)
                    )
                else:
                    failures.append(
                        archive_model.item(
                            "footnotes", "unsupported_scheme", path, scheme_detail(external)
                        )
                    )
                continue
            if target is None:
                failures.append(archive_model.item("footnotes", "missing_target", path, "missing"))
                continue
            target_soup = soups.get(target)
            if target_soup is None or (
                fragment is not None and fragment not in ids_by_path.get(target, set())
            ):
                failures.append(archive_model.item("footnotes", "missing_target", path, "missing"))
                continue
            target_tag = (
                target_soup
                if fragment is None
                else target_soup.find(id=fragment) or target_soup.find(attrs={"name": fragment})
            )
            source_id = anchor.get("id") or anchor.get("name")
            if not source_id:
                parent = anchor.parent
                while isinstance(parent, Tag) and parent.name not in {"body", "html"}:
                    candidate = parent.get("id") or parent.get("name")
                    if candidate:
                        source_id = candidate
                        break
                    parent = parent.parent
            backlink = False
            for back in target_tag.find_all("a"):
                back_href = back.get("href")
                if not isinstance(back_href, str):
                    continue
                back_target, back_fragment, back_external = archive_model.resolve(target, back_href)
                if back_external is None and back_target == path and back_fragment == source_id:
                    backlink = True
                    break
            if not backlink:
                failures.append(
                    archive_model.item("footnotes", "missing_backlink", path, "missing")
                )


def graph_from_soups(
    soups: dict[str, BeautifulSoup],
    archive: set[str],
    ids_by_path: dict[str, set[str]],
    failures: list[dict[str, str]],
    warnings: list[dict[str, str]],
    checked: dict[str, int],
    category: str = "internal_links",
) -> Counter[tuple[str, str, str, str, str]]:
    graph: Counter[tuple[str, str, str, str, str]] = Counter()
    for resource, soup in soups.items():
        graph.update(
            check_links(
                soup, resource, archive, ids_by_path, failures, warnings, checked, category=category
            )
        )
    return graph


def inline_hashes(soups: dict[str, BeautifulSoup]) -> dict[str, list[tuple[str, str, str, str]]]:
    result: dict[str, list[tuple[str, str, str, str]]] = {}
    for resource, soup in soups.items():
        entries: list[tuple[str, str, str, str]] = []
        for tag in soup.find_all(["style", "script"]):
            text = tag.get_text("", strip=False)
            attrs = repr(sorted((str(key), str(value)) for key, value in tag.attrs.items()))
            entries.append(
                (
                    tag.name,
                    str(tag.get("id") or ""),
                    hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    hashlib.sha256(attrs.encode("utf-8")).hexdigest(),
                )
            )
        result[resource] = entries
    return result
