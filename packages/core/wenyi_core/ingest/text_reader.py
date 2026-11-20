"""Plain-text and Markdown reader.
Recognize Markdown ATX headings first, then common Japanese chapter/prologue/epilogue
labels. Without headings, use one chapter. Blank lines separate paragraphs while single
internal newlines remain. Reconstruct output as headings and blank-line-separated
paragraphs.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..markup import _epub_native
from .encoding import decode_source
from .models import KIND_HEADING, KIND_TEXT, Chapter, Document, Segment


def read_text(path: str, source_lang: str, target_lang: str) -> Document:
    """Read text or Markdown in UTF-8 or a detected legacy encoding, then build a Document."""
    content, _encoding = decode_source(Path(path).read_bytes())
    book_title = os.path.splitext(os.path.basename(path))[0]

    chapters: list[Chapter] = []
    for ci, (heading, paragraphs) in enumerate(_epub_native.parse_text(content)):
        title, level = heading or (book_title, 1)
        segments: list[Segment] = []
        # Keep titles as heading segments so they can be translated and backfilled.
        if heading:
            segments.append(Segment(index=0, source=title, kind=KIND_HEADING))
        segments.extend(
            Segment(index=idx, source=para, kind=KIND_TEXT)
            for idx, para in enumerate(paragraphs, start=len(segments))
        )
        chapters.append(
            Chapter(
                index=ci,
                title=title,
                segments=segments,
                meta={"heading_level": level},
            )
        )

    return Document(
        title=book_title,
        source_lang=source_lang,
        target_lang=target_lang,
        fmt="text",
        source_path=os.path.abspath(path),
        chapters=chapters,
    )
