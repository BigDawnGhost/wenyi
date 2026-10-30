"""Shared export ownership and retention policy."""

import logging
from pathlib import Path

EXPORT_LIMIT = 5
log = logging.getLogger(__name__)


def export_path(data_dir: str, pid: str, stored: str) -> Path | None:
    root = Path(data_dir).resolve()
    directory = root / pid / "exports"
    candidate = root / stored
    resolved = candidate.resolve()
    if (
        directory.resolve() != directory
        or resolved != candidate.absolute()
        or not resolved.is_relative_to(directory)
        or resolved == directory
    ):
        return None
    return resolved
