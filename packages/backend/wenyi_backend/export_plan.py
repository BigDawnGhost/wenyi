"""Shared, side-effect-free export option and filename policy."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from fastapi import HTTPException
from wenyi_core.assemble.writer_common import default_output_format

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
    options = body.model_dump(exclude={"format"})
    meta = manifest.get("meta") or {}
    is_babeldoc = meta.get("pdf_export") == "babeldoc" or bool(meta.get("babeldoc"))
    if fmt == "pdf" and not is_babeldoc:
        engines = [
            name
            for name, module in (("weasyprint", "weasyprint"), ("fpdf2", "fpdf"))
            if importlib.util.find_spec(module)
        ]
        if not engines:
            raise HTTPException(422, "PDF export requires the optional pdf-export dependencies")
        if "pdf_engine" not in body.model_fields_set:
            options["pdf_engine"] = engines[0]
        elif body.pdf_engine not in engines:
            raise HTTPException(422, f"PDF engine {body.pdf_engine} is not installed")
    return fmt, options
