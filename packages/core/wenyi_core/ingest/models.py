"""Core Document, Chapter and Segment models.
Segment is the smallest alignable/backfillable translation unit, usually a paragraph or
heading. Send batches of segments to the model and require an equal-length translation array
to detect alignment errors and prevent missing paragraphs.
Use Pydantic v2 for validation, serialization and deep copies.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# Segment kinds.
KIND_TEXT = "text"
KIND_HEADING = "heading"


class Segment(BaseModel):
    """One translatable unit."""

    index: int  # Zero-based index within the chapter.
    source: str  # Source text.
    kind: str = KIND_TEXT  # text | heading
    target: str | None = None  # Target text, populated after translation/polishing.
    target_before_polish: str | None = (
        None  # Translation before polishing, or None when polishing is disabled.
    )
    anchor: str | None = None  # Backfill anchor; EPUB uses a placeholder ID.
    resource_href: str | None = None  # EPUB physical XHTML resource containing this segment.
    cont: bool = False  # Continuation of a split long paragraph; merge back during export instead of creating a paragraph.
    meta: dict[str, Any] = Field(default_factory=dict)


class Chapter(BaseModel):
    """A chapter: ordered segments and structural information for backfill."""

    index: int  # Zero-based chapter index within the book.
    title: str = ""
    segments: list[Segment] = Field(default_factory=list)
    href: str | None = None  # Physical resource where the chapter begins.
    template: str | None = None  # HTML/PDF backfill template with placeholders.
    meta: dict[str, Any] = Field(default_factory=dict)

    @property
    def text_segments(self) -> list[Segment]:
        """Nonempty segments that need translation."""
        return [s for s in self.segments if s.source.strip()]


class Document(BaseModel):
    """A complete book."""

    title: str = ""
    source_lang: str
    target_lang: str
    fmt: str  # epub | text
    source_path: str = ""
    chapters: list[Chapter] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
