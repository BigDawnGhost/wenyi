"""Deprecated import path for the document models that moved to `wenyi_core.models`.

The models are pure data, so `markup`, `storage` and `assemble` can name them without
importing the ingest pipeline. New code should import from `wenyi_core.models`.
"""

from __future__ import annotations

from ..models.document import (  # noqa: F401 (forwarded for existing importers)
    KIND_HEADING,
    KIND_TEXT,
    Chapter,
    Document,
    Segment,
)

__all__ = ["KIND_HEADING", "KIND_TEXT", "Chapter", "Document", "Segment"]
