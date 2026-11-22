"""Content hash that binds persisted state and caches to one source file."""

from __future__ import annotations

import hashlib
from pathlib import Path


def source_sha256(path: str | Path) -> str:
    """Stream source SHA-256 calculation without loading the whole book into memory."""
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
