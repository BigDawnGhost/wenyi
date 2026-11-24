"""SQLite glossary with two tables.
glossary stores one mapping per source. When another target is proposed, retain the current
translation and log the alternative in term_conflicts for human review. term_conflicts
records unresolved translation conflicts.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
import unicodedata
from collections.abc import Iterable
from functools import lru_cache
from typing import Any

from ..models.glossary import (
    SOURCE_ONLY_TYPES,
    STATUS_CONFLICT,
    STATUS_CONTEXT_DEPENDENT,
    STATUS_OK,
    TYPE_APPELLATION,
    TYPE_FIXED_EXPR,
    TYPE_HONORIFIC,
    TYPE_PERSON,
    TYPE_SPEECH,
    TYPE_TERM,
    GlossaryTerm,
    merge_context_senses,
    normalize_term_status,
)

# The term record and its type tags moved to wenyi_core.models.glossary. They stay
# importable from here so existing callers keep working without taking SQLite along.
_SOURCE_ONLY_TYPES = SOURCE_ONLY_TYPES

__all__ = [
    "STATUS_CONFLICT",
    "STATUS_CONTEXT_DEPENDENT",
    "STATUS_OK",
    "TYPE_APPELLATION",
    "TYPE_FIXED_EXPR",
    "TYPE_HONORIFIC",
    "TYPE_PERSON",
    "TYPE_SPEECH",
    "TYPE_TERM",
    "GlossaryOccurrenceMatcher",
    "GlossaryStore",
    "GlossaryTerm",
    "equivalent_targets",
    "merge_context_senses",
    "normalize_match_text",
    "search_term_page",
    "source_matches_normalized",
    "source_matches_text",
    "source_occurrence_spans",
    "term_match_sources",
]

_CREATE_GLOSSARY_TABLE = """
CREATE TABLE IF NOT EXISTS glossary (
    source        TEXT PRIMARY KEY,
    target        TEXT NOT NULL,
    reading       TEXT,
    type          TEXT,
    gender        TEXT,
    aliases       TEXT,
    first_chapter INTEGER,
    note          TEXT,
    status        TEXT DEFAULT 'ok',
    updated_at    REAL
)
"""

_SCHEMA = (
    _CREATE_GLOSSARY_TABLE
    + ";"
    + """
