"""Bind the policy-adjusted export snapshot to the native Word renderer."""

from __future__ import annotations

import json
import posixpath
import re
import zipfile
from collections.abc import Iterator
from typing import Any

from wenyi_core.atomicio import publish_target
from wenyi_core.ingest.docx_reader import read_docx_media
from wenyi_core.markup import _epub_native
from wenyi_core.markup.contracts import HTML_ATTRIBUTE_META_KEY
from wenyi_core.models.document import KIND_HEADING

from .export_view import AssembleStore
from .policy import export_options
from .writer_common import _ch_title

# Only saved metadata consumed by the version 1 DOCX projection. Do not send
# templates, annotations from other formats, history, or stage metadata.
_SEGMENT_FIELDS = (
    HTML_ATTRIBUTE_META_KEY,
    "heading_level",
    "align",
    "shade",
    "docx_paragraph",
    "docx_tabs",
    "docx_toc_level",
    "docx_source_runs",
    "docx_style",
    "docx_styles",
    "list_num_id",
    "list_ilvl",
    "list_fmt",
    "table_id",
    "row",
    "col",
    "rows",
    "cols",
    "docx_grid_span",
    "docx_row_span",
    "docx_merge_continuation",
)

_IMAGE_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
_EMU_PER_PIXEL = 9525  # 96 dpi
_MAX_IMAGE_WIDTH_EMU = 5486400  # 6 inches, the default portrait text width


def _encode(data: dict[str, Any]) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()


def _chapters(store: AssembleStore, rows: list[dict[str, Any]]) -> Iterator[bytes]:
    for row in rows:
        chapter = store.load_chapter(row["index"])
        yield _encode(
            {
                "title": _ch_title(row),
                "explicit_title": bool(chapter.meta.get("explicit_title")),
                "heading_level": chapter.meta.get("heading_level"),
                "segments": [
                    {
                        "source": seg.source,
                        "target": seg.target,
                        "cont": seg.cont,
                        "heading": seg.kind == KIND_HEADING,
                        "meta": {key: seg.meta[key] for key in _SEGMENT_FIELDS if key in seg.meta},
                    }
                    for seg in chapter.segments
                ],
            }
        )


def _assemble_docx(
    store: AssembleStore,
    out_path: str,
    *,
    bilingual: bool = False,
    order: str = "target_first",
    preserve_source_style: bool = False,
    source_path: str | None = None,
) -> str:
    """Rebuild a DOCX without mutating saved chapters.

    ``source_path`` is reopened read-only only to recover image media that the
    native renderer drops; chapter text always comes from the store snapshot.
    """
    manifest = store.load_manifest()
    meta = manifest.get("meta") or {}
    header = _encode(
        {
            "version": 1,
            "options": {
                "target_font": export_options(store, "docx").target_font,
                "bilingual": bilingual,
                "source_first": order == "source_first",
                "preserve_source_style": preserve_source_style,
            },
            "numbering": meta.get("docx_numbering") or {},
            "core_properties": meta.get("docx_core_properties") or {},
        }
    )
    with publish_target(out_path) as temporary:
        _epub_native.write_docx(temporary, header, _chapters(store, manifest["chapters"]))
        _restore_media(str(temporary), source_path)
    return out_path


def _restore_media(path: str, source_path: str | None) -> None:
    """Copy image media referenced by the source DOCX into the rendered export."""
    if not source_path:
        return
    try:
        media = read_docx_media(source_path)
    except (OSError, ValueError):
        return  # A missing or unreadable source keeps the text-only export.
    if media:
        _write_media(path, media)


