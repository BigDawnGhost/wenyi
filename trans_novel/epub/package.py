"""共享 EPUB 包解析：保留声明证据，并统一资源身份与阅读顺序。"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from typing import Any
from urllib.parse import unquote

from trans_novel.epub.archive import ZipSafetyError, read_member, safe_name
from trans_novel.epub.navigation import resolve_epub_href

HTML_MEDIA = {"application/xhtml+xml", "text/html"}
NCX_MEDIA = "application/x-dtbncx+xml"
CONTAINER_PATH = "META-INF/container.xml"


def _local_name(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _manifest_path(opf_path: str, href: str) -> str | None:
    if not href or "\x00" in href or re.search(r"%(?![0-9A-Fa-f]{2})", href):
        return None
    if unquote(href).startswith("/"):
        return None
    try:
        reference = resolve_epub_href(opf_path, href)
    except ValueError:
        return None
    if reference.external or not safe_name(reference.resource_href):
        return None
    return reference.resource_href


def _content_model(root: ET.Element | None, opf_path: str, archive: set[str]) -> dict[str, Any]:
    elements = list(root.iter()) if root is not None else []
    items: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    resolved: list[dict[str, Any]] = []
    for element in elements:
        if _local_name(element.tag) != "item":
            continue
        attrs = dict(element.attrib)
        href = attrs.get("href", "").strip()
        media = attrs.get("media-type", "").strip()
        target = _manifest_path(opf_path, href)
        item = {
            "id": attrs.get("id", "").strip(),
            "href": href,
            "media": media,
            "properties": attrs.get("properties", "").strip(),
            "fallback": attrs.get("fallback", "").strip(),
            "media_overlay": attrs.get("media-overlay", "").strip(),
            "attrs": attrs,
            "path": target,
        }
        items.append(item)
        if item["id"] and item["id"] not in by_id:
            by_id[item["id"]] = item
        if target in archive:
            effective_media = media
            if not media and target.lower().endswith((".xhtml", ".html", ".htm")):
                effective_media = "application/xhtml+xml"
            resolved.append({**item, "media": effective_media})
    spine = next((e for e in elements if _local_name(e.tag) == "spine"), None)
    spine_ids = (
        [e.attrib.get("idref", "").strip() for e in spine if _local_name(e.tag) == "itemref"]
        if spine is not None
        else []
    )
    spine_toc = spine.attrib.get("toc", "").strip() if spine is not None else ""
    nav_items = [item for item in resolved if "nav" in item["properties"].split()]
    ncx_items = [item for item in resolved if item["media"] == NCX_MEDIA]
    path_by_id = {item["id"]: item["path"] for item in resolved if item["id"]}
    ordered_ncx = [item for item in ncx_items if item["id"] == spine_toc] + ncx_items
    toc_kinds = {
        item["path"]: "ncx" if item["media"] == NCX_MEDIA else "nav"
        for item in nav_items + ordered_ncx
    }
    guide_entries = []
    guide_references = (
        element
        for guide in elements
        if _local_name(guide.tag) == "guide"
        for element in guide.iter()
        if _local_name(element.tag) == "reference"
    )
    for element in guide_references:
        raw_href = element.attrib.get("href", "").strip()
        resolved_href = _manifest_path(opf_path, raw_href)
        if resolved_href is None:
            continue
        guide_entries.append(
            {
                "type": element.attrib.get("type", "").strip(),
                "title": element.attrib.get("title", "").strip(),
                "raw_href": raw_href,
                "resource_href": resolved_href,
            }
        )
    return {
        "title": next(
            (e.text.strip() for e in elements if _local_name(e.tag) == "title" and e.text), ""
        ),
        "items": items,
        "resolved": resolved,
        "by_id": by_id,
        "spine_ids": spine_ids,
        "spine_paths": [path_by_id[item_id] for item_id in spine_ids if item_id in path_by_id],
        "spine_toc": spine_toc,
        "nav_items": nav_items,
        "ncx_items": ncx_items,
        "content_paths": list(
            dict.fromkeys(item["path"] for item in resolved if item["media"] in HTML_MEDIA)
        ),
        "toc_paths": list(toc_kinds),
        "toc_kinds": toc_kinds,
        "guide_entries": guide_entries,
    }


def _read_xml(
    zf: zipfile.ZipFile, name: str, failures: list[dict[str, str]], invalid_code: str
) -> ET.Element | None:
    try:
        return ET.fromstring(read_member(zf, zf.getinfo(name)))
    except (KeyError, ZipSafetyError) as exc:
        code = exc.code if isinstance(exc, ZipSafetyError) else "member_read"
        failures.append({"category": "zip", "code": code, "path": name, "detail": "member"})
    except (ET.ParseError, ValueError, TypeError):
        failures.append(
            {"category": "parse", "code": invalid_code, "path": name, "detail": "malformed XML"}
        )
    return None


def read_package(
    zf: zipfile.ZipFile, failures: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    """有界读取一次 container/OPF；结构错误保留为诊断，不吞掉原始声明。"""
    failures = failures if failures is not None else []
    archive = {info.filename for info in zf.infolist()}
    opf_path = ""
    opf = None
    if CONTAINER_PATH not in archive:
        failures.append(
            {
                "category": "resources",
                "code": "missing_container",
                "path": CONTAINER_PATH,
                "detail": "container.xml is required",
            }
        )
    else:
        container = _read_xml(zf, CONTAINER_PATH, failures, "invalid_container")
        if container is not None:
            roots = [
                e.attrib.get("full-path", "").strip()
                for e in container.iter()
                if _local_name(e.tag) == "rootfile"
            ]
            if len(roots) != 1 or not safe_name(roots[0]) or roots[0] not in archive:
                failures.append(
                    {
                        "category": "resources",
                        "code": "invalid_rootfile",
                        "path": CONTAINER_PATH,
                        "detail": "rootfile unresolved",
                    }
                )
            else:
                opf_path = roots[0]
                opf = _read_xml(zf, opf_path, failures, "invalid_opf")
    return {
        "archive": archive,
        "opf_path": opf_path,
        "valid": opf is not None,
        "model": _content_model(opf, opf_path, archive),
    }
