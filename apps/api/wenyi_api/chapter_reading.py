"""Bounded PostgreSQL projections for the shared continuous chapter reader."""

from psycopg.types.json import Jsonb
from wenyi_core.storage.chapter_reading import MAX_WINDOW, SOURCE_WHITESPACE, validate_window


class PostgresChapterReader:
    def __init__(self, connection, pid: str, ci: int, parsed: dict | None = None):
        self.connection = connection
        self.pid = pid
        self.ci = ci
        if parsed is None:
            self.header = """SELECT title,title_translated,meta FROM chapters
                WHERE project_id=%s AND seq=%s"""
            self.header_args = (pid, ci)
            self.rows = """SELECT seg_seq AS id,seg_seq AS sort_key,source,target,
                target_before_polish,kind,anchor FROM segments
                WHERE project_id=%s AND chapter_seq=%s AND btrim(source,%s)<>''"""
            self.args = (pid, ci, SOURCE_WHITESPACE)
        else:
            chapter = """SELECT ch.value FROM artifacts a,
                jsonb_array_elements(a.value->'document'->'chapters') ch
                WHERE a.project_id=%s AND a.key='parsed_document.json'
                AND a.value->>'source_sha256'=%s AND a.value->'ingest_config'=%s
                AND (ch.value->>'index')::integer=%s"""
            self.header_args = (
                pid,
                parsed["source_sha256"],
                Jsonb(parsed["ingest_config"]),
                ci,
            )
            self.header = f"""SELECT value->>'title',NULL,value->'meta'
                FROM ({chapter}) chapter"""
            self.rows = f"""SELECT (s.value->>'index')::integer AS id,
                s.ordinality AS sort_key,s.value->>'source' AS source,
                s.value->>'target' AS target,s.value->>'target_before_polish' AS target_before_polish,
                s.value->>'kind' AS kind,s.value->>'anchor' AS anchor
                FROM ({chapter}) chapter,
                jsonb_array_elements(chapter.value->'segments') WITH ORDINALITY s
                WHERE btrim(s.value->>'source',%s)<>''"""
            self.args = (*self.header_args, SOURCE_WHITESPACE)

    def window(self, offset: int, limit: int, around: int | None) -> dict:
        validate_window(offset, limit)
        header = self.connection.execute(self.header, self.header_args).fetchone()
        if header is None:
            raise KeyError(f"Chapter not found: {self.ci}")
        counts = self.connection.execute(
            f"""SELECT count(*),count(*) FILTER (WHERE target IS NOT NULL)
                FROM ({self.rows}) rows""",
            self.args,
        ).fetchone()
        position = None
        if around is not None:
            found = self.connection.execute(
                f"""SELECT position FROM (SELECT id,
                    row_number() OVER (ORDER BY sort_key)-1 AS position
                    FROM ({self.rows}) rows) positions WHERE id=%s""",
                (*self.args, around),
            ).fetchone()
            if found is None:
                raise KeyError(f"Segment not found: {around}")
            position = found[0]
            offset = position // limit * limit
        fields = ("index", "source", "target", "target_before_polish", "kind", "anchor")
        rows = self.connection.execute(
            f"""SELECT id,source,target,target_before_polish,kind,anchor
                FROM ({self.rows}) rows ORDER BY sort_key LIMIT %s OFFSET %s""",
            (*self.args, limit, offset),
        ).fetchall()
        return {
            "index": self.ci,
            "title": header[0] or "",
            "title_translated": header[1],
            "_meta": header[2] or {},
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
        rows = self.connection.execute(
            """SELECT seg_seq,target,target_before_polish FROM segments
                WHERE project_id=%s AND chapter_seq=%s AND seg_seq=ANY(%s) ORDER BY seg_seq""",
            (self.pid, self.ci, indices),
        )
        return [{"index": row[0], "target": row[1], "target_before_polish": row[2]} for row in rows]
