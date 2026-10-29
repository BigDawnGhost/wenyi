"""Shared, side-effect-free export option and filename policy."""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException
from wenyi_core.assemble.writer_common import default_output_format, output_backend

from .schemas import ExportRequest


def export_filename(project: dict, target_lang: str, fmt: str, bilingual: bool) -> str:
    original = Path(
        (project.get("source_meta") or {}).get("original_filename") or "translation"
    ).stem
    suffix = "md" if fmt == "markdown" else fmt
    return f"{original}.{target_lang}{'-bi' if bilingual else ''}.{suffix}"


def resolve_export(project: dict, manifest: dict, body: ExportRequest) -> tuple[str, dict]:
    if not project.get("initialized"):
        raise HTTPException(409, "Prepare or translate the source before exporting")
    fmt = body.format or (
        "srt"
        if project["fmt"] == "srt"
        else "docx"
        if project["fmt"] == "docx"
        else default_output_format(manifest)
    )
    if (project["fmt"] == "srt") != (fmt == "srt"):
        raise HTTPException(422, "Subtitle projects export SRT; book projects export book formats")
    try:
        output_backend(manifest, fmt)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return fmt, body.model_dump(exclude={"format"})
