"""Read WordprocessingML into the existing chapter/segment state model."""

from __future__ import annotations

import os

from ..markup import _epub_native
from .models import Document


def read_docx(path: str, source_lang: str, target_lang: str) -> Document:
    """Read DOCX blocks in document order, splitting chapters at level-one headings."""
    try:
        data = _epub_native.parse_docx(
            path,
            os.path.splitext(os.path.basename(path))[0],
            os.path.abspath(path),
            source_lang,
            target_lang,
        )
    except (ValueError, OSError) as error:
        if str(error).startswith("No translatable paragraphs"):
            raise
        raise ValueError(f"Cannot read Word document: {error}") from error
    return Document.model_validate_json(data)
