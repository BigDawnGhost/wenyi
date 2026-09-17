"""PostgreSQL implementation of the core storage port.

All mutable run state lives in PostgreSQL. ``run_dir`` contains only source resources
and exported artifacts. Long workflow locks use session advisory locks, while state
writes and export snapshots use short transactions with no model calls inside them.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator, Literal

from psycopg import sql
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool
from wenyi_core.glossary.store import GlossaryStore, GlossaryTerm
from wenyi_core.ingest.models import Chapter, Document, Segment
from wenyi_core.pipeline.runstore import ExportSnapshotStore, source_sha256

from .segment_history import load_history, record_chapter_changes


class ProjectBusyError(BlockingIOError):
    """A different workflow or editor currently owns this project."""


class PostgresStorage:
    def __init__(self, project_id: str, pool: ConnectionPool, *, run_dir: str | None = None):
        self.project_id = project_id
        self._pool = pool
        self._run_dir = os.path.abspath(run_dir) if run_dir else None
        self._local = threading.local()

    @property
    def run_dir(self) -> str:
        if self._run_dir is None:
            raise ValueError("A resource directory is required for source parsing and export")
        return self._run_dir

    @property
    def source_dir(self) -> str:
        return os.path.join(self.run_dir, "source")

    @property
    def reviews_dir(self) -> str:
        """Logical review run identity; artifacts are persisted through the DB port."""
        return os.path.join(self.run_dir, "reviews")

    def close(self) -> None:
        """The application owns the shared pool."""

    @property
    @contextmanager
    def _conn(self):
        current = getattr(self._local, "state_conn", None)
        if current is not None:
            yield current
        else:
            with self._pool.connection() as conn:
                yield conn

    def _lock_key(self, scope: str) -> int:
        digest = hashlib.blake2b(f"wenyi:{scope}:{self.project_id}".encode(), digest_size=8)
        return int.from_bytes(digest.digest(), "big", signed=True)

    @contextmanager
    def _session_lock(self, scope: str, *, blocking: bool = True) -> Iterator[None]:
        depths = getattr(self._local, "lock_depths", None)
        if depths is None:
            depths = self._local.lock_depths = {}
        if depths.get(scope, 0):
            depths[scope] += 1
            try:
                yield
            finally:
                depths[scope] -= 1
            return
        key = self._lock_key(scope)
        with self._pool.connection() as conn:
            acquired = False
            try:
                if blocking:
                    conn.execute("SELECT pg_advisory_lock(%s)", (key,))
                    acquired = True
                else:
                    lock_row = conn.execute("SELECT pg_try_advisory_lock(%s)", (key,)).fetchone()
                    acquired = bool(lock_row[0]) if lock_row is not None else False
                conn.commit()  # A session lock needs no open transaction during LLM calls.
                if not acquired:
                    raise ProjectBusyError(f"Project {self.project_id} is busy")
                depths[scope] = 1
                yield
            finally:
                depths.pop(scope, None)
                if acquired:
                    conn.rollback()
                    conn.execute("SELECT pg_advisory_unlock(%s)", (key,))
                    conn.commit()

    def lock(self, *, blocking: bool = True):
        return self._session_lock("write", blocking=blocking)

    def export_lock(self, export_id: int, *, blocking: bool = True):
        """Keep one export's render/recovery mutually exclusive without blocking translation."""
        if not isinstance(export_id, int) or isinstance(export_id, bool) or export_id <= 0:
            raise ValueError("Invalid export identifier")
        return self._session_lock(f"export:{export_id}", blocking=blocking)

    def assemble_lock(self):
        return self._session_lock("assemble")

    @contextmanager
    def state_lock(self) -> Iterator[None]:
        """Serialize a brief state update and make its nested calls one transaction."""
        if getattr(self._local, "state_conn", None) is not None:
            yield
            return
        with self._pool.connection() as conn:
            with conn.transaction():
                conn.execute("SELECT pg_advisory_xact_lock(%s)", (self._lock_key("state"),))
                self._local.state_conn = conn
                try:
                    yield
                finally:
                    self._local.state_conn = None

    # Initialization is committed only after chapters, analysis, glossary and context.
    def exists(self) -> bool:
        with self._conn as conn:
            row = conn.execute(
                "SELECT initialized FROM projects WHERE id=%s", (self.project_id,)
            ).fetchone()
        return bool(row and row[0])

    @staticmethod
    def _validate_digest(digest: object) -> None:
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid source SHA-256 format")

    def begin_initialization(self, source_hash: str) -> None:
        self._validate_digest(source_hash)
        with self.state_lock(), self._conn as conn:
            row = conn.execute(
                "SELECT initialized, initialization_sha256, source_sha256 FROM projects WHERE id=%s FOR UPDATE",
                (self.project_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"project {self.project_id} not found")
            if row[0]:
                raise ValueError("Project is already initialized; create a new project")
            for table in ("chapters", "glossary", "term_conflicts"):
                conn.execute(
                    sql.SQL("DELETE FROM {} WHERE project_id=%s").format(sql.Identifier(table)),
                    (self.project_id,),
                )
            if row[1] != source_hash:
                conn.execute("DELETE FROM events WHERE project_id=%s", (self.project_id,))
                # Upload parsing happens before preparation. Preserve the matching
                # source preview, but discard mutable run state.
                conn.execute(
                    """DELETE FROM artifacts WHERE project_id=%s
                    AND NOT (%s AND key IN ('parsed_document.json','preview.json'))""",
                    (self.project_id, row[2] == source_hash),
                )
                conn.execute("DELETE FROM artifact_events WHERE project_id=%s", (self.project_id,))
            else:
                conn.execute(
                    "DELETE FROM artifacts WHERE project_id=%s AND key IN ('usage-pending.json','timing.json')",
                    (self.project_id,),
                )
            conn.execute(
                """UPDATE projects SET initialization_sha256=%s, source_sha256=%s,
                manifest='{}'::jsonb, meta='{}'::jsonb, context=NULL,
                annotation_contexts=NULL, analysis=NULL, usage=NULL, report=NULL,
                updated_at=now() WHERE id=%s""",
                (source_hash, source_hash, self.project_id),
            )

    def finish_initialization(self) -> None:
        with self._conn as conn:
            conn.execute(
                "UPDATE projects SET initialization_sha256=NULL WHERE id=%s AND initialized",
                (self.project_id,),
            )

    def stage_document(self, doc: Document, *, source_hash: str | None = None) -> dict:
        digest = source_hash or source_sha256(doc.source_path)
        self._validate_digest(digest)
        meta = dict(doc.meta)
        annotations = meta.pop("epub_annotation_contexts", None)
        manifest = {
            "title": doc.title,
            "fmt": doc.fmt,
            "source_path": doc.source_path,
            "source_sha256": digest,
            "source_lang": doc.source_lang,
            "target_lang": doc.target_lang,
            "meta": meta,
            "chapters": [
                {
                    "index": ch.index,
                    "title": ch.title,
                    "href": ch.href,
                    "toc_entry_id": ch.meta.get("toc_entry_id"),
                    "status": "pending",
                }
                for ch in doc.chapters
            ],
        }
        with self.state_lock(), self._conn as conn:
            conn.execute(
                "UPDATE projects SET source_path=%s, source_sha256=%s, annotation_contexts=%s WHERE id=%s",
                (
                    doc.source_path,
                    digest,
                    Jsonb(annotations) if annotations else None,
                    self.project_id,
                ),
            )
            for chapter in doc.chapters:
                self.save_chapter(chapter)
        return manifest

    def init_from_document(self, doc: Document) -> dict:
        manifest = self.stage_document(doc)
        self.save_manifest(manifest)
        self.finish_initialization()
        return manifest

    def ensure_source_identity(self, input_path: str, *, actual_sha256: str | None = None) -> str:
        actual = actual_sha256 or source_sha256(input_path)
        self._validate_source_identity(self.load_manifest(), actual)
        return actual

    @classmethod
    def _validate_source_identity(cls, manifest: dict, actual: str) -> None:
        cls._validate_digest(actual)
        expected = manifest.get("source_sha256")
        cls._validate_digest(expected)
        if expected != actual:
            raise ValueError(
                "Input content does not match existing translation state; create a new project"
            )

    def load_manifest(self) -> dict:
        with self.state_lock(), self._conn as conn:
            row = conn.execute(
                """SELECT manifest,title,fmt,source_path,source_sha256,
                source_lang,target_lang,meta FROM projects WHERE id=%s""",
                (self.project_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"project {self.project_id} not found")
            chapters = conn.execute(
                """SELECT seq,title,href,status,title_translated,
                review_status,manifest_entry FROM chapters WHERE project_id=%s ORDER BY seq""",
                (self.project_id,),
            ).fetchall()
        manifest = dict(row[0] or {})
        manifest.update(
            zip(
                (
                    "title",
                    "fmt",
                    "source_path",
                    "source_sha256",
                    "source_lang",
                    "target_lang",
                    "meta",
                ),
                row[1:],
            )
        )
        manifest["meta"] = manifest.get("meta") or {}
        manifest["chapters"] = []
        for ch in chapters:
            entry = dict(ch[6] or {})
            entry.update(index=ch[0], title=ch[1], href=ch[2], status=ch[3], review_status=ch[5])
            if ch[4] is not None:
                entry["title_translated"] = ch[4]
            manifest["chapters"].append(entry)
        return manifest

    def save_manifest(self, manifest: dict) -> None:
        with self.state_lock(), self._conn as conn:
            conn.execute(
                """UPDATE projects SET manifest=%s,title=%s,fmt=%s,
                source_path=COALESCE(%s,source_path),source_sha256=COALESCE(%s,source_sha256),
                source_lang=%s,target_lang=%s,meta=%s,initialized=TRUE,updated_at=now()
                WHERE id=%s""",
                (
                    Jsonb(manifest),
                    manifest.get("title"),
                    manifest.get("fmt"),
                    manifest.get("source_path"),
                    manifest.get("source_sha256"),
                    manifest.get("source_lang"),
                    manifest.get("target_lang"),
                    Jsonb(manifest.get("meta") or {}),
                    self.project_id,
                ),
            )
            for entry in manifest.get("chapters", []):
                conn.execute(
                    """INSERT INTO chapters(project_id,seq,title,href,status,
                    title_translated,review_status,manifest_entry) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(project_id,seq) DO UPDATE SET title=EXCLUDED.title,
                    href=EXCLUDED.href,status=EXCLUDED.status,title_translated=EXCLUDED.title_translated,
                    review_status=EXCLUDED.review_status,manifest_entry=EXCLUDED.manifest_entry""",
                    (
                        self.project_id,
                        entry["index"],
                        entry.get("title", ""),
                        entry.get("href"),
                        entry.get("status", "pending"),
                        entry.get("title_translated"),
                        entry.get("review_status", "pending"),
                        Jsonb(entry),
                    ),
                )

    def set_chapter_status(self, ci: int, status: str) -> None:
        with self.state_lock(), self._conn as conn:
            conn.execute(
                "UPDATE chapters SET status=%s WHERE project_id=%s AND seq=%s",
                (status, self.project_id, ci),
            )

    def set_chapter_review_status(self, ci: int, status: str) -> None:
        with self.state_lock(), self._conn as conn:
            conn.execute(
                "UPDATE chapters SET review_status=%s WHERE project_id=%s AND seq=%s",
                (status, self.project_id, ci),
            )

    def pending_chapters(self) -> list[int]:
        with self._conn as conn:
            rows = conn.execute(
                "SELECT seq FROM chapters WHERE project_id=%s AND status<>'done' ORDER BY seq",
                (self.project_id,),
            ).fetchall()
        return [row[0] for row in rows]

    def save_chapter(
        self, chapter: Chapter, *, revision_kind: Literal["manual"] | None = None
    ) -> None:
        with self.state_lock(), self._conn as conn:
            translated = getattr(chapter, "title_translated", None) or chapter.meta.get(
                "title_translated"
            )
            conn.execute(
                """INSERT INTO chapters(project_id,seq,title,href,template,meta,title_translated)
                VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(project_id,seq) DO UPDATE
                SET title=EXCLUDED.title,href=EXCLUDED.href,template=EXCLUDED.template,
                meta=EXCLUDED.meta,title_translated=COALESCE(EXCLUDED.title_translated,chapters.title_translated)""",
                (
                    self.project_id,
                    chapter.index,
                    chapter.title,
                    chapter.href,
                    chapter.template,
                    Jsonb(chapter.meta),
                    translated,
                ),
            )
            record_chapter_changes(conn, self.project_id, chapter, kind=revision_kind)
            conn.execute(
                "DELETE FROM segments WHERE project_id=%s AND chapter_seq=%s",
                (self.project_id, chapter.index),
            )
            if chapter.segments:
                with conn.cursor() as cursor:
                    cursor.executemany(
                        """INSERT INTO segments(project_id,chapter_seq,seg_seq,source,target,
                        target_before_polish,kind,anchor,cont,meta,resource_href)
                        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        [
                            (
                                self.project_id,
                                chapter.index,
                                s.index,
                                s.source,
                                s.target,
                                s.target_before_polish,
                                s.kind,
                                s.anchor,
                                s.cont,
                                Jsonb(s.meta),
                                s.resource_href,
                            )
                            for s in chapter.segments
                        ],
                    )

    def save_chapter_with_status(self, chapter: Chapter, status: str) -> None:
        with self.state_lock():
            self.save_chapter(chapter)
            self.set_chapter_status(chapter.index, status)

    def load_segment_history(self, ci: int, si: int) -> list[dict]:
        with self.state_lock(), self._conn as conn:
            return load_history(conn, self.project_id, ci, si)

    def load_chapter(self, ci: int) -> Chapter:
        with self.state_lock(), self._conn as conn:
            row = conn.execute(
                "SELECT title,href,template,meta,title_translated FROM chapters WHERE project_id=%s AND seq=%s",
                (self.project_id, ci),
            ).fetchone()
            if row is None:
                raise KeyError(f"chapter {ci} not found in project {self.project_id}")
            segments = conn.execute(
                """SELECT seg_seq,source,target,target_before_polish,kind,anchor,
                cont,meta,resource_href FROM segments WHERE project_id=%s AND chapter_seq=%s ORDER BY seg_seq""",
                (self.project_id, ci),
            ).fetchall()
        meta = dict(row[3] or {})
        if row[4]:
            meta["title_translated"] = row[4]
        return Chapter(
            index=ci,
            title=row[0],
            href=row[1],
            template=row[2],
            meta=meta,
            segments=[
                Segment(
                    index=s[0],
                    source=s[1],
                    target=s[2],
                    target_before_polish=s[3],
                    kind=s[4],
                    anchor=s[5],
                    cont=s[6],
                    meta=s[7],
                    resource_href=s[8],
                )
                for s in segments
            ],
        )

    def create_export_snapshot(self, *, actual_sha256: str) -> ExportSnapshotStore:
        """Capture all chapters in one MVCC snapshot; release DB before rendering."""
        if getattr(self._local, "state_conn", None) is not None:
            raise RuntimeError("Export snapshot must start outside a write transaction")
        with self._pool.connection() as conn:
            with conn.transaction():
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                self._local.state_conn = conn
                try:
                    manifest = self.load_manifest()
                    self._validate_source_identity(manifest, actual_sha256)
                    chapters = {
                        entry["index"]: self.load_chapter(entry["index"])
                        for entry in manifest["chapters"]
                    }
                finally:
                    self._local.state_conn = None
        return ExportSnapshotStore(self.run_dir, manifest, chapters)

    # Structured project state and generic artifacts.
    def _save_field(self, field: str, value: Any) -> None:
        with self._conn as conn:
            conn.execute(
                sql.SQL("UPDATE projects SET {}=%s,updated_at=now() WHERE id=%s").format(
                    sql.Identifier(field)
                ),
                (Jsonb(value), self.project_id),
            )

    def _load_field(self, field: str) -> Any:
        with self._conn as conn:
            row = conn.execute(
                sql.SQL("SELECT {} FROM projects WHERE id=%s").format(sql.Identifier(field)),
                (self.project_id,),
            ).fetchone()
        return row[0] if row else None

    def save_context(self, data: dict) -> None:
        self._save_field("context", data)

    def load_context(self) -> dict | None:
        return self._load_field("context")

    def save_analysis(self, data: dict) -> None:
        self._save_field("analysis", data)

    def load_analysis(self) -> dict | None:
        return self._load_field("analysis")

    def save_report(self, data: dict) -> None:
        self._save_field("report", data)

    def load_report(self) -> dict | None:
        return self._load_field("report")

    def save_usage(self, data: dict) -> None:
        self._save_field("usage", data)

    def load_usage(self) -> dict | None:
        return self._load_field("usage")

    def save_annotation_contexts(self, data: dict) -> None:
        self._save_field("annotation_contexts", data)

    def load_annotation_contexts(self) -> dict | None:
        return self._load_field("annotation_contexts")

    @staticmethod
    def _artifact_key(key: str, *, allow_empty: bool = False) -> str:
        if allow_empty and not key:
            return key
        if (
            not isinstance(key, str)
            or not key
            or "\\" in key
            or key.startswith("/")
            or "\x00" in key
        ):
            raise ValueError("Invalid artifact key")
        if any(part in {"", ".", ".."} for part in key.rstrip("/").split("/")):
            raise ValueError("Invalid artifact key")
        return key

    def read_artifact(self, key: str) -> Any | None:
        key = self._artifact_key(key)
        if key == "usage.json":
            return self.load_usage()
        with self._conn as conn:
            row = conn.execute(
                "SELECT value FROM artifacts WHERE project_id=%s AND key=%s", (self.project_id, key)
            ).fetchone()
        return row[0] if row else None

    def write_artifact(self, key: str, value: Any) -> None:
        key = self._artifact_key(key)
        if key == "usage.json":
            self.save_usage(value)
            return
        with self._conn as conn:
            conn.execute(
                """INSERT INTO artifacts(project_id,key,value) VALUES(%s,%s,%s)
                ON CONFLICT(project_id,key) DO UPDATE SET value=EXCLUDED.value,updated_at=now()""",
                (self.project_id, key, Jsonb(value)),
            )

    def delete_artifact(self, key: str) -> None:
        key = self._artifact_key(key)
        with self._conn as conn:
            conn.execute(
                "DELETE FROM artifacts WHERE project_id=%s AND key=%s", (self.project_id, key)
            )
            conn.execute(
                "DELETE FROM artifact_events WHERE project_id=%s AND key=%s", (self.project_id, key)
            )
            if key == "usage.json":
                conn.execute("UPDATE projects SET usage=NULL WHERE id=%s", (self.project_id,))

    def list_artifacts(self, prefix: str = "") -> list[str]:
        self._artifact_key(prefix, allow_empty=True)
        with self._conn as conn:
            rows = conn.execute(
                """SELECT key FROM artifacts WHERE project_id=%s AND starts_with(key,%s)
                UNION SELECT key FROM artifact_events WHERE project_id=%s AND starts_with(key,%s)
                ORDER BY key""",
                (self.project_id, prefix, self.project_id, prefix),
            ).fetchall()
        return [row[0] for row in rows]

    def append_artifact_record(self, key: str, record: dict) -> None:
        key = self._artifact_key(key)
        with self._conn as conn:
            conn.execute(
                "INSERT INTO artifact_events(project_id,key,payload) VALUES(%s,%s,%s)",
                (self.project_id, key, Jsonb(record)),
            )

    def read_artifact_records(self, key: str) -> list[dict]:
        key = self._artifact_key(key)
        with self._conn as conn:
            rows = conn.execute(
                "SELECT payload FROM artifact_events WHERE project_id=%s AND key=%s ORDER BY id",
                (self.project_id, key),
            ).fetchall()
        return [row[0] for row in rows]

    @staticmethod
    def _usage_key(key: str) -> str:
        if key != "usage.json" and not re.fullmatch(r"reviews/review-[^/]+/usage\.json", key):
            raise ValueError("Invalid usage journal destination")
        return key

    def prepare_usage_commit(self, ledgers: dict[str, dict]) -> None:
        from wenyi_core.llm.routing import identity

        with self.state_lock():
            entries = [
                {
                    "path": self._usage_key(key),
                    "before": identity(self.read_artifact(key)),
                    "value": value,
                }
                for key, value in ledgers.items()
            ]
            self.write_artifact("usage-pending.json", {"version": 1, "entries": entries})

    def recover_usage(self) -> None:
        from wenyi_core.llm.routing import identity
        from wenyi_core.llm.usage import validate_usage

        with self.state_lock():
            pending = self.read_artifact("usage-pending.json")
            if pending is None:
                return
            if pending.get("version") != 1 or not isinstance(pending.get("entries"), list):
                raise ValueError("Invalid usage journal")
            for entry in pending["entries"]:
                key = self._usage_key(entry["path"])
                value = validate_usage(entry["value"])
                if identity(self.read_artifact(key)) not in {entry["before"], identity(value)}:
                    raise ValueError("Usage ledger changed outside its pending commit")
                self.write_artifact(key, value)
            self.delete_artifact("usage-pending.json")

    def record_timing(self, record: dict[str, Any]) -> dict[str, Any]:
        with self.state_lock():
            ledger = self.read_artifact("timing.json") or {"runs": []}
            runs = {run["id"]: run for run in ledger["runs"]}
            runs[record["id"]] = record
            ledger = {
                "total_seconds": sum(run["elapsed_seconds"] for run in runs.values()),
                "runs": list(runs.values()),
            }
            self.write_artifact("timing.json", ledger)
            return ledger

    def load_timing(self) -> dict | None:
        return self.read_artifact("timing.json")

    def load_latest_review_result(self) -> dict | None:
        for key in reversed(self.list_artifacts("reviews/")):
            if re.fullmatch(r"reviews/review-[^/]+/result\.json", key):
                result = self.read_artifact(key)
                if isinstance(result, dict):
                    return result
        return None

    @staticmethod
    def batch_glossary_key(start_index: int, count: int) -> str:
        return f"{start_index}:{count}"

    def completed_batch_glossary_keys(self, chapter: int) -> set[str]:
        rows = self.list_events(event_type="batch_glossary_extracted", limit=0)
        return {
            self.batch_glossary_key(row["start_index"], row["count"])
            for row in rows
            if row.get("chapter") == chapter
            and isinstance(row.get("start_index"), int)
            and isinstance(row.get("count"), int)
        }

    def log_event(self, event: str, **data: Any) -> None:
        with self._conn as conn:
            conn.execute(
                "INSERT INTO events(project_id,type,payload) VALUES(%s,%s,%s)",
                (self.project_id, event, Jsonb(data)),
            )

    def list_events(self, *, event_type: str | None = None, limit: int = 200) -> list[dict]:
        with self._conn as conn:
            rows = conn.execute(
                """SELECT id,type,payload,created_at FROM events WHERE project_id=%s
                AND (%s::text IS NULL OR type=%s) ORDER BY id DESC LIMIT %s""",
                (self.project_id, event_type, event_type, limit or None),
            ).fetchall()
        result = []
        for row in reversed(rows):
            payload = dict(row[2] or {})
            payload.update(event=row[1], _id=row[0], _ts=row[3].isoformat(), ts=row[3].isoformat())
            result.append(payload)
        return result

    # Glossary retains first insertion order and established translations on conflict.
    _TERM_COLS = "source,target,reading,type,gender,aliases,first_chapter,note,status"

    @staticmethod
    def _row_to_term(row) -> GlossaryTerm:
        return GlossaryTerm(
            source=row[0],
            target=row[1],
            reading=row[2],
            type=row[3],
            gender=row[4],
            aliases=row[5],
            first_chapter=row[6],
            note=row[7],
            status=row[8],
        )

    def get_term(self, source: str) -> GlossaryTerm | None:
        with self._conn as conn:
            row = conn.execute(
                f"SELECT {self._TERM_COLS} FROM glossary WHERE project_id=%s AND source=%s",
                (self.project_id, source),
            ).fetchone()
        return self._row_to_term(row) if row else None

    def upsert_term(self, term: GlossaryTerm, chapter: int | None = None) -> str:
        with self.state_lock(), self._conn as conn:
            existing = self.get_term(term.source)
            now = time.time()
            if existing is None:
                conn.execute(
                    """INSERT INTO glossary(project_id,source,target,reading,type,gender,
                    aliases,first_chapter,note,status,updated_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        self.project_id,
                        term.source,
                        term.target,
                        term.reading,
                        term.type,
                        term.gender,
                        Jsonb(term.aliases),
                        term.first_chapter if term.first_chapter is not None else chapter,
                        term.note,
                        term.status,
                        now,
                    ),
                )
                return "inserted"
            if existing.target == term.target:
                aliases = sorted(set(existing.aliases) | set(term.aliases))
                conn.execute(
                    """UPDATE glossary SET reading=COALESCE(NULLIF(%s,''),reading),
                    gender=COALESCE(NULLIF(%s,''),gender),aliases=%s,note=COALESCE(NULLIF(%s,''),note),
                    updated_at=%s WHERE project_id=%s AND source=%s""",
                    (
                        term.reading,
                        term.gender,
                        Jsonb(aliases),
                        term.note,
                        now,
                        self.project_id,
                        term.source,
                    ),
                )
                return "unchanged"
            conn.execute(
                """INSERT INTO term_conflicts(project_id,source,existing_target,proposed_target,
                chapter,created_at) VALUES(%s,%s,%s,%s,%s,%s)""",
                (self.project_id, term.source, existing.target, term.target, chapter, now),
            )
            conn.execute(
                "UPDATE glossary SET status='conflict',updated_at=%s WHERE project_id=%s AND source=%s",
                (now, self.project_id, term.source),
            )
            return "conflict"

    def resolve_term(self, source: str, target: str) -> bool:
        with self.state_lock(), self._conn as conn:
            cursor = conn.execute(
                "UPDATE glossary SET target=%s,status='ok',updated_at=%s WHERE project_id=%s AND source=%s",
                (target, time.time(), self.project_id, source),
            )
            return cursor.rowcount > 0

    def delete_term(self, source: str) -> bool:
        with self.state_lock(), self._conn as conn:
            cursor = conn.execute(
                "DELETE FROM glossary WHERE project_id=%s AND source=%s", (self.project_id, source)
            )
            return cursor.rowcount > 0

    def all_terms(self) -> list[GlossaryTerm]:
        with self._conn as conn:
            rows = conn.execute(
                f"SELECT {self._TERM_COLS} FROM glossary WHERE project_id=%s ORDER BY insertion_id",
                (self.project_id,),
            ).fetchall()
        return [self._row_to_term(row) for row in rows]

    def terms_in(self, terms: list[GlossaryTerm], text: str) -> list[GlossaryTerm]:
        return GlossaryStore.terms_in(terms, text)

    def terms_in_text(self, text: str) -> list[GlossaryTerm]:
        return self.terms_in(self.all_terms(), text)

    def mark_conflicts_resolved(self, source: str) -> None:
        with self.state_lock(), self._conn as conn:
            conn.execute(
                "UPDATE term_conflicts SET resolved=TRUE WHERE project_id=%s AND source=%s",
                (self.project_id, source),
            )

    def open_conflicts(self) -> list[dict]:
        with self._conn as conn:
            rows = conn.execute(
                """SELECT id,source,existing_target,proposed_target,chapter,note FROM term_conflicts
                WHERE project_id=%s AND NOT resolved ORDER BY id""",
                (self.project_id,),
            ).fetchall()
        return [
            dict(
                zip(("id", "source", "existing_target", "proposed_target", "chapter", "note"), row)
            )
            for row in rows
        ]

    def stats(self) -> dict[str, int]:
        with self._conn as conn:
            terms_row = conn.execute(
                "SELECT count(*) FROM glossary WHERE project_id=%s", (self.project_id,)
            ).fetchone()
            conflicts_row = conn.execute(
                "SELECT count(*) FROM term_conflicts WHERE project_id=%s AND NOT resolved",
                (self.project_id,),
            ).fetchone()
            terms = 0 if terms_row is None else terms_row[0]
            conflicts = 0 if conflicts_row is None else conflicts_row[0]
        return {"terms": terms, "open_conflicts": conflicts}
