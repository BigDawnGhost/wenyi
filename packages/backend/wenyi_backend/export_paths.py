"""Shared export ownership and retention policy."""

import logging
from pathlib import Path

EXPORT_LIMIT = 5
log = logging.getLogger(__name__)


def export_path(data_dir: str, pid: str, stored: str) -> Path | None:
    # Deliberately unprefixed. The extended-length spelling would let a deep
    # directory resolve, but it also changes the spelling of the returned path,
    # and every caller compares that result against a stored, unprefixed name.
    # Silently returning a differently spelled path would turn a length limit
    # into a wrong answer instead of a visible failure.
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
