"""Transactional, rebuildable paragraph projections for local reader seeks.

Artifacts remain authoritative. Only chapters actually opened by a reader are
indexed. Once indexed, their projections are updated in the artifact transaction.
"""

import json
import sqlite3


def install(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS reader_chapters (
        artifact_key TEXT NOT NULL REFERENCES artifacts(key) ON DELETE CASCADE,
        chapter_index INTEGER NOT NULL, title TEXT NOT NULL, meta TEXT NOT NULL,
        binding TEXT, total INTEGER NOT NULL, saved_count INTEGER NOT NULL,
        PRIMARY KEY (artifact_key, chapter_index))"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS reader_segments (
        artifact_key TEXT NOT NULL, chapter_index INTEGER NOT NULL,
        position INTEGER NOT NULL, segment_index INTEGER NOT NULL,
        source TEXT NOT NULL, target TEXT, target_before_polish TEXT,
        kind TEXT NOT NULL, anchor TEXT,
        PRIMARY KEY (artifact_key, chapter_index, position),
        UNIQUE (artifact_key, chapter_index, segment_index),
        FOREIGN KEY (artifact_key, chapter_index)
            REFERENCES reader_chapters(artifact_key, chapter_index) ON DELETE CASCADE)"""
    )


def binding(parsed: dict | None) -> str | None:
    return json.dumps(parsed, sort_keys=True, ensure_ascii=False) if parsed is not None else None


def write_chapter(
    connection: sqlite3.Connection, key: str, chapter: dict, identity: dict | None = None
) -> None:
    ci = chapter["index"]
    segments = [s for s in chapter.get("segments", []) if s.get("source", "").strip()]
    # Ingest/source identities and stable positions change only on a replacement,
    # not ordinary translation saves. Clear incompatible ID mappings before upsert.
    old_ids = [
        row[0]
        for row in connection.execute(
            """SELECT segment_index FROM reader_segments
            WHERE artifact_key=? AND chapter_index=? ORDER BY position""",
            (key, ci),
        )
    ]
    if old_ids != [s["index"] for s in segments]:
        connection.execute(
            "DELETE FROM reader_chapters WHERE artifact_key=? AND chapter_index=?", (key, ci)
        )
    connection.execute(
        """INSERT INTO reader_chapters
        (artifact_key,chapter_index,title,meta,binding,total,saved_count)
        VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(artifact_key,chapter_index) DO UPDATE SET
        title=excluded.title,meta=excluded.meta,binding=excluded.binding,
        total=excluded.total,saved_count=excluded.saved_count""",
        (
            key,
            ci,
            chapter.get("title", ""),
            json.dumps(chapter.get("meta") or {}),
            binding(identity),
            len(segments),
            sum(s.get("target") is not None for s in segments),
        ),
    )
    connection.executemany(
        """INSERT INTO reader_segments
        (artifact_key,chapter_index,position,segment_index,source,target,target_before_polish,kind,anchor)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(artifact_key,chapter_index,position) DO UPDATE SET
        source=excluded.source,target=excluded.target,
        target_before_polish=excluded.target_before_polish,kind=excluded.kind,anchor=excluded.anchor
        WHERE source IS NOT excluded.source OR target IS NOT excluded.target
        OR target_before_polish IS NOT excluded.target_before_polish
        OR kind IS NOT excluded.kind OR anchor IS NOT excluded.anchor""",
        (
            (
                key,
                ci,
                position,
                s["index"],
                s["source"],
                s.get("target"),
                s.get("target_before_polish"),
                s.get("kind", "text"),
                s.get("anchor"),
            )
            for position, s in enumerate(segments)
        ),
    )


def refresh(connection: sqlite3.Connection, key: str, value: object) -> None:
    """Maintain only existing projections, using the writer's already decoded value."""
    indexed = {
        row[0]
        for row in connection.execute(
            "SELECT chapter_index FROM reader_chapters WHERE artifact_key=?", (key,)
        )
    }
    if not indexed:
        return
    if not isinstance(value, dict):
        connection.execute("DELETE FROM reader_chapters WHERE artifact_key=?", (key,))
        return
    parsed = key == "parsed_document.json"
    chapters = value.get("document", {}).get("chapters", []) if parsed else [value]
    identity = (
        {name: value.get(name) for name in ("source_sha256", "ingest_config")} if parsed else None
    )
    for chapter in chapters:
        ci = chapter.get("index")
        if ci in indexed:
            write_chapter(connection, key, chapter, identity)
            indexed.remove(ci)
    connection.executemany(
        "DELETE FROM reader_chapters WHERE artifact_key=? AND chapter_index=?",
        ((key, ci) for ci in indexed),
    )


