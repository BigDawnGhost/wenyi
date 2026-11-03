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
TYPE_APPELLATION = "appellation"
TYPE_HONORIFIC = "honorific"
TYPE_SPEECH = "speech"
TYPE_FIXED_EXPR = "fixed_expression"

SOURCE_ONLY_TYPES = frozenset({TYPE_APPELLATION, TYPE_HONORIFIC, TYPE_SPEECH, TYPE_FIXED_EXPR})


def normalize_term_type(value: str) -> str:
    """Normalize known types without discarding custom categories."""
    value = value.strip()
    return value or TYPE_TERM


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


__all__ = [
    "SOURCE_ONLY_TYPES",
    "TYPE_APPELLATION",
    "TYPE_FIXED_EXPR",
    "TYPE_HONORIFIC",
    "TYPE_PERSON",
    "TYPE_SPEECH",
    "TYPE_TERM",
    "GlossaryTerm",
    "normalize_gender",
    "normalize_term_type",
]
