"""SQLite reader projections. JSON stays inside SQLite; only bounded rows reach Python.

Legacy chapter artifacts remain authoritative. SQLite still parses their JSON, but
HTTP reads do not allocate whole-book dictionaries, Pydantic models or response lists.
"""

import json
import sqlite3

from .chapter_reading import MAX_WINDOW, SOURCE_WHITESPACE, validate_window


class SqliteChapterReader:
    def __init__(self, connection: sqlite3.Connection, ci: int, parsed: dict | None = None):
        self.connection = connection
        self.ci = ci
        if parsed is None:
            self.chapter = "SELECT value FROM artifacts WHERE key=?"
            self.args = (f"chapters/ch{ci}.json",)
        else:
            binding = connection.execute(
                """SELECT json_extract(value,'$.source_sha256'),
                json_extract(value,'$.ingest_config') FROM artifacts
                WHERE key='parsed_document.json'"""
            ).fetchone()
            if (
                binding is None
                or binding[0] != parsed["source_sha256"]
                or json.loads(binding[1]) != parsed["ingest_config"]
            ):
                raise KeyError(f"Parsed chapter not found: {ci}")
            self.chapter = """SELECT ch.value FROM artifacts a,
                json_each(a.value,'$.document.chapters') ch
                WHERE a.key='parsed_document.json' AND json_extract(ch.value,'$.index')=?"""
            self.args = (ci,)
        self.cte = f"WITH chapter(value) AS ({self.chapter})"
        self.rows = """FROM chapter, json_each(chapter.value,'$.segments') s
            WHERE trim(json_extract(s.value,'$.source'),?)<>''"""
        self.row_args = (*self.args, SOURCE_WHITESPACE)

    def window(self, offset: int, limit: int, around: int | None) -> dict:
        validate_window(offset, limit)
        header = self.connection.execute(
            self.cte
            + """ SELECT json_extract(value,'$.title'),
                json_extract(value,'$.meta.title_translated'),
                json_extract(value,'$.meta') FROM chapter""",
            self.args,
        ).fetchone()
        if header is None:
            raise KeyError(f"Chapter not found: {self.ci}")
        counts = self.connection.execute(
            self.cte
            + """ SELECT count(*),
                coalesce(sum(json_extract(s.value,'$.target') IS NOT NULL),0) """
            + self.rows,
            self.row_args,
        ).fetchone()
        position = None
        if around is not None:
            found = self.connection.execute(
                self.cte
                + """, positions AS (SELECT json_extract(s.value,'$.index') AS id,
                    row_number() OVER (ORDER BY CAST(s.key AS INTEGER))-1 AS position """
                + self.rows
                + ") SELECT position FROM positions WHERE id=?",
                (*self.row_args, around),
            ).fetchone()
            if found is None:
                raise KeyError(f"Segment not found: {around}")
            position = found[0]
            offset = position // limit * limit
        fields = ("index", "source", "target", "target_before_polish", "kind", "anchor")
        projection = ",".join(f"json_extract(s.value,'$.{field}')" for field in fields)
        rows = self.connection.execute(
            self.cte
            + " SELECT "
            + projection
            + " "
            + self.rows
            + " ORDER BY CAST(s.key AS INTEGER) LIMIT ? OFFSET ?",
            (*self.row_args, limit, offset),
        ).fetchall()
        return {
            "index": self.ci,
            "title": header[0] or "",
            "title_translated": header[1],
            "_meta": json.loads(header[2]) if header[2] else {},
            "offset": offset,
            "position": position,
            "total": counts[0],
            "saved_count": counts[1],
            "segments": [
                {**dict(zip(fields, row)), "position": offset + i} for i, row in enumerate(rows)
            ],
        }

    def targets(self, indices: list[int]) -> list[dict]:
        if len(indices) > MAX_WINDOW:
            raise ValueError("Too many paragraph revisions")
        if not indices:
            return []
        fields = ("index", "target", "target_before_polish")
        projection = ",".join(f"json_extract(s.value,'$.{field}')" for field in fields)
        placeholders = ",".join("?" for _ in indices)
        rows = self.connection.execute(
            self.cte
            + f""" SELECT {projection} FROM chapter, json_each(chapter.value,'$.segments') s
                WHERE json_extract(s.value,'$.index') IN ({placeholders})
                ORDER BY CAST(s.key AS INTEGER)""",
            (*self.args, *indices),
        )
        return [dict(zip(fields, row)) for row in rows]
