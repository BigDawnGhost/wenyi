"""Bounded EPUB archive and package model primitives."""

from __future__ import annotations

import hashlib
import lzma
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from trans_novel.epub.archive import (
    MAX_ARCHIVE_BYTES as ARCHIVE_MAX_BYTES,
)
from trans_novel.epub.archive import (
    MAX_ARCHIVE_MEMBERS as ARCHIVE_MAX_MEMBERS,
)
from trans_novel.epub.archive import (
    MAX_MEMBER_BYTES as MEMBER_MAX_BYTES,
)
from trans_novel.epub.archive import (
    ZipSafetyError,
    preflight_zip,
    safe_name,
)
from trans_novel.epub.archive import (
    read_member as bounded_read_member,
)

SCHEMA_VERSION = 1
MAX_MEMBER_BYTES = MEMBER_MAX_BYTES
MAX_ARCHIVE_BYTES = ARCHIVE_MAX_BYTES
MAX_ARCHIVE_MEMBERS = ARCHIVE_MAX_MEMBERS
HTML_MEDIA = {"application/xhtml+xml", "text/html"}
NCX_MEDIA = "application/x-dtbncx+xml"

_EXTERNAL_SCHEMES = {"http", "https", "mailto", "data"}
HTML_MEDIA = {"application/xhtml+xml", "text/html"}
NCX_MEDIA = "application/x-dtbncx+xml"
_BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "td", "th", "dt", "dd"}
_BLOCK_CANDIDATE_TAGS = _BLOCK_TAGS | {"div"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


class MemberError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def archive_label(path: str) -> str:
    """Return a stable archive-relative label, never an OS path."""
    if not path or "\x00" in path or "\\" in path or path.startswith("/"):
        return "<opaque:" + hashlib.sha256(path.encode("utf-8", "replace")).hexdigest()[:16] + ">"
    normalized = posixpath.normpath(path)
    if normalized == ".." or normalized.startswith("../"):
        return "<opaque:" + hashlib.sha256(path.encode("utf-8", "replace")).hexdigest()[:16] + ">"
    return normalized


def item(category: str, code: str, path: str, detail: str) -> dict[str, str]:
    return {"category": category, "code": code, "path": archive_label(path), "detail": detail}


def sort_items(items: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(
        items, key=lambda item: tuple(item[key] for key in ("category", "code", "path", "detail"))
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return ""
    return digest.hexdigest()


def read_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, *, as_bytes: bool = True) -> bytes:
    """Read a member through the shared bounded CRC/decompression guard."""
    try:
        data = bounded_read_member(zf, info, max_member_bytes=MAX_MEMBER_BYTES)
    except ZipSafetyError as exc:
        raise MemberError(exc.code) from None
    return data if as_bytes else b""


def member_hash(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    try:
        if info.file_size > MAX_MEMBER_BYTES:
            raise MemberError("member_too_large")
        with zf.open(info, "r") as stream:
            while True:
                chunk = stream.read(min(1024 * 1024, MAX_MEMBER_BYTES - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_MEMBER_BYTES:
                    raise MemberError("member_too_large")
                digest.update(chunk)
    except MemberError:
        raise
    except (
        OSError,
        RuntimeError,
        NotImplementedError,
        EOFError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        ValueError,
        zlib.error,
        lzma.LZMAError,
    ) as exc:
        raise MemberError(
            "crc_error" if type(exc).__name__ in {"BadZipFile", "BadCRC"} else "member_read"
        ) from None
    if total != info.file_size:
        raise MemberError("member_size_mismatch")
    return digest.hexdigest(), total


def parse_xml(data: bytes) -> ET.Element | None:
    try:
        return ET.fromstring(data)
    except (ET.ParseError, ValueError, TypeError):
        return None


def local_name(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def safe_archive_name(name: str) -> bool:
    return safe_name(name)


def resolve(base: str, reference: str) -> tuple[str | None, str | None, str | None]:
    """Return (archive path, decoded fragment, scheme/error marker)."""
    raw = reference.strip()
    if "\x00" in raw or re.search(r"%(?![0-9A-Fa-f]{2})", raw):
        return None, None, "unsafe"
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None, None, "unsafe"
    scheme = parsed.scheme.lower()
    if scheme in _EXTERNAL_SCHEMES:
        return None, unquote(parsed.fragment) if parsed.fragment else None, scheme
    if scheme or parsed.netloc:
        return None, None, scheme or "unsupported"
    decoded_path = unquote(parsed.path)
    joined = (
        base
        if not decoded_path
        else posixpath.normpath(posixpath.join(posixpath.dirname(base), decoded_path))
    )
    if not safe_archive_name(joined):
        return None, None, "unsafe"
    fragment = unquote(parsed.fragment) if parsed.fragment else None
    return joined, fragment, None


def manifest_href(opf_path: str, href: str) -> str | None:
    target, _, external = resolve(opf_path, href)
    return target if external is None else None


def manifest_items(root: ET.Element) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for element in root.iter():
        if local_name(element.tag) != "item":
            continue
        attrs = dict(element.attrib)
        result.append(
            {
                "id": attrs.get("id", "").strip(),
                "href": attrs.get("href", "").strip(),
                "media": attrs.get("media-type", "").strip(),
                "properties": attrs.get("properties", "").strip(),
                "fallback": attrs.get("fallback", "").strip(),
                "media_overlay": attrs.get("media-overlay", "").strip(),
                "attrs": attrs,
            }
        )
    return result


def content_model(root: ET.Element, opf_path: str, archive: set[str]) -> dict[str, Any]:
    items = manifest_items(root)
    by_id: dict[str, dict[str, Any]] = {}
    for item in items:
        if item["id"] and item["id"] not in by_id:
            by_id[item["id"]] = item
    spine_element = next((e for e in root.iter() if local_name(e.tag) == "spine"), None)
    spine_ids = [
        e.attrib.get("idref", "").strip()
        for e in root.iter()
        if local_name(e.tag) == "itemref" and e.attrib.get("idref", "").strip()
    ]
    resolved: list[dict[str, Any]] = []
    for item in items:
        target = manifest_href(opf_path, item["href"])
        if target in archive:
            resolved.append({**item, "path": target})
    nav_items = [item for item in resolved if "nav" in item["properties"].split()]
    ncx_items = [item for item in resolved if item["media"] == NCX_MEDIA]
    path_by_id = {item["id"]: item["path"] for item in resolved if item["id"]}
    spine_paths = [path_by_id[item_id] for item_id in spine_ids if item_id in path_by_id]
    return {
        "items": items,
        "resolved": resolved,
        "by_id": by_id,
        "spine_ids": spine_ids,
        "spine_paths": spine_paths,
        "spine_toc": spine_element.attrib.get("toc", "").strip()
        if spine_element is not None
        else "",
        "nav_items": nav_items,
        "ncx_items": ncx_items,
    }


def resource_hashes(
    zf: zipfile.ZipFile,
    model: dict[str, Any],
    opf_path: str,
    failures: list[dict[str, str]],
    checked: dict[str, int],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    resolved = model.get("resolved")
    if not isinstance(resolved, list):
        nested = model.get("model")
        resolved = nested.get("resolved", []) if isinstance(nested, dict) else []
    for entry in resolved:
        if (
            entry["path"] == opf_path
            or entry["media"] in HTML_MEDIA
            or entry["media"] == NCX_MEDIA
            or "nav" in entry["properties"].split()
        ):
            continue
        checked["assets"] += 1
        try:
            info = zf.getinfo(entry["path"])
            digest, size = member_hash(zf, info)
            result[entry["path"]] = {"sha256": digest, "size": size, "media": entry["media"]}
        except (KeyError, MemberError) as exc:
            code = exc.code if isinstance(exc, MemberError) else "member_read"
            failures.append(item("zip", code, entry["path"], "member"))
    return result


def model_read(zf: zipfile.ZipFile, name: str, failures: list[dict[str, str]]) -> bytes | None:
    try:
        return read_member(zf, zf.getinfo(name))
    except (KeyError, MemberError) as exc:
        code = exc.code if isinstance(exc, MemberError) else "member_read"
        failures.append(item("zip", code, name, "member"))
        return None


def archive_model(
    zf: zipfile.ZipFile, failures: list[dict[str, str]] | None = None
) -> dict[str, Any]:
    failures = failures if failures is not None else []
    names = [info.filename for info in zf.infolist()]
    archive = set(names)
    container_data = (
        model_read(zf, "META-INF/container.xml", failures)
        if "META-INF/container.xml" in archive
        else None
    )
    container = parse_xml(container_data) if container_data is not None else None
    roots = (
        [
            e.attrib.get("full-path", "").strip()
            for e in container.iter()
            if local_name(e.tag) == "rootfile"
        ]
        if container is not None
        else []
    )
    opf_path = roots[0] if roots else ""
    opf_data = model_read(zf, opf_path, failures) if opf_path in archive else None
    opf = parse_xml(opf_data) if opf_data is not None else None
    if opf is None:
        return {
            "archive": archive,
            "opf_path": opf_path,
            "model": {
                "items": [],
                "resolved": [],
                "spine_paths": [],
                "spine_ids": [],
                "spine_toc": "",
                "nav_items": [],
                "ncx_items": [],
            },
            "graphs": Counter(),
            "ids": set(),
        }
    model = content_model(opf, opf_path, archive)
    return {"archive": archive, "opf_path": opf_path, "model": model}


def open_validated_zip(
    path: Path,
    failures: list[dict[str, str]],
    checked: dict[str, int],
    *,
    max_member_bytes: int = MAX_MEMBER_BYTES,
    max_archive_bytes: int = MAX_ARCHIVE_BYTES,
):
    if not path.is_file():
        failures.append(item("zip", "missing_file", "<input>", "EPUB path does not exist"))
        checked["zip"] += 1
        return None, [], set()
    try:
        zf = zipfile.ZipFile(path, "r")
    except (OSError, zipfile.BadZipFile):
        failures.append(item("zip", "invalid_zip", "<input>", "invalid_zip"))
        checked["zip"] += 1
        return None, [], set()
    try:
        infos = preflight_zip(
            zf,
            max_member_bytes=max_member_bytes,
            max_archive_bytes=max_archive_bytes,
            max_archive_members=MAX_ARCHIVE_MEMBERS,
        )
    except ZipSafetyError as exc:
        failures.append(item("zip", exc.code, exc.name or "<archive>", "archive"))
        zf.close()
        return None, [], set()
    names = [info.filename for info in infos]
    archive_names = set(names)
    checked["zip"] += len(infos) + 1
    if not infos or infos[0].filename != "mimetype":
        failures.append(item("zip", "mimetype_not_first", "mimetype", "mimetype must be first"))
    counts = Counter(names)
    for name, count in sorted(counts.items()):
        if count > 1:
            failures.append(
                item(
                    "zip",
                    "mimetype_duplicate" if name == "mimetype" else "duplicate_entry",
                    name,
                    "duplicate entry",
                )
            )
    for info in infos:
        if not safe_archive_name(info.filename):
            failures.append(
                item("zip", "unsafe_entry", info.filename, "archive entry escapes root")
            )
        if info.flag_bits & 0x1:
            failures.append(item("zip", "encrypted_entry", info.filename, "encrypted"))
    if sum(info.file_size for info in infos) > max_archive_bytes:
        failures.append(
            item("zip", "archive_too_large", "<archive>", "archive declared size exceeds limit")
        )
        zf.close()
        return None, [], set()
    if any(info.file_size > max_member_bytes for info in infos):
        failures.append(item("zip", "member_too_large", "<archive>", "member exceeds limit"))
        zf.close()
        return None, [], set()
    if counts.get("mimetype") != 1:
        failures.append(item("zip", "mimetype_duplicate", "mimetype", "exactly one mimetype"))
    elif "mimetype" in archive_names:
        info = next(i for i in infos if i.filename == "mimetype")
        if info.compress_type != zipfile.ZIP_STORED:
            failures.append(
                item("zip", "mimetype_compressed", "mimetype", "mimetype must be stored")
            )
        try:
            if read_member(zf, info) != b"application/epub+zip":
                failures.append(
                    item("zip", "mimetype_invalid", "mimetype", "invalid mimetype bytes")
                )
        except MemberError as exc:
            failures.append(item("zip", exc.code, "mimetype", "member"))
    for info in infos:
        try:
            read_member(zf, info, as_bytes=False)
        except MemberError as exc:
            failures.append(item("zip", exc.code, info.filename, "member"))
    if any(not safe_archive_name(name) for name in names) or any(
        info.flag_bits & 0x1 for info in infos
    ):
        zf.close()
        return None, [], set()
    return zf, infos, archive_names
