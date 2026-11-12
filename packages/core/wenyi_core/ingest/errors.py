"""Deprecated import path for the parsing errors that moved to `wenyi_core.models`.

`pdf_bridge` raises `IngestError` without importing the ingest pipeline, which is the whole
reason the class lives in the models package. New code should import from `wenyi_core.models`.
"""

from __future__ import annotations

from ..models.errors import (  # noqa: F401 (forwarded for existing importers)
    IngestError,
    MinerUError,
    MinerUTimeoutError,
)

__all__ = ["IngestError", "MinerUError", "MinerUTimeoutError"]