def _write_media(path: str, media: list[tuple[str, str, bytes]]) -> None:
    """Rewrite the DOCX package with media parts, relationships and drawings."""
    with zipfile.ZipFile(path) as archive:
        parts = {info.filename: archive.read(info.filename) for info in archive.infolist()}
    content_types = parts["[Content_Types].xml"].decode("utf-8")
    relationships = parts["word/_rels/document.xml.rels"].decode("utf-8")
    document = parts["word/document.xml"].decode("utf-8")
    existing_ids = [int(value) for value in re.findall(r'Id="rId(\d+)"', relationships)]
    next_id = 1 + max(existing_ids or [0])
    rel_nodes: list[str] = []
    drawings: list[str] = []
    for offset, (part, content_type, data) in enumerate(media):
        rel_id = f"rId{next_id + offset}"
        parts.setdefault(part, data)
        rel_nodes.append(
            f'<Relationship Id="{rel_id}" Type="{_IMAGE_REL_TYPE}" '
            f'Target="{posixpath.relpath(part, "word")}"/>'
        )
        drawings.append(_drawing_paragraph(rel_id, part, offset + 1, data))
        extension = part.rpartition(".")[2].lower()
        if extension and f'Extension="{extension}"' not in content_types:
            content_types = content_types.replace(
                "</Types>",
                f'<Default Extension="{extension}" ContentType="{content_type}"/></Types>',
            )
    parts["[Content_Types].xml"] = content_types.encode("utf-8")
    parts["word/_rels/document.xml.rels"] = relationships.replace(
        "</Relationships>", "".join(rel_nodes) + "</Relationships>"
    ).encode("utf-8")
    marker = "<w:sectPr" if "<w:sectPr" in document else "</w:body>"
    parts["word/document.xml"] = document.replace(marker, "".join(drawings) + marker, 1).encode(
        "utf-8",
    )
    with publish_target(path) as staged:
        with zipfile.ZipFile(staged, "w", zipfile.ZIP_DEFLATED) as updated:
            for name, data in parts.items():
                updated.writestr(name, data)


def _drawing_paragraph(relationship: str, part: str, doc_pr_id: int, data: bytes) -> str:
    """Build one centered paragraph embedding the image as an inline drawing."""
    width, height = _image_extent(data)
    name = posixpath.basename(part)
    return (
        '<w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:drawing>'
        '<wp:inline xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"'
        ' xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
        ' xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"'
        ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
        ' distT="0" distB="0" distL="0" distR="0">'
        f'<wp:extent cx="{width}" cy="{height}"/><wp:effectExtent l="0" t="0" r="0" b="0"/>'
        f'<wp:docPr id="{doc_pr_id}" name="Picture {doc_pr_id}"/><wp:cNvGraphicFramePr/>'
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        f'<pic:pic><pic:nvPicPr><pic:cNvPr id="{doc_pr_id}" name="{name}"/>'
        "<pic:cNvPicPr/></pic:nvPicPr>"
        f'<pic:blipFill><a:blip r:embed="{relationship}"/><a:stretch><a:fillRect/></a:stretch>'
        "</pic:blipFill>"
        '<pic:spPr><a:xfrm><a:off x="0" y="0"/>'
        f'<a:ext cx="{width}" cy="{height}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic>'
        "</a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>"
    )


def _image_extent(data: bytes) -> tuple[int, int]:
    """Return the display extent in EMU, scaled down when wider than the text column."""
    pixels = _pixel_size(data) or (480, 360)
    width = max(pixels[0], 1) * _EMU_PER_PIXEL
    height = max(pixels[1], 1) * _EMU_PER_PIXEL
    if width > _MAX_IMAGE_WIDTH_EMU:
        height = max(round(height * _MAX_IMAGE_WIDTH_EMU / width), 1)
        width = _MAX_IMAGE_WIDTH_EMU
    return width, height


def _pixel_size(data: bytes) -> tuple[int, int] | None:
    """Return the (width, height) pixel size of PNG, GIF, BMP or JPEG bytes."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:6] in {b"GIF87a", b"GIF89a"} and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data[:2] == b"BM" and len(data) >= 26:
        return abs(int.from_bytes(data[18:22], "little", signed=True)), abs(
            int.from_bytes(data[22:26], "little", signed=True)
        )
    if data[:2] == b"\xff\xd8":
        return _jpeg_pixel_size(data)
    return None


def _jpeg_pixel_size(data: bytes) -> tuple[int, int] | None:
    """Scan JPEG segments for the first frame header carrying the pixel size."""
    offset = 2
    while offset + 9 <= len(data) and data[offset] == 0xFF:
        marker = data[offset + 1]
        if 0xC0 <= marker <= 0xCF and marker not in {0xC4, 0xC8, 0xCC}:
            return (
                int.from_bytes(data[offset + 7 : offset + 9], "big"),
                int.from_bytes(data[offset + 5 : offset + 7], "big"),
            )
        if marker in {0x01, 0xD8} or 0xD0 <= marker <= 0xD7:
            offset += 2
        else:
            offset += 2 + int.from_bytes(data[offset + 2 : offset + 4], "big")
    return None
