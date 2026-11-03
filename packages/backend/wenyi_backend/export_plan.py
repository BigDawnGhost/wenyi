"""Shared, side-effect-free export option and filename policy."""

from __future__ import annotations

from pathlib import Path

from wenyi_core.assemble.writer_common import default_output_format, output_backend
from wenyi_core.storage.names import safe_component

from .errors import INVALID_CONFIGURATION, SOURCE_NOT_READY, ApiError
from .schemas import ExportRequest


def export_stem(filename: str) -> str:
    """Reduce a source filename to a stem Windows accepts as a filename.

    `Path.stem` strips only the last suffix, so "book.tar.gz" keeps its inner
    suffix while "book." loses the dot; both leave a name whose on-disk spelling
    differs from the one checked here. The reserved-name escape and the trailing
    dot/space strip run afterwards, and the fallback covers a name that reduces
    to nothing, which would otherwise become a hidden ".dotfile" export.
    """
    stem = Path(filename).stem
    while Path(stem).suffix:
        stem = Path(stem).stem
    return safe_component(stem, fallback="translation")


def export_filename(project: dict, target_lang: str, fmt: str, bilingual: bool) -> str:
    original = export_stem((project.get("source_meta") or {}).get("original_filename") or "")
    suffix = "md" if fmt == "markdown" else fmt
    return f"{original}.{target_lang}{'-bi' if bilingual else ''}.{suffix}"


def resolve_export(project: dict, manifest: dict, body: ExportRequest) -> tuple[str, dict]:
    if not project.get("initialized"):
        raise ApiError(SOURCE_NOT_READY, 409, "Prepare or translate the source before exporting")
    fmt = body.format or ("docx" if project["fmt"] == "docx" else default_output_format(manifest))
    try:
        output_backend(manifest, fmt)
    except ValueError as error:
        # The text comes from the writer; pass it through unchanged so detail is
        # byte-identical to what clients already receive.
        raise ApiError(INVALID_CONFIGURATION, 422, str(error)) from error
    return fmt, body.model_dump(exclude={"format"})