def prepare(connection: sqlite3.Connection, ci: int, parsed: dict | None) -> None:
    """Build a chapter once; the caller holds the short state transaction."""
    key = "parsed_document.json" if parsed is not None else f"chapters/ch{ci}.json"
    if connection.execute(
        "SELECT 1 FROM reader_chapters WHERE artifact_key=? AND chapter_index=?", (key, ci)
    ).fetchone():
        return
    # One cold decode, never a full Chapter/Document model and never per window.
    row = connection.execute("SELECT value FROM artifacts WHERE key=?", (key,)).fetchone()
    if row is None:
        return
    value = json.loads(row[0])
    if parsed is not None:
        if any(value.get(name) != expected for name, expected in parsed.items()):
            raise KeyError(f"Parsed chapter not found: {ci}")
        chapter = next(
            (ch for ch in value.get("document", {}).get("chapters", []) if ch["index"] == ci), None
        )
    else:
        chapter = value
    if chapter is not None:
        write_chapter(connection, key, chapter, parsed)


class IndexedChapterReader:
    def __init__(self, connection: sqlite3.Connection, ci: int, parsed: dict | None):
        self.connection = connection
        self.ci = ci
        self.key = "parsed_document.json" if parsed is not None else f"chapters/ch{ci}.json"
        self.args = self.key, ci
        self.header = connection.execute(
            "SELECT * FROM reader_chapters WHERE artifact_key=? AND chapter_index=?", self.args
        ).fetchone()
        if self.header is None or self.header["binding"] != binding(parsed):
            raise KeyError(f"Chapter not found: {ci}")

    def window(self, offset: int, limit: int, around: int | None) -> dict:
        from .chapter_reading import validate_window

        validate_window(offset, limit)
        position = None
        if around is not None:
            row = self.connection.execute(
                """SELECT position FROM reader_segments
                WHERE artifact_key=? AND chapter_index=? AND segment_index=?""",
                (*self.args, around),
            ).fetchone()
            if row is None:
                raise KeyError(f"Segment not found: {around}")
            position = row[0]
            offset = position // limit * limit
        rows = self.connection.execute(
            """SELECT segment_index AS "index",position,source,target,target_before_polish,kind,anchor
            FROM reader_segments WHERE artifact_key=? AND chapter_index=?
            AND position>=? AND position<? ORDER BY position""",
            (*self.args, offset, offset + limit),
        )
        meta = json.loads(self.header["meta"])
        return {
            "index": self.ci,
            "title": self.header["title"],
            "title_translated": meta.get("title_translated"),
            "_meta": meta,
            "offset": offset,
            "position": position,
            "total": self.header["total"],
            "saved_count": self.header["saved_count"],
            "segments": [dict(row) for row in rows],
        }

    def targets(self, indices: list[int]) -> list[dict]:
        from .chapter_reading import MAX_WINDOW

        if len(indices) > MAX_WINDOW:
            raise ValueError("Too many paragraph revisions")
        placeholders = ",".join("?" for _ in indices)
        # Ordering by position in SQL can make SQLite choose the position index
        # and scan the whole chapter. Seek IDs first, then sort the bounded result.
        rows = self.connection.execute(
            f"""SELECT segment_index AS "index",position,target,target_before_polish
            FROM reader_segments WHERE artifact_key=? AND chapter_index=?
            AND segment_index IN ({placeholders})""",
            (*self.args, *indices),
        ).fetchall()
        return [
            {name: row[name] for name in ("index", "target", "target_before_polish")}
            for row in sorted(rows, key=lambda row: row["position"])
        ]
