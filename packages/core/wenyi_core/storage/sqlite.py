"""Local SQLite state for the desktop backend, with short transactions.

Resource directories hold only original inputs, parser caches and exports. All mutable
state, including JSON-shaped review artifacts and glossary rows, lives in state.sqlite3.
Long workflow locks are OS locks, never open database transactions.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from ..glossary.store import GlossaryStore, GlossaryTerm
from ..ingest.models import Chapter, Document
from ..pipeline.runstore import ExportSnapshotStore, RunStore, source_sha256
from .protocol import STATUS_DONE, STATUS_PENDING


class StorageBusyError(BlockingIOError):
    """Another local process owns the requested workflow lock."""


class SqliteStorage:
    """One target-isolated local database, safe across threads and processes.

    A reentrant mutex serializes this instance's connection. Independent instances use
    SQLite's WAL and busy timeout. Nested state locks use savepoints in the same outer
    transaction, including borrowed glossary operations.
    """

    def __init__(self, run_dir: str, *, create: bool = True):
        self.run_dir = os.path.abspath(run_dir)
        self.database_path = os.path.join(self.run_dir, "state.sqlite3")
        self.source_dir = os.path.join(self.run_dir, "source")
        self.reviews_dir = os.path.join(self.run_dir, "reviews")
        self._create = create
        self._mutex = threading.RLock()
        self._lock_local = threading.local()
        self._conn: sqlite3.Connection | None = None
        self._depth = 0
        if create:
            with self._mutex:
                self._connection()

    def _connection(self) -> sqlite3.Connection:
        """Open lazily so read-only discovery of missing projects creates nothing."""
        if self._conn is not None:
            return self._conn
        if self._create:
            os.makedirs(self.run_dir, exist_ok=True)
        elif not os.path.isfile(self.database_path):
            raise FileNotFoundError(f"Local state database not found: {self.database_path}")
        # SQLite exposes the new file before its schema transaction commits.
        # Serialize first opens across instances/processes, including discovery;
        # ordinary reads/writes keep using WAL without holding this OS lock.
        with self._file_lock(".schema.lock"):
            return self._open_connection()

    def _open_connection(self) -> sqlite3.Connection:
        """Called under the instance mutex and the short schema initialization lock."""
        uri = Path(self.database_path).as_uri() + ("?mode=rwc" if self._create else "?mode=rw")
        conn = sqlite3.connect(uri, uri=True, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA foreign_keys = ON")
            if conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                conn.execute("PRAGMA journal_mode = WAL")
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifacts'"
            ).fetchone():
                if not self._create:
                    raise ValueError(f"Invalid local state database: {self.database_path}")
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS artifacts "
                    "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS artifact_records "
                    "(sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "key TEXT NOT NULL REFERENCES artifacts(key) ON DELETE CASCADE, "
                    "value TEXT NOT NULL)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS artifact_records_key "
                    "ON artifact_records(key, sequence)"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS chapter_statistics "
                    "(chapter_index INTEGER PRIMARY KEY, "
                    "artifact_key TEXT NOT NULL REFERENCES artifacts(key) ON DELETE CASCADE, "
                    "title TEXT NOT NULL, meta TEXT NOT NULL, "
                    "word_count INTEGER NOT NULL, target_word_count INTEGER NOT NULL)"
                )
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS segment_revisions "
                    "(id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "artifact_key TEXT NOT NULL REFERENCES artifacts(key) ON DELETE CASCADE, "
                    "chapter_index INTEGER NOT NULL, segment_index INTEGER NOT NULL, "
                    "kind TEXT NOT NULL, previous_target TEXT, new_target TEXT, "
                    "created_at TEXT NOT NULL)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS segment_revisions_segment "
                    "ON segment_revisions(chapter_index, segment_index, id)"
                )
                GlossaryStore.initialize_schema(conn)
                conn.commit()
        except BaseException:
            conn.close()
            raise
        self._conn = conn
        return conn

    def close(self) -> None:
        """Release the owned connection after active operations have finished."""
        with self._mutex:
            if self._depth:
                raise RuntimeError("Cannot close storage inside a state transaction")
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    @contextmanager
    def state_lock(self) -> Iterator[None]:
        """Commit a short atomic state change; nested failures roll back their savepoint."""
        with self._mutex:
            conn = self._connection()
            depth = self._depth
            savepoint = f"state_{depth}"
            conn.execute("BEGIN IMMEDIATE" if depth == 0 else f"SAVEPOINT {savepoint}")
            self._depth += 1
            try:
                yield
            except BaseException:
                if depth == 0:
                    conn.rollback()
                else:
                    conn.execute(f"ROLLBACK TO {savepoint}")
                    conn.execute(f"RELEASE {savepoint}")
                raise
            else:
                try:
                    if depth == 0:
                        conn.commit()
                    else:
                        conn.execute(f"RELEASE {savepoint}")
                except BaseException:
                    if depth == 0:
                        conn.rollback()
                    raise
            finally:
                self._depth -= 1

    @contextmanager
    def _read_lock(self) -> Iterator[None]:
        """Capture a WAL read snapshot without competing for SQLite's writer lock."""
        with self._mutex:
            conn = self._connection()
            if conn.in_transaction:
                yield
                return
            conn.execute("BEGIN")
            try:
                yield
            finally:
                conn.rollback()

    @contextmanager
    def _file_lock(self, filename: str, *, blocking: bool = True) -> Iterator[None]:
        """Reenter only for this instance's owning thread, never another caller."""
        depths = getattr(self._lock_local, "depths", None)
        if depths is None:
            depths = self._lock_local.depths = {}
        if depths.get(filename, 0):
            depths[filename] += 1
            try:
                yield
            finally:
                depths[filename] -= 1
            return
        with self._process_file_lock(filename, blocking=blocking):
            depths[filename] = 1
            try:
                yield
            finally:
                depths.pop(filename, None)

    @contextmanager
    def _process_file_lock(self, filename: str, *, blocking: bool) -> Iterator[None]:
        if self._create:
            os.makedirs(self.run_dir, exist_ok=True)
        with open(os.path.join(self.run_dir, filename), "a+b") as handle:
            if os.name == "nt":  # pragma: no cover - Windows-specific
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                while True:
                    handle.seek(0)
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as error:
                        if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                            raise
                        if not blocking:
                            raise StorageBusyError("Local workflow is already running") from error
                        time.sleep(0.05)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                except BlockingIOError as error:
                    raise StorageBusyError("Local workflow is already running") from error
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def lock(self, blocking: bool = True) -> Iterator[None]:
        with self._file_lock(".run.lock", blocking=blocking):
            try:
                yield
            finally:
                if self._lock_local.depths[".run.lock"] == 1:
                    self.close()

    def assemble_lock(self):
        return self._file_lock(".assemble.lock")

    @contextmanager
    def export_history_lock(self, blocking: bool = True) -> Iterator[None]:
        """Serialize publication, download opening and retention, not rendering."""
        with self._file_lock(".export-history.lock", blocking=blocking):
            yield

    def export_lock(self, export_id: int, blocking: bool = True):
        if not isinstance(export_id, int) or isinstance(export_id, bool) or export_id <= 0:
            raise ValueError("Invalid export identifier")
        digest = hashlib.sha256(str(export_id).encode("ascii")).hexdigest()
        return self._file_lock(f".export-{digest}.lock", blocking=blocking)

    @staticmethod
    def _key(key: str, *, prefix: bool = False) -> str:
        """Accept portable relative keys only, never filesystem-dependent aliases."""
        if not isinstance(key, str) or "\\" in key or "\0" in key or ":" in key:
            raise ValueError("Artifact key must be a portable relative path")
        value = key.removesuffix("/") if prefix else key
        if not value and prefix and not key:
            return key
        if not value or any(part in ("", ".", "..") for part in value.split("/")):
            raise ValueError("Artifact key must remain within the run")
        return key

    def read_artifact(self, key: str) -> Any | None:
        key = self._key(key)
        with self._mutex:
            if self._conn is None and not os.path.isfile(self.database_path):
                return None
            row = (
                self._connection()
                .execute("SELECT value FROM artifacts WHERE key=?", (key,))
                .fetchone()
            )
            return json.loads(row[0]) if row else None

    def write_artifact(self, key: str, value: Any) -> None:
        key = self._key(key)
        encoded = json.dumps(value, ensure_ascii=False)
        with self.state_lock():
            conn = self._connection()
            conn.execute("DELETE FROM artifact_records WHERE key=?", (key,))
            conn.execute(
                "INSERT INTO artifacts(key,value) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, encoded),
            )

    def delete_artifact(self, key: str) -> None:
        key = self._key(key)
        with self.state_lock():
            self._connection().execute("DELETE FROM artifacts WHERE key=?", (key,))

    def list_artifacts(self, prefix: str = "") -> list[str]:
        prefix = self._key(prefix, prefix=True)
        with self._mutex:
            if self._conn is None and not os.path.isfile(self.database_path):
                return []
            # A binary range uses the primary-key index, including literal '%' and '_'.
            rows = self._connection().execute(
                "SELECT key FROM artifacts WHERE key>=? AND key<? ORDER BY key",
                (prefix, prefix + chr(0x10FFFF)),
            )
            return [row[0] for row in rows]

    def append_artifact_record(self, key: str, record: dict) -> None:
        key = self._key(key)
        with self.state_lock():
            conn = self._connection()
            conn.execute(
                "INSERT INTO artifacts(key,value) VALUES (?, 'null') ON CONFLICT(key) DO NOTHING",
                (key,),
            )
            conn.execute(
                "INSERT INTO artifact_records(key,value) VALUES (?,?)",
                (key, json.dumps(record, ensure_ascii=False)),
            )

    def read_artifact_records(self, key: str) -> list[dict]:
        key = self._key(key)
        with self._mutex:
            if self._conn is None and not os.path.isfile(self.database_path):
                return []
            return [
                json.loads(row[0])
                for row in self._connection().execute(
                    "SELECT value FROM artifact_records WHERE key=? ORDER BY sequence", (key,)
                )
            ]

    def exists(self) -> bool:
        return isinstance(self.read_artifact("manifest.json"), dict)

    def begin_initialization(self, source_hash: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
            raise ValueError("Invalid source SHA-256 format")
        with self.state_lock():
            if self.exists():
                raise ValueError("Translation state is already initialized")
            previous = self.read_artifact("initializing.json") or {}
            same_source = previous.get("source_sha256") == source_hash
            parsed = self.read_artifact("parsed_document.json")
            keep_source = isinstance(parsed, dict) and parsed.get("source_sha256") == source_hash
            for key in self.list_artifacts():
                if keep_source and key in {"parsed_document.json", "preview.json"}:
                    continue
                if same_source and (key == "events.jsonl" or key.startswith("reviews/")):
                    continue
                self.delete_artifact(key)
            conn = self._connection()
            conn.execute("DELETE FROM glossary")
            conn.execute("DELETE FROM term_conflicts")
            self.write_artifact("initializing.json", {"source_sha256": source_hash})

    def finish_initialization(self) -> None:
        self.delete_artifact("initializing.json")

    def stage_document(self, doc: Document, *, source_hash: str | None = None) -> dict:
        digest = source_hash or source_sha256(doc.source_path)
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("Invalid source SHA-256 format")
        metadata = dict(doc.meta)
        annotations = metadata.pop("epub_annotation_contexts", None)
        with self.state_lock():
            if annotations:
                self.save_annotation_contexts(annotations)
            else:
                self.delete_artifact("annotation_contexts.json")
            for chapter in doc.chapters:
                self.save_chapter(chapter)
        return {
            "title": doc.title,
            "fmt": doc.fmt,
            "source_sha256": digest,
            "source_lang": doc.source_lang,
            "target_lang": doc.target_lang,
            "meta": metadata,
            "chapters": [
                {
                    "index": chapter.index,
                    "title": chapter.title,
                    "href": chapter.href,
                    "toc_entry_id": chapter.meta.get("toc_entry_id"),
                    "status": STATUS_PENDING,
                }
                for chapter in doc.chapters
            ],
        }

    def init_from_document(self, doc: Document) -> dict:
        with self.state_lock():
            manifest = self.stage_document(doc)
            manifest["initialized"] = True
            self.save_manifest(manifest)
            self.finish_initialization()
        return manifest

    def ensure_source_identity(self, input_path: str, *, actual_sha256: str | None = None) -> str:
        actual = actual_sha256 or source_sha256(input_path)
        RunStore._validate_source_identity(self.load_manifest(), actual)
        return actual

    def create_export_snapshot(self, *, actual_sha256: str) -> ExportSnapshotStore:
        with self._read_lock():
            manifest = self.load_manifest()
            RunStore._validate_source_identity(manifest, actual_sha256)
            entries = manifest.get("chapters")
            if not isinstance(entries, list):
                raise ValueError("Invalid chapter inventory in translation state")
            chapters = {}
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ValueError("Invalid chapter entry in translation state")
                index = entry.get("index")
                if not isinstance(index, int) or isinstance(index, bool):
                    raise ValueError("Invalid chapter index in translation state")
                if index in chapters:
                    raise ValueError(f"Duplicate chapter index in translation state: {index}")
                chapter = self.load_chapter(index)
                if chapter.index != index:
                    raise ValueError(f"Chapter index mismatch: {index}")
                chapters[index] = chapter
        return ExportSnapshotStore(self.run_dir, manifest, chapters)

    def save_manifest(self, manifest: dict) -> None:
        self.write_artifact("manifest.json", manifest)

    def load_manifest(self) -> dict:
        manifest = self.read_artifact("manifest.json")
        if manifest is None:
            raise FileNotFoundError(f"Translation state is not initialized: {self.database_path}")
        return manifest

    def set_chapter_status(self, ci: int, status: str) -> None:
        with self.state_lock():
            manifest = self.load_manifest()
            for chapter in manifest["chapters"]:
                if chapter["index"] == ci:
                    chapter["status"] = status
                    break
            self.save_manifest(manifest)

    def pending_chapters(self) -> list[int]:
        return [
            chapter["index"]
            for chapter in self.load_manifest()["chapters"]
            if chapter["status"] != STATUS_DONE
        ]

    def set_chapter_review_status(self, ci: int, status: str) -> None:
        with self.state_lock():
            manifest = self.load_manifest()
            for chapter in manifest["chapters"]:
                if chapter["index"] == ci:
                    chapter["review_status"] = status
                    break
            self.save_manifest(manifest)

    def save_chapter(
        self, chapter: Chapter, *, revision_kind: Literal["manual"] | None = None
    ) -> None:
        if revision_kind not in (None, "manual"):
            raise ValueError("Invalid chapter revision kind")
        key = f"chapters/ch{chapter.index}.json"
        text = [
            segment for segment in chapter.segments if segment.source and segment.kind == "text"
        ]
        with self.state_lock():
            previous = self.read_artifact(key)
            self.write_artifact(key, chapter.model_dump())
            self._record_chapter_changes(chapter, previous, kind=revision_kind)
            self._connection().execute(
                "INSERT INTO chapter_statistics "
                "(chapter_index, artifact_key, title, meta, word_count, target_word_count) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(chapter_index) DO UPDATE SET "
                "title=excluded.title, meta=excluded.meta, word_count=excluded.word_count, "
                "target_word_count=excluded.target_word_count",
                (
                    chapter.index,
                    key,
                    chapter.title,
                    json.dumps(chapter.meta, ensure_ascii=False),
                    len(text),
                    sum(segment.target is not None for segment in text),
                ),
            )

    def chapter_statistics(self) -> list[dict]:
        """Read compact chapter summaries without loading any segment bodies.

        Counts match the API's segment counts, including intentionally empty targets.
        Manifest status and translated titles remain authoritative; review presentation
        is intentionally left to the caller.
        """
        if not self.exists():
            return []
        with self._read_lock():
            inventory = {
                entry["index"]: entry for entry in self.load_manifest().get("chapters", [])
            }
            result = []
            for row in self._connection().execute(
                "SELECT * FROM chapter_statistics ORDER BY chapter_index"
            ):
                entry = inventory.get(row["chapter_index"])
                if entry is None:
                    continue
                meta = json.loads(row["meta"])
                result.append(
                    {
                        "index": row["chapter_index"],
                        "title": row["title"],
                        "title_translated": self._translated_title(entry, meta),
                        "status": entry.get("status", STATUS_PENDING),
                        "review_status": entry.get("review_status", STATUS_PENDING),
                        "word_count": row["word_count"],
                        "target_word_count": row["target_word_count"],
                        "meta": meta,
                    }
                )
            return result

    def _record_chapter_changes(
        self, chapter: Chapter, previous: dict | None, *, kind: Literal["manual"] | None
    ) -> None:
        old = {
            segment["index"]: (segment.get("target"), segment.get("target_before_polish"))
            for segment in (previous or {}).get("segments", [])
        }
        changes = []
        timestamp = datetime.now().astimezone().isoformat()
        for segment in chapter.segments:
            before, old_polish = old.get(segment.index, (None, None))
            after, unpolished = segment.target, segment.target_before_polish
            if before == after:
                continue
            base = (f"chapters/ch{chapter.index}.json", chapter.index, segment.index)
            if (
                kind is None
                and unpolished is not None
                and unpolished != old_polish
                and unpolished != after
            ):
                if before != unpolished:
                    changes.append(
                        (
                            *base,
                            "translation" if before is None else "update",
                            before,
                            unpolished,
                            timestamp,
                        )
                    )
                changes.append((*base, "polish", unpolished, after, timestamp))
            else:
                changes.append(
                    (
                        *base,
                        kind or ("translation" if before is None else "update"),
                        before,
                        after,
                        timestamp,
                    )
                )
        self._connection().executemany(
            "INSERT INTO segment_revisions "
            "(artifact_key, chapter_index, segment_index, kind, previous_target, "
            "new_target, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            changes,
        )

    def load_segment_history(self, ci: int, si: int) -> list[dict]:
        """Return newest-first revisions, preserving NULL and empty target distinctions."""
        with self._read_lock():
            try:
                chapter = self.load_chapter(ci)
            except FileNotFoundError as error:
                raise KeyError("segment not found") from error
            segment = next((segment for segment in chapter.segments if segment.index == si), None)
            if segment is None:
                raise KeyError("segment not found")
            rows = (
                self._connection()
                .execute(
                    "SELECT * FROM segment_revisions WHERE chapter_index=? AND segment_index=? "
                    "ORDER BY created_at DESC, id DESC",
                    (ci, si),
                )
                .fetchall()
            )
            ordered = [
                (
                    row["created_at"],
                    row["id"],
                    {
                        "id": f"revision-{row['id']}",
                        "kind": row["kind"],
                        "before": row["previous_target"],
                        "after": row["new_target"],
                        "created_at": row["created_at"],
                    },
                )
                for row in rows
            ]
            # Event-only manual edits remain visible without duplicating recorded revisions.
            for row in self._connection().execute(
                "SELECT sequence,value FROM artifact_records WHERE key='events.jsonl' "
                "ORDER BY sequence"
            ):
                event = json.loads(row["value"])
                if (
                    event.get("event") == "manual_translation_edited"
                    and event.get("chapter") == ci
                    and event.get("index") == si
                    and not event.get("history_recorded")
                    and "before" in event
                    and "after" in event
                    and event["before"] != event["after"]
                ):
                    date = event.get("ts", "")
                    ordered.append(
                        (
                            date,
                            row["sequence"],
                            {
                                "id": f"event-{row['sequence']}",
                                "kind": "manual",
                                "before": event["before"],
                                "after": event["after"],
                                "created_at": date,
                            },
                        )
                    )
            history = [
                entry for _, _, entry in sorted(ordered, key=lambda row: row[:2], reverse=True)
            ]
            target, unpolished = segment.target, segment.target_before_polish
            if target is not None and (not history or history[0]["after"] != target):
                history.insert(
                    0,
                    {
                        "id": "current-snapshot",
                        "kind": "snapshot",
                        "before": None,
                        "after": target,
                        "created_at": None,
                    },
                )
            if unpolished is not None and not any(
                unpolished in (entry["before"], entry["after"]) for entry in history
            ):
                history.append(
                    {
                        "id": "before-polish",
                        "kind": "before_polish",
                        "before": None,
                        "after": unpolished,
                        "created_at": None,
                    }
                )
            return history

    def save_chapter_with_status(self, chapter: Chapter, status: str) -> None:
        with self.state_lock():
            self.save_chapter(chapter)
            self.set_chapter_status(chapter.index, status)

    @staticmethod
    def _translated_title(entry: dict, meta: dict) -> str | None:
        # Presence matters: explicit clearing must not revive an older chapter title.
        if "title_translated" in entry:
            return entry["title_translated"] or None
        return meta.get("title_translated") or None

    def load_chapter(self, ci: int) -> Chapter:
        if self._conn is None and not os.path.isfile(self.database_path):
            raise FileNotFoundError(f"Chapter not found: {ci}")
        with self._read_lock():
            value = self.read_artifact(f"chapters/ch{ci}.json")
            if value is None:
                raise FileNotFoundError(f"Chapter not found: {ci}")
            chapter = Chapter.model_validate(value)
            manifest = self.read_artifact("manifest.json") or {}
            entry = next(
                (entry for entry in manifest.get("chapters", []) if entry["index"] == ci), {}
            )
            translated = self._translated_title(entry, chapter.meta)
            if translated is not None:
                chapter.meta["title_translated"] = translated
            elif "title_translated" in entry:
                chapter.meta.pop("title_translated", None)
            return chapter

    def save_context(self, data: dict) -> None:
        self.write_artifact("context.json", data)

    def load_context(self) -> dict | None:
        return self.read_artifact("context.json")

    def save_annotation_contexts(self, data: dict) -> None:
        self.write_artifact("annotation_contexts.json", data)

    def load_annotation_contexts(self) -> dict | None:
        return self.read_artifact("annotation_contexts.json")

    def save_analysis(self, data: dict) -> None:
        self.write_artifact("analysis.json", data)

    def load_analysis(self) -> dict | None:
        return self.read_artifact("analysis.json")

    def save_report(self, data: dict) -> None:
        self.write_artifact("report.json", data)

    def load_report(self) -> dict | None:
        return self.read_artifact("report.json")

    def save_usage(self, data: dict) -> None:
        self.write_artifact("usage.json", data)

    def load_usage(self) -> dict | None:
        return self.read_artifact("usage.json")

    @classmethod
    def _usage_key(cls, relative: str) -> str:
        key = cls._key(relative)
        if key != "usage.json" and not re.fullmatch(r"reviews/review-[^/]+/usage\.json", key):
            raise ValueError("Invalid usage journal destination")
        return key

    def prepare_usage_commit(self, ledgers: dict[str, dict]) -> None:
        from ..llm.routing import identity

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
        from ..llm.routing import identity
        from ..llm.usage import validate_usage

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
            ledger = self.load_timing() or {"runs": []}
            runs = {run["id"]: run for run in ledger["runs"]}
            runs[record["id"]] = record
            ledger = {
                "total_seconds": sum(run["elapsed_seconds"] for run in runs.values()),
                "runs": list(runs.values()),
            }
            self.write_artifact("timing.json", ledger)
            return ledger

    def load_timing(self) -> dict[str, Any] | None:
        return self.read_artifact("timing.json")

    def load_latest_review_result(self) -> dict | None:
        for key in reversed(self.list_artifacts("reviews/review-")):
            if re.fullmatch(r"reviews/review-[^/]+/result\.json", key):
                value = self.read_artifact(key)
                if isinstance(value, dict):
                    return value
        return None

    @staticmethod
    def batch_glossary_key(start_index: int, count: int) -> str:
        return f"{start_index}:{count}"

    def completed_batch_glossary_keys(self, chapter: int) -> set[str]:
        return {
            self.batch_glossary_key(row["start_index"], row["count"])
            for row in self.list_events(event_type="batch_glossary_extracted", limit=0)
            if row.get("chapter") == chapter
            and isinstance(row.get("start_index"), int)
            and isinstance(row.get("count"), int)
        }

    def log_event(self, event: str, **data: Any) -> None:
        self.append_artifact_record(
            "events.jsonl",
            {
                "ts": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                "event": event,
                **data,
            },
        )

    def list_events(self, *, event_type: str | None = None, limit: int = 200) -> list[dict]:
        with self._mutex:
            if self._conn is None and not os.path.isfile(self.database_path):
                return []
            rows = (
                self._connection()
                .execute(
                    """SELECT sequence,value FROM artifact_records
                WHERE key='events.jsonl'
                  AND (? IS NULL OR json_extract(value, '$.event')=?)
                ORDER BY sequence DESC LIMIT ?""",
                    (event_type, event_type, limit or -1),
                )
                .fetchall()
            )
        result = []
        for sequence, value in reversed(rows):
            record = json.loads(value)
            # Use the persisted identity, not a filtered page's position.
            record.update(_id=sequence, _ts=record.get("ts"))
            result.append(record)
        return result

    def _glossary(self) -> GlossaryStore:
        return GlossaryStore.from_connection(self._connection())

    def get_term(self, source: str) -> GlossaryTerm | None:
        with self._mutex:
            if self._conn is None and not os.path.isfile(self.database_path):
                return None
            return self._glossary().get_term(source)

    def upsert_term(self, term: GlossaryTerm, chapter: int | None = None) -> str:
        with self.state_lock():
            return self._glossary().upsert_term(term, chapter)

    def all_terms(self) -> list[GlossaryTerm]:
        with self._mutex:
            if self._conn is not None:
                return self._glossary().all_terms()
            if not os.path.isfile(self.database_path):
                return []
            return GlossaryStore.load_terms_readonly(self.database_path)

    @staticmethod
    def terms_in(terms: list[GlossaryTerm], text: str) -> list[GlossaryTerm]:
        return GlossaryStore.terms_in(terms, text)

    def terms_in_text(self, text: str) -> list[GlossaryTerm]:
        return self.terms_in(self.all_terms(), text)

    def resolve_term(self, source: str, target: str) -> bool:
        with self.state_lock():
            return self._glossary().resolve_term(source, target)

    def delete_term(self, source: str) -> bool:
        with self.state_lock():
            return (
                self._connection()
                .execute("DELETE FROM glossary WHERE source=?", (source,))
                .rowcount
                > 0
            )

    def update_term(self, source: str, term: GlossaryTerm) -> bool:
        """Apply an explicit edit in place, retaining prompt order and first chapter."""
        with self.state_lock():
            cursor = self._connection().execute(
                "UPDATE glossary SET source=?, target=?, reading=?, type=?, gender=?, "
                "aliases=?, note=?, status='ok', updated_at=? WHERE source=?",
                (
                    term.source,
                    term.target,
                    term.reading,
                    term.type,
                    term.gender,
                    json.dumps(term.aliases, ensure_ascii=False),
                    term.note,
                    time.time(),
                    source,
                ),
            )
            if not cursor.rowcount:
                return False
            self._connection().execute(
                "UPDATE term_conflicts SET source=?, resolved=1 WHERE source=?",
                (term.source, source),
            )
            return True

    def mark_conflicts_resolved(self, source: str) -> None:
        with self.state_lock():
            self._glossary().mark_conflicts_resolved(source)

    def open_conflicts(self) -> list[dict]:
        with self._mutex:
            if self._conn is None and not os.path.isfile(self.database_path):
                return []
            return self._glossary().open_conflicts()

    def stats(self) -> dict[str, int]:
        with self._mutex:
            if self._conn is None and not os.path.isfile(self.database_path):
                return {"terms": 0, "open_conflicts": 0}
            return self._glossary().stats()
