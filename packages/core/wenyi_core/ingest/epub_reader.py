"""Read EPUBs through Core's native document engine."""

from __future__ import annotations

import os

from wenyi_core.markup import _epub_native
from wenyi_core.models.document import Document


def peek_epub_title(path: str) -> str:
    """Read the package title without opening any XHTML body resources."""
    return _epub_native.peek(path)


def read_epub(
    path: str,
    source_lang: str,
    target_lang: str,
    *,
    epub_schema: int | None = None,
) -> Document:
    """Build a document with stable chapter, segment and annotation identities."""
    return Document.model_validate_json(
        _epub_native.ingest(os.path.abspath(path), source_lang, target_lang, epub_schema)
    )