CREATE TABLE IF NOT EXISTS term_conflicts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    existing_target TEXT,
    proposed_target TEXT,
    chapter         INTEGER,
    note            TEXT,
    resolved        INTEGER DEFAULT 0,
    created_at      REAL
);
"""
)


# Match the reader's furigana markers in Segment.source; strip them before substring matching.
_RUBY_MARK_RE = re.compile(r"〘[^〙]*〙")


def normalize_match_text(text: str) -> str:
    """Normalize width, compatibility forms and case for glossary matching.
    Strip embedded furigana so a reading inserted inside a word does not prevent that word
    from matching. The original ruby markup remains in the source template.
    """
    if "〘" in text:
        text = _RUBY_MARK_RE.sub("", text)
    return unicodedata.normalize("NFKC", text).casefold()


_match_text = normalize_match_text

_LEADING_ARTICLE = re.compile(r"^the\s+")
_TRAILING_PUNCTUATION = re.compile(r"[\s.!?。！？]+$")


def equivalent_targets(left: str, right: str) -> bool:
    """Treat targets differing only by case, spacing, a leading English "the" or a final
    stop as the same translation, so automatic extraction does not log them as conflicts.
    Only "the" is stripped: "A" and "An" also romanize Chinese names (阿紫, 安禄山).
    """
    if left == right:
        return True

    def key(value: str) -> str:
        normalized = " ".join(normalize_match_text(value).split())
        return _TRAILING_PUNCTUATION.sub("", _LEADING_ARTICLE.sub("", normalized))

    return bool(key(left)) and key(left) == key(right)


_WORD_BOUNDARY_SCRIPTS = ("LATIN", "GREEK", "CYRILLIC")


def search_term_page(
    terms: Iterable[GlossaryTerm], query: str, *, offset: int, limit: int
) -> tuple[list[GlossaryTerm], int]:
    """Count Unicode casefold substring matches while retaining only one display page."""
    needle = query.strip().casefold()
    items: list[GlossaryTerm] = []
    total = 0
    for term in terms:
        if not any(
            needle in value.casefold()
            for value in (term.source, term.target, term.reading, term.note, *term.aliases)
        ):
            continue
        if offset <= total < offset + limit:
            items.append(term)
        total += 1
    return items, total


@lru_cache(maxsize=1 << 14)
def _source_pattern(key: str) -> re.Pattern[str] | None:
    """Build word boundaries for space-delimited scripts; return None for substring-matched
    scripts. Keyed only on bounded glossary source strings (never paragraph text).
    """
    if key.isascii():
        return re.compile(rf"(?<![a-z0-9_]){re.escape(key)}(?![a-z0-9_])")

    letters = [char for char in key if char.isalpha()]
    if not letters or not all(
        any(script in unicodedata.name(char, "") for script in _WORD_BOUNDARY_SCRIPTS)
        for char in letters
    ):
        return None

    left_boundary = r"(?<!\w)" if key[0].isalnum() else ""
    right_boundary = r"(?!\w)" if key[-1].isalnum() else ""
    return re.compile(f"{left_boundary}{re.escape(key)}{right_boundary}")


@lru_cache(maxsize=1 << 14)
def _compiled_source_key(source: str) -> tuple[str, re.Pattern[str] | None]:
    """Return the normalized match key for ``source`` and its boundary pattern, if any.
    Glossary sources recur across every segment and batch, so cache the normalization and
    script detection instead of repeating them for each lookup.
    """
    key = _match_text(source).strip()
    return key, (_source_pattern(key) if key else None)


def raw_source_pattern(source: str) -> re.Pattern[str]:
    """Match ``source`` verbatim in unnormalized text, on word boundaries where its script
    is space-delimited, so Ann never matches inside Anna; CJK sources match as substrings.
    """
    key = source.strip()
    escaped = re.escape(key)
    if not key or _source_pattern(_match_text(key)) is None:
        return re.compile(escaped)
    left_boundary = r"(?<!\w)" if key[0].isalnum() else ""
    right_boundary = r"(?!\w)" if key[-1].isalnum() else ""
    return re.compile(f"{left_boundary}{escaped}{right_boundary}")


def source_matches_text(source: str, text: str) -> bool:
    """Match source terms without matching inside longer words in space-delimited scripts.
    Use normalized substring matching for continuous scripts such as CJK. Check boundaries
    for ASCII, Latin, Greek and Cyrillic to avoid matching Ann inside Anna or a short
    Cyrillic word inside a longer one.
    """
    key, pattern = _compiled_source_key(source)
    if not key:
        return False
    normalized_text = _match_text(text)
    if pattern is not None:
        return pattern.search(normalized_text) is not None
    return key in normalized_text


def source_matches_normalized(source: str, normalized_text: str) -> bool:
    """Match like ``source_matches_text`` against text already passed through
    ``normalize_match_text``, so callers testing many terms normalize the text only once.
    """
    key, pattern = _compiled_source_key(source)
    if not key:
        return False
    if pattern is not None:
        return pattern.search(normalized_text) is not None
    return key in normalized_text


def term_match_sources(term: GlossaryTerm) -> list[str]:
    """Return source spellings allowed when matching a term.
    Match appellations, honorifics, speech habits and fixed expressions only by their
    complete source. A bare-name alias must not inject a context-specific derived
    translation into an ordinary address. Other entities can also match aliases.
    """
    if term.type in _SOURCE_ONLY_TYPES:
        return [term.source]
    return [term.source, *term.aliases]


def trusted_match_sources(term: GlossaryTerm, all_terms: list[GlossaryTerm]) -> list[str]:
    """For note citations, use aliases only when no other glossary identity claims them."""
    canonical = _match_text(term.source).strip()
    claimed = {
        _match_text(key).strip()
        for other in all_terms
        if _match_text(other.source).strip() != canonical
        for key in [other.source, *other.aliases]
    }
    return [
        key
        for key in dict.fromkeys(term_match_sources(term))
        if key == term.source or (term.status == "ok" and _match_text(key).strip() not in claimed)
    ]


def source_occurrence_spans(source: str, normalized_text: str) -> list[tuple[int, int]]:
    """Return nonoverlapping source-term spans in normalized text."""
    key, pattern = _compiled_source_key(source)
    if not key:
        return []
    if pattern is not None:
        return [match.span() for match in pattern.finditer(normalized_text)]

    spans: list[tuple[int, int]] = []
    start = 0
    while (index := normalized_text.find(key, start)) != -1:
        end = index + len(key)
        spans.append((index, end))
        start = end
    return spans


_source_occurrence_spans = source_occurrence_spans


def _merged_occurrence_count(spans: set[tuple[int, int]]) -> int:
    """Merge overlapping source/alias matches at one location into a single mention."""
    count = 0
    active_end = -1
    for start, end in sorted(spans):
        if start >= active_end:
            count += 1
            active_end = end
        else:
            active_end = max(active_end, end)
    return count


class GlossaryOccurrenceMatcher:
    """Reuse one normalized corpus to detect repeated terms by source and aliases."""

    def __init__(self, text: str):
        self.normalized_text = _match_text(text)

    def recurring_terms(
        self,
        terms: list[GlossaryTerm],
        *,
        min_occurrences: int = 2,
    ) -> list[GlossaryTerm]:
        """Select terms whose source/aliases occur at least the requested number of times."""
        if min_occurrences <= 1:
            return GlossaryStore.terms_in(terms, self.normalized_text)

        recurring: list[GlossaryTerm] = []
        for term in terms:
            raw_keys = term_match_sources(term)
            keys = {normalized for key in raw_keys if (normalized := _match_text(key).strip())}
            spans: set[tuple[int, int]] = set()
            for key in keys:
                spans.update(_source_occurrence_spans(key, self.normalized_text))
            if _merged_occurrence_count(spans) >= min_occurrences:
                recurring.append(term)
        return recurring


class GlossaryStore:
    def __init__(self, db_path: str):
        """Open the glossary database and initialize the current schema."""
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        # Wait for concurrent writes so editors and translation workers avoid database-is-locked errors.
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()
        self._owns_connection = True

    @classmethod
    def from_connection(cls, conn: sqlite3.Connection) -> GlossaryStore:
        """Borrow an initialized connection; the caller owns transactions and its lifetime."""
        store = cls.__new__(cls)
        store.conn = conn
        store._owns_connection = False
        return store

    @staticmethod
    def initialize_schema(conn: sqlite3.Connection) -> None:
        """Create glossary tables without committing the caller's transaction."""
        for statement in _SCHEMA.split(";"):
            if statement.strip():
                conn.execute(statement)

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        if self._owns_connection:
            self.conn.close()

    @classmethod
    def load_terms_readonly(cls, db_path: str) -> list[GlossaryTerm]:
        """Read terms from a temporary snapshot without touching the formal database or its WAL
        files.
        immutable=1 avoids creating shm but ignores committed, uncheckpointed WAL data. Copy
        the database and existing WAL to a temporary directory and let SQLite recover the
        complete view there. Any resulting shm files or checkpoints remain outside formal
        book state.
        """
        with tempfile.TemporaryDirectory(prefix="wenyi-glossary-review-") as directory:
            snapshot_path = f"{directory}/glossary.db"
            wal_path = f"{db_path}-wal"
            snapshot_wal_path = f"{snapshot_path}-wal"

            def signature(path: str) -> tuple[int, int, int, int] | None:
                """Return a lightweight signature sufficient to detect changes during copying."""
                try:
                    stat = os.stat(path)
                except FileNotFoundError:
                    return None
                return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns

            # DB and WAL are separate files; sequential copying is not an atomic snapshot. Accept only
            # matching before/after signatures; discard and retry if a checkpoint or write intervenes.
            for _attempt in range(5):
                before = signature(db_path), signature(wal_path)
                try:
                    shutil.copy2(db_path, snapshot_path)
                    if before[1] is not None:
                        shutil.copy2(wal_path, snapshot_wal_path)
                    elif os.path.exists(snapshot_wal_path):
                        os.unlink(snapshot_wal_path)
                except FileNotFoundError:
                    continue
                after = signature(db_path), signature(wal_path)
                if before == after:
                    break
            else:
                raise RuntimeError(
                    "The glossary kept changing during snapshot capture; retry review later."
                )

            conn = sqlite3.connect(snapshot_path)
            conn.row_factory = sqlite3.Row
            try:
                # Return insertion order (rowid), not type/source sorting: inserting a new term in the middle
                # would shift the remaining prompt glossary and invalidate prefix caches.
                rows = conn.execute("SELECT * FROM glossary ORDER BY rowid").fetchall()
                return [GlossaryTerm.from_row(row) for row in rows]
            finally:
                conn.close()

    # Glossary terms.
    def get_term(self, source: str) -> GlossaryTerm | None:
        """Look up the exact source term; return None if absent."""
        row = self.conn.execute("SELECT * FROM glossary WHERE source = ?", (source,)).fetchone()
        return GlossaryTerm.from_row(row) if row else None

    def upsert_term(self, term: GlossaryTerm, chapter: int | None = None) -> str:
        """Insert or update a term; return inserted, unchanged or conflict.
        For an existing source with a different target, retain the established translation
        and record the candidate. Automatic extraction must not replace confirmed mappings
        without human resolution.
        """
        try:
            # Acquire the lock before reading existing so two connections cannot decide from the same old view.
            if self._owns_connection:
                self.conn.execute("BEGIN IMMEDIATE")
            existing = self.get_term(term.source)
            now = time.time()
            if existing is None:
                self.conn.execute(
                    """INSERT INTO glossary
                       (source,target,reading,type,gender,aliases,first_chapter,note,
                        status,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        term.source,
                        term.target,
                        term.reading,
                        term.type,
                        term.gender,
                        json.dumps(term.aliases, ensure_ascii=False),
                        term.first_chapter if term.first_chapter is not None else chapter,
                        term.note,
                        term.status,
                        now,
                    ),
                )
                result = "inserted"
            elif existing.status == STATUS_CONTEXT_DEPENDENT:
                # Context-dependent terms accept multiple senses without raising conflicts.
                merged_target = merge_context_senses(existing.target, term.target)
                merged_aliases = sorted(set(existing.aliases) | set(term.aliases))
                self.conn.execute(
                    """UPDATE glossary SET target=?, reading=COALESCE(NULLIF(?,''),reading),
                       gender=COALESCE(NULLIF(?,''),gender), aliases=?,
                       note=COALESCE(NULLIF(?,''),note), updated_at=? WHERE source=?""",
                    (
                        merged_target or existing.target,
                        term.reading,
                        term.gender,
                        json.dumps(merged_aliases, ensure_ascii=False),
                        term.note,
                        now,
                        term.source,
                    ),
                )
                result = "unchanged"
            elif existing.target == term.target:
                # Merging aliases or filling missing fields is not a conflict.
                merged_aliases = sorted(set(existing.aliases) | set(term.aliases))
                self.conn.execute(
                    """UPDATE glossary SET reading=COALESCE(NULLIF(?,''),reading),
                       gender=COALESCE(NULLIF(?,''),gender), aliases=?,
                       note=COALESCE(NULLIF(?,''),note), updated_at=? WHERE source=?""",
                    (
                        term.reading,
                        term.gender,
                        json.dumps(merged_aliases, ensure_ascii=False),
                        term.note,
                        now,
                        term.source,
                    ),
                )
                result = "unchanged"
            else:
                # Different target: keep the established mapping and record the candidate for human resolution.
                self._log_conflict(term.source, existing.target, term.target, chapter)
                self.conn.execute(
                    "UPDATE glossary SET status='conflict', updated_at=? WHERE source=?",
                    (now, term.source),
                )
                result = "conflict"
            if self._owns_connection:
                self.conn.commit()
            return result
        except Exception:
            if self._owns_connection:
                self.conn.rollback()
            raise

    def _log_conflict(self, source, existing_target, proposed_target, chapter):
        """Record one candidate-translation conflict within the current transaction."""
        self.conn.execute(
            """INSERT INTO term_conflicts
               (source,existing_target,proposed_target,chapter,created_at)
               VALUES (?,?,?,?,?)""",
            (source, existing_target, proposed_target, chapter, time.time()),
        )

    def resolve_term(self, source: str, target: str, *, status: str = STATUS_OK) -> bool:
        """Resolve the final translation and status; report whether the term exists.

        A resolution is a user decision, so ``first_chapter`` (extraction provenance) is
        cleared and the entry is always sent, like an imported one.
        """
        resolved_status = normalize_term_status(status)
        if resolved_status == STATUS_CONFLICT:
            resolved_status = STATUS_OK
        cur = self.conn.execute(
            "UPDATE glossary SET target=?, status=?, first_chapter=NULL, updated_at=? "
            "WHERE source=?",
            (target, resolved_status, time.time(), source),
        )
        if self._owns_connection:
            self.conn.commit()
        return cur.rowcount > 0

    def all_terms(self) -> list[GlossaryTerm]:
        """Return all terms in insertion order (rowid).
        Translation, review and polishing prompts share this source. Existing positions must
        remain stable while new entries append at the end, preserving provider prefix-cache
        hits. Do not sort by type/source here: inserted entries would shift later text and
        invalidate the prefix. Alphabetical grouping belongs in the display layer, such as
        CLI sorted().
        """
        rows = self.conn.execute("SELECT * FROM glossary ORDER BY rowid").fetchall()
        return [GlossaryTerm.from_row(r) for r in rows]

    def get_terms(self, sources: list[str]) -> list[GlossaryTerm]:
        """Look up a bounded set of exact sources independently of display filters."""
        if not sources:
            return []
        placeholders = ",".join("?" for _ in sources)
        rows = self.conn.execute(
            f"SELECT * FROM glossary WHERE source IN ({placeholders})", sources
        )
        return [GlossaryTerm.from_row(row) for row in rows]

    def list_terms_page(
        self, *, q: str = "", term_type: str = "", offset: int = 0, limit: int = 100
    ) -> tuple[list[GlossaryTerm], int]:
        """Read a source-codepoint ordered display page, never changing prompt order."""
        where = " WHERE type = ?" if term_type else ""
        params = (term_type,) if term_type else ()
        query = f"SELECT * FROM glossary{where} ORDER BY source COLLATE BINARY"
        if q.strip():
            # Stream substring searches to preserve Python's Unicode casefold semantics.
            # SQL lower()/LIKE differ across SQLite and PostgreSQL for characters such as ß.
            rows = self.conn.execute(query, params)
            return search_term_page(
                (GlossaryTerm.from_row(row) for row in rows), q, offset=offset, limit=limit
            )
        total = self.conn.execute(f"SELECT COUNT(*) FROM glossary{where}", params).fetchone()[0]
        rows = self.conn.execute(f"{query} LIMIT ? OFFSET ?", (*params, limit, offset))
        return [GlossaryTerm.from_row(row) for row in rows], total

    @staticmethod
    def terms_in(terms: list[GlossaryTerm], text: str) -> list[GlossaryTerm]:
        """Filter a prefetched term list by source/alias occurrences in text.
        Match evidence and local revision context without querying the database per term.
        """
        out: list[GlossaryTerm] = []
        normalized_text = _match_text(text)
        for term in terms:
            # Forms of address, verbal habits and fixed expressions carry tone or context. A bare-name alias
            # must not inject their derived translation into an ordinary address.
            keys = term_match_sources(term)
            if any(source_matches_normalized(k, normalized_text) for k in keys):
                out.append(term)
        return out

    @staticmethod
    def recurring_terms(
        terms: list[GlossaryTerm],
        text: str,
        *,
        min_occurrences: int = 2,
    ) -> list[GlossaryTerm]:
        """Select terms meeting the requested source/alias occurrence count.
        Count appellations, honorifics, speech habits and fixed expressions by source only
        so bare-name aliases cannot falsely make them frequent. For other types, merge
        source and deduplicated alias occurrences.
        """
        return GlossaryOccurrenceMatcher(text).recurring_terms(
            terms,
            min_occurrences=min_occurrences,
        )

    def mark_conflicts_resolved(self, source: str) -> None:
        """Mark every unresolved conflict for the given source term as handled."""
        self.conn.execute("UPDATE term_conflicts SET resolved=1 WHERE source=?", (source,))
        if self._owns_connection:
            self.conn.commit()

    def open_conflicts(self) -> list[dict[str, Any]]:
        """Return conflicts awaiting human resolution in occurrence order."""
        rows = self.conn.execute(
            "SELECT * FROM term_conflicts WHERE resolved=0 ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict[str, int]:
        """Return glossary and unresolved-conflict counts."""
        g = self.conn.execute("SELECT COUNT(*) FROM glossary").fetchone()[0]
        c = self.conn.execute("SELECT COUNT(*) FROM term_conflicts WHERE resolved=0").fetchone()[0]
        return {"terms": g, "open_conflicts": c}
