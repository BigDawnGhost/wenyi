"""Validate and persist uploaded originals before publishing their database identity."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, UploadFile

from . import paths
from .context import current_context
from .errors import SOURCE_TOO_LARGE, ApiError

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
}

# Upload ceiling. Large enough for any realistic novel with artwork, small enough that
# one bad upload cannot exhaust the DATA_DIR volume (audit C-2).
MAX_SOURCE_BYTES = 512 * 1024 * 1024


def format_for_suffix(suffix: str) -> str | None:
    """Resolve an input extension without opening or parsing its source."""
    return _FORMATS.get(suffix.lstrip(".").lower())


def input_format(file: UploadFile, fmt: str | None = None) -> str:
    ext = (fmt or Path(file.filename or "").suffix.lstrip(".")).lower()
    result = format_for_suffix(ext)
    if result is None:
        raise HTTPException(422, "Unsupported input format")
    return result


@dataclass(frozen=True)
class UploadedSource:
    path: Path
    filename: str
    fmt: str
    sha256: str

    def fields(self) -> dict:
        source_paths = current_context().source_paths
        return {
            "source_path": (
                source_paths.source_reference(self.path)
                if source_paths is not None
                else os.path.relpath(self.path, paths.data_dir())
            ),
            "book_title": Path(self.filename).stem,
            "source_sha256": self.sha256,
            "fmt": self.fmt,
            "source_meta": {"original_filename": self.filename},
        }


async def save_source(
    pid: str,
    file: UploadFile,
    fmt: str | None = None,
    *,
    directory: Path | str | None = None,
) -> UploadedSource:
    """Write one upload into the project directory it will belong to.

    `directory` is the project directory the caller has already resolved. A host
    that lets a person choose where a project lives knows that answer before the
    project exists, and the upload has to go into the same directory the record
    will name -- resolving it twice would let a lookup answer with a different
    directory than the one the project is then catalogued under.

    It defaults to the context projection, which is what every caller without a
    location choice uses.
    """
    size = getattr(file, "size", None)
    if isinstance(size, int) and not isinstance(size, bool) and size > MAX_SOURCE_BYTES:
        raise ApiError(SOURCE_TOO_LARGE, 413, "Source file exceeds the upload size limit")
    fmt_code = input_format(file, fmt)
    suffix = {"markdown": "md", "text": "txt"}.get(fmt_code, fmt_code)
    root = Path(directory) if directory is not None else Path(paths.project_dir(pid))
    destination = root / f"source-{uuid4().hex}.{suffix}"
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    written = 0
    try:
        with destination.open("xb") as handle:
            while block := await file.read(1024 * 1024):
                written += len(block)
                if written > MAX_SOURCE_BYTES:
                    # Chunked uploads may not carry a trustworthy Content-Length.
                    raise ApiError(
                        SOURCE_TOO_LARGE, 413, "Source file exceeds the upload size limit"
                    )
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
