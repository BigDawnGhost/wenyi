"""Shared, side-effect-free export option and filename policy."""

from __future__ import annotations

from pathlib import Path

from wenyi_core.assemble.writer_common import default_output_format, output_backend

from .errors import INVALID_CONFIGURATION, QUEUE_UNAVAILABLE, ApiError
from .schemas import ExportRequest


def export_filename(project: dict, target_lang: str, fmt: str, bilingual: bool) -> str:
    original = Path(
        (project.get("source_meta") or {}).get("original_filename") or "translation"
    ).stem
    suffix = "md" if fmt == "markdown" else fmt
    return f"{original}.{target_lang}{'-bi' if bilingual else ''}.{suffix}"


def resolve_export(project: dict, manifest: dict, body: ExportRequest) -> tuple[str, dict]:
    if not project.get("initialized"):
        raise ApiError(QUEUE_UNAVAILABLE, 409, "Prepare or translate the source before exporting")
    fmt = body.format or (
        "srt"
        if project["fmt"] == "srt"
        else "docx"
        if project["fmt"] == "docx"
        else default_output_format(manifest)
    )
    if (project["fmt"] == "srt") != (fmt == "srt"):
        raise ApiError(
            INVALID_CONFIGURATION,
            422,
            "Subtitle projects export SRT; book projects export book formats",
        )
    try:
        output_backend(manifest, fmt)
    except ValueError as error:
        # The text comes from the writer; pass it through unchanged so detail is
        # byte-identical to what clients already receive.
        raise ApiError(INVALID_CONFIGURATION, 422, str(error)) from error
    return fmt, body.model_dump(exclude={"format"})
