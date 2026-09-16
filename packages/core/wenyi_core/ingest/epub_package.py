"""Read EPUB container, OPF manifest and spine without processing body markup."""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from urllib.parse import urlsplit

from bs4 import UnicodeDammit

from wenyi_core.markup.anchors import resolve_epub_href

_CONTAINER = "META-INF/container.xml"


def _find_opf_path(zf: zipfile.ZipFile) -> str:
    """Resolve the EPUB package document ZIP path from container.xml."""
    data = zf.read(_CONTAINER)
    root = ET.fromstring(data)
    # Match local names because container.xml uses a default namespace.
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == "rootfile":
            path = el.attrib.get("full-path", "").strip()
            if path:
                return path
    raise ValueError("Invalid EPUB: container.xml has no valid rootfile full-path")


def _zip_href(base_path: str, href: str) -> str:
    """Resolve an EPUB-relative href to a normalized zip member path."""
    return resolve_epub_href(base_path, href).resource_href


def _parse_opf(zf: zipfile.ZipFile, opf_path: str) -> tuple[str, list[str], list[str]]:
    """Return the book title, spine-ordered XHTML ZIP paths and TOC/NAV paths."""
    root = ET.fromstring(zf.read(opf_path))

    def local(tag: str) -> str:
        """Remove the XML namespace and return the local tag name."""
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
            # The spine may reference one physical resource repeatedly, but the ZIP contains only one XHTML.
            # Annotate it once to avoid a second set of anchors that cannot be backfilled.
            hrefs.append(resolved_href)

    # Prefer EPUB3 NAV as the main TOC, then the EPUB2 NCX named by spine.toc. Retain other TOCs
    # for title backfill without mixing their chapter boundaries with those of the main TOC.
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
    """List all OPF XHTML/HTML resources so annotations outside the spine can be parsed."""
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
    """Decode XHTML using declarations and byte signatures; use UTF-8 replacement only as a
    last resort.
    """
    decoded = UnicodeDammit(data).unicode_markup
    return decoded if decoded is not None else data.decode("utf-8", errors="replace")
