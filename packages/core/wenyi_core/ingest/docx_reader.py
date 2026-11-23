"""Read WordprocessingML into the existing chapter/segment state model."""

from __future__ import annotations

import os
import posixpath
import zipfile
from xml.etree import ElementTree

from ..markup import _epub_native
from .models import Document


def read_docx(path: str, source_lang: str, target_lang: str) -> Document:
    """Read DOCX blocks in document order, splitting chapters at level-one headings."""
    try:
        data = _epub_native.parse_docx(
            path,
            os.path.splitext(os.path.basename(path))[0],
            os.path.abspath(path),
            source_lang,
            target_lang,
        )
    except (ValueError, OSError) as error:
        if str(error).startswith("No translatable paragraphs"):
            raise
        raise ValueError(f"Cannot read Word document: {error}") from error
    return Document.model_validate_json(data)


_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_VML_NS = "urn:schemas-microsoft-com:vml"

_FALLBACK_MEDIA_TYPES = {
    "bmp": "image/bmp",
    "emf": "image/x-emf",
    "gif": "image/gif",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "png": "image/png",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "wmf": "image/x-wmf",
}


def read_docx_media(path: str) -> list[tuple[str, str, bytes]]:
    """Return image media referenced by the body as (part, content type, data).

    Parts are deduplicated and ordered by first reference in ``word/document.xml``.
    Raises ValueError when the file is not a readable DOCX package; callers that
    only want best-effort media recovery should catch it.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            rel_targets = _image_relationships(archive)
            if not rel_targets:
                return []
            media_types = _default_content_types(archive)
            body = ElementTree.fromstring(archive.read("word/document.xml"))
            media: list[tuple[str, str, bytes]] = []
            for part in _referenced_image_parts(body, rel_targets):
                try:
                    data = archive.read(part)
                except KeyError:
                    continue
                extension = part.rpartition(".")[2].lower()
                content_type = (
                    media_types.get(extension)
                    or _FALLBACK_MEDIA_TYPES.get(extension)
                    or "application/octet-stream"
                )
                media.append((part, content_type, data))
            return media
    except (OSError, KeyError, ElementTree.ParseError, zipfile.BadZipFile) as error:
        raise ValueError(f"Cannot read Word media from {path}: {error}") from error


def _image_relationships(archive: zipfile.ZipFile) -> dict[str, str]:
    """Map body relationship id to package part for every image relationship."""
    relationships = ElementTree.fromstring(archive.read("word/_rels/document.xml.rels"))
    targets: dict[str, str] = {}
    for node in relationships.iter(f"{{{_REL_NS}}}Relationship"):
        if node.get("TargetMode") == "External":
            continue
        if not (node.get("Type") or "").endswith("/image"):
            continue
        target = node.get("Target") or ""
        if target.startswith("/"):
            part = target.lstrip("/")
        else:
            part = posixpath.normpath(posixpath.join("word", target))
        node_id = node.get("Id")
        if node_id and part:
            targets[node_id] = part
    return targets


def _default_content_types(archive: zipfile.ZipFile) -> dict[str, str]:
    types = ElementTree.fromstring(archive.read("[Content_Types].xml"))
    return {
        (node.get("Extension") or "").lower(): node.get("ContentType") or ""
        for node in types.iter(f"{{{_CONTENT_TYPES_NS}}}Default")
        if node.get("Extension")
    }


def _referenced_image_parts(body: ElementTree.Element, rel_targets: dict[str, str]) -> list[str]:
    """Return image parts in first-reference document order, without duplicates."""
    ordered: list[str] = []
    seen: set[str] = set()
    for node in body.iter():
        if node.tag == f"{{{_DRAWING_NS}}}blip":
            relationship = node.get(f"{{{_OFFICE_REL_NS}}}embed")
        elif node.tag == f"{{{_VML_NS}}}imagedata":
            relationship = node.get(f"{{{_OFFICE_REL_NS}}}id")
        else:
            continue
        part = rel_targets.get(relationship or "")
        if part and part not in seen:
            seen.add(part)
            ordered.append(part)
    return ordered
