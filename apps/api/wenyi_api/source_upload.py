"""Validate and persist uploaded originals before publishing their database identity."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, UploadFile

from . import paths
from .config import settings

_FORMATS = {
    "epub": "epub",
    "fb2": "fb2",
    "txt": "text",
    "text": "text",
    "md": "markdown",
    "markdown": "markdown",
    "html": "html",
    "htm": "html",
    "pdf": "pdf",
    "docx": "docx",
    "srt": "srt",
}


def input_format(file: UploadFile, fmt: str | None = None) -> str:
    ext = (fmt or Path(file.filename or "").suffix.lstrip(".")).lower()
    if ext not in _FORMATS:
        raise HTTPException(422, "Unsupported input format")
    return _FORMATS[ext]


@dataclass(frozen=True)
class UploadedSource:
    path: Path
    filename: str
    fmt: str
    sha256: str

    def fields(self) -> dict:
        return {
            "source_path": os.path.relpath(self.path, settings.data_dir),
            "book_title": Path(self.filename).stem,
            "source_sha256": self.sha256,
            "fmt": self.fmt,
            "source_meta": {"original_filename": self.filename},
        }


async def save_source(pid: str, file: UploadFile, fmt: str | None = None) -> UploadedSource:
    fmt_code = input_format(file, fmt)
    suffix = {"markdown": "md", "text": "txt"}.get(fmt_code, fmt_code)
    destination = Path(paths.project_dir(pid)) / f"source-{uuid4().hex}.{suffix}"
    digest = hashlib.sha256()
    try:
        with destination.open("xb") as handle:
            while block := await file.read(1024 * 1024):
                digest.update(block)
                handle.write(block)
        if destination.stat().st_size == 0:
            raise HTTPException(422, "Source file is empty")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return UploadedSource(
        destination, Path(file.filename or "source").name, fmt_code, digest.hexdigest()
    )
