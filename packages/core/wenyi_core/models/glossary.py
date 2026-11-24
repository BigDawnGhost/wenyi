"""The glossary term record and its canonical type tags.

The record lives apart from `glossary.store` because `agents` and `storage` need to name
terms without acquiring SQLite; `store` keeps the persistence side. `from_row` stays with
the record so every caller decodes a database row the same way.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

# Canonical glossary types.
TYPE_PERSON = "person"
TYPE_TERM = "term"
TYPE_NAVIGATION = "navigation"
TYPE_APPELLATION = "appellation"
TYPE_HONORIFIC = "honorific"
TYPE_SPEECH = "speech"
TYPE_FIXED_EXPR = "fixed_expression"

# Canonical glossary statuses.
STATUS_OK = "ok"
STATUS_CONFLICT = "conflict"
STATUS_CONTEXT_DEPENDENT = "context_dependent"
VALID_STATUSES = frozenset({STATUS_OK, STATUS_CONFLICT, STATUS_CONTEXT_DEPENDENT})

SOURCE_ONLY_TYPES = frozenset({TYPE_APPELLATION, TYPE_HONORIFIC, TYPE_SPEECH, TYPE_FIXED_EXPR})


def normalize_term_type(value: str) -> str:
    """Normalize known types without discarding custom categories."""
    value = value.strip()
    return value or TYPE_TERM


def normalize_term_status(value: str) -> str:
    """Normalize glossary entry status; fall back to ``ok`` for unknown values."""
    cleaned = value.strip().lower()
    return cleaned if cleaned in VALID_STATUSES else STATUS_OK


def merge_context_senses(*targets: str) -> str:
    """Combine candidate translations for a context-dependent entry without duplicates."""
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in targets:
        if not isinstance(raw, str):
            continue
        for piece in raw.split("|"):
            cleaned = piece.strip()
            if not cleaned:
                continue
            key = cleaned.casefold()
            if key in seen:
                continue
            seen.add(key)
            ordered.append(cleaned)
    return " | ".join(ordered)


def normalize_gender(value: str) -> str:
    """Use English values and preserve an empty value for unknown gender."""
    value = value.strip()
    return "" if value == "unknown" else value


@dataclass
class GlossaryTerm:
    source: str
    target: str
    reading: str = ""
    type: str = TYPE_TERM
    gender: str = ""
    aliases: list[str] = field(default_factory=list)
    first_chapter: int | None = None
    note: str = ""
    status: str = "ok"

    def __post_init__(self) -> None:
        self.type = normalize_term_type(self.type)
        self.gender = normalize_gender(self.gender)
        self.status = normalize_term_status(self.status)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> GlossaryTerm:
        """Convert a SQLite row into a term and decode its JSON aliases."""
        return cls(
            source=row["source"],
            target=row["target"],
            reading=row["reading"] or "",
            type=row["type"] or TYPE_TERM,
            gender=row["gender"] or "",
            aliases=json.loads(row["aliases"] or "[]"),
            first_chapter=row["first_chapter"],
            note=row["note"] or "",
            status=row["status"] or "ok",
        )


def promote_navigation_term(
    existing: GlossaryTerm,
    incoming: GlossaryTerm,
    chapter: int | None = None,
) -> tuple[str, int | None]:
    """Return ``(type, first_chapter)`` when merging ``incoming`` into ``existing``.

    A real entity term promotes an existing ``TYPE_NAVIGATION`` stub (updating both type
    and ``first_chapter``); existing non-navigation entries never change type or chapter.
    """
    if existing.type == TYPE_NAVIGATION and incoming.type != TYPE_NAVIGATION:
        first_chapter = incoming.first_chapter if incoming.first_chapter is not None else chapter
        return incoming.type, first_chapter
    return existing.type, existing.first_chapter


__all__ = [
    "SOURCE_ONLY_TYPES",
    "STATUS_CONFLICT",
    "STATUS_CONTEXT_DEPENDENT",
    "STATUS_OK",
    "TYPE_APPELLATION",
    "TYPE_FIXED_EXPR",
    "TYPE_HONORIFIC",
    "TYPE_NAVIGATION",
    "TYPE_PERSON",
    "TYPE_SPEECH",
    "TYPE_TERM",
    "VALID_STATUSES",
    "GlossaryTerm",
    "merge_context_senses",
    "normalize_gender",
    "normalize_term_status",
    "normalize_term_type",
    "promote_navigation_term",
]
