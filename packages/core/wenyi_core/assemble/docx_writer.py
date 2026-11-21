"""Bind the policy-adjusted export snapshot to the native Word renderer."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from wenyi_core.atomicio import publish_target
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
                        "target": "" if seg.meta.get("skip") else seg.target,
                        "cont": seg.cont,
                        "heading": seg.kind == KIND_HEADING,
                        "meta": {key: seg.meta[key] for key in _SEGMENT_FIELDS if key in seg.meta},
                    }
                    for seg in chapter.segments
                    if not (seg.meta.get("skip") and "table_id" not in seg.meta)
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
) -> str:
    """Rebuild a DOCX without mutating saved chapters or reopening the source archive."""
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
    return out_path
