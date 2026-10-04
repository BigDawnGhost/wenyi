"""Independent Desktop catalog; domain state belongs only to each project's Storage.

Catalog transactions never span model calls or domain writes. A saved manifest is
the authority for initialization; catalog jobs describe scheduling, not translations.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any, BinaryIO, Iterator

from fastapi import HTTPException
from wenyi_core.config import Config

if TYPE_CHECKING:
    from wenyi_core.storage.sqlite import SqliteStorage


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LocalBackend:
    def __init__(self, workspace: str, config_path: str | None = None) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.config_path = config_path
        self.database = self.workspace / "local.sqlite3"
        # Releasing memory must not depend on an available project directory.
        self._export_leases_lock = Lock()
        self._export_leases: dict[int, int] = {}
        with self.transaction() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, document TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    run_id TEXT NOT NULL UNIQUE, arq_job_id TEXT NOT NULL UNIQUE,
                    document TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS exports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    document TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS application_settings (
                    id INTEGER PRIMARY KEY CHECK(id=1), document TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS progress (
                    id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
                    document TEXT NOT NULL);
            """)

    @contextmanager
    def transaction(
        self, connection: sqlite3.Connection | None = None, *, write: bool = True
    ) -> Iterator[sqlite3.Connection]:
        if connection is not None:
            yield connection
            return
        conn = sqlite3.connect(self.database, timeout=5)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            if write and conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            if not write:
                conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield conn
            conn.commit()
        except sqlite3.OperationalError as error:
            conn.rollback()
            if "locked" in str(error).lower() or "busy" in str(error).lower():
                raise HTTPException(409, "Local workspace is busy; retry the operation") from error
            raise
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def project_dir(self, pid: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", pid):
            raise ValueError("Invalid project ID")
        return self.workspace / "projects" / pid

    def storage_for(self, pid: str, *, create: bool = True) -> SqliteStorage:
        from wenyi_core.storage.sqlite import SqliteStorage

        return SqliteStorage(str(self.project_dir(pid)), create=create)

    def defaults(self) -> Config:
        # Config() uses built-in defaults, never the CLI's cwd config.yaml.
        return Config.load(self.config_path) if self.config_path else Config()

    def _get(self, table, identity, connection=None):
        with self.transaction(connection, write=False) as conn:
            row = conn.execute(f"SELECT document FROM {table} WHERE id=?", (identity,)).fetchone()
        return json.loads(row[0]) if row else None

    def _put(self, table, identity, document, connection=None):
        with self.transaction(connection) as conn:
            conn.execute(
                f"UPDATE {table} SET document=? WHERE id=?", (json.dumps(document), identity)
            )

    def _list(self, table, *, pid=None, connection=None):
        with self.transaction(connection, write=False) as conn:
            rows = conn.execute(
                f"SELECT document FROM {table}"
                + (" WHERE project_id=?" if pid is not None else "")
                + " ORDER BY id DESC",
                (pid,) if pid is not None else (),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def create_project(
        self,
        name: str,
        source_lang: str,
        target_lang: str,
        strategy: dict,
        *,
        project_id: str | None = None,
        source: dict | None = None,
        config: dict | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> str:
        pid = project_id or uuid.uuid4().hex[:16]
        document = {
            "id": pid,
            "name": name,
            "source_lang": source_lang,
            "target_lang": target_lang,
            "strategy": strategy,
            "status": "uploaded" if source else "created",
            "title": None,
            "fmt": None,
            "book_title": None,
            "source_path": None,
            "source_sha256": None,
            "source_meta": {},
            "initialized": False,
            "error": None,
            "config": config or {},
            "created_at": _now(),
            **(source or {}),
        }
        with self.transaction(connection) as conn:
            conn.execute("INSERT INTO projects VALUES (?,?)", (pid, json.dumps(document)))
        return pid

    def get_project(self, pid: str) -> dict | None:
        project = self._get("projects", pid)
        if project is not None:
            storage = self.storage_for(pid, create=False)
            try:
                if storage.exists():
                    manifest = storage.load_manifest()
                    project.update(
                        initialized=True,
                        title=manifest.get("title"),
                        fmt=manifest.get("fmt", project["fmt"]),
                        source_lang=manifest.get("source_lang", project["source_lang"]),
                        target_lang=manifest.get("target_lang", project["target_lang"]),
                    )
            finally:
                storage.close()
            project.pop("updated_at", None)
        return project

    def list_projects(self) -> list[dict]:
        rows = sorted(self._list("projects"), key=lambda row: row["created_at"], reverse=True)
        keys = ("id", "name", "title", "fmt", "source_lang", "target_lang", "status", "created_at")
        return [
            {key: project[key] for key in keys}
            for row in rows
            if (project := self.get_project(row["id"])) is not None
        ]

    def update_project(
        self, pid: str, *, connection: sqlite3.Connection | None = None, **fields: Any
    ) -> None:
        with self.transaction(connection) as conn:
            project = self._get("projects", pid, conn)
            if project is None:
                raise KeyError(pid)
            project.update(fields, updated_at=_now())
            self._put("projects", pid, project, conn)

    def set_project_status(self, pid: str, status: str, *, error: str | None = None) -> None:
        self.update_project(pid, status=status, error=error)

    def set_project_strategy(
        self, pid: str, strategy: dict, *, connection: sqlite3.Connection | None = None
    ) -> None:
        self.update_project(pid, strategy=strategy, connection=connection)

    def set_project_config(
        self, pid: str, config: dict, *, connection: sqlite3.Connection | None = None
    ) -> None:
        self.update_project(pid, config=config, connection=connection)

    def get_project_config(self, pid: str) -> dict:
        project = self._get("projects", pid)
        if project is None:
            raise KeyError(pid)
        return project["config"]

    def set_project_languages(
        self,
        pid: str,
        source_lang: str,
        target_lang: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        self.update_project(
            pid, source_lang=source_lang, target_lang=target_lang, connection=connection
        )

    def set_project_source(
        self, pid: str, source_path: str, book_title: str | None, **fields: Any
    ) -> None:
        self.update_project(
            pid,
            source_path=source_path,
            book_title=book_title,
            **{key: value for key, value in fields.items() if value is not None},
        )

    def delete_project(self, pid: str) -> None:
        # Deliberately retain source and generated files, just as the Web endpoint does.
        with self.transaction() as conn:
            if any(
                row["status"] in {"pending", "running"}
                for row in self._list("exports", pid=pid, connection=conn)
            ) or any(
                row["kind"] == "export" and row["status"] in {"queued", "running"}
                for row in self._list("jobs", pid=pid, connection=conn)
            ):
                raise HTTPException(409, "An export is active; wait for it before deleting")
            conn.execute("DELETE FROM projects WHERE id=?", (pid,))

    def create_job(
        self,
        pid: str,
        kind: str,
        arq_job_id: str,
        *,
        params: dict | None = None,
        config_snapshot: dict | None = None,
        run_id: str | None = None,
        status: str = "queued",
        connection: sqlite3.Connection | None = None,
    ) -> int:
        saved = dict(params or {})
        if config_snapshot is not None:
            saved["config_snapshot"] = config_snapshot
        run_id = run_id or uuid.uuid4().hex
        document = {
            "project_id": pid,
            "kind": kind,
            "status": status,
            "arq_job_id": arq_job_id,
            "run_id": run_id,
            "params": saved,
            "result": None,
            "error": None,
            "created_at": _now(),
            "updated_at": _now(),
        }
        with self.transaction(connection) as conn:
            cursor = conn.execute(
                "INSERT INTO jobs(project_id,run_id,arq_job_id,document) VALUES(?,?,?,?)",
                (pid, run_id, arq_job_id, json.dumps(document)),
            )
            identity = cursor.lastrowid
            assert identity is not None
            document["id"] = identity
            self._put("jobs", identity, document, conn)
        return identity

    def admit_job(
        self,
        pid: str,
        kind: str,
        arq_job_id: str,
        *,
        project_status: str,
        params: dict | None = None,
        config_snapshot: dict | None = None,
        run_id: str | None = None,
    ) -> int:
        """Commit scheduling identity and busy status together, outside domain state."""
        from wenyi_backend.dal import RUNNING_PROJECT_STATUSES

        with self.transaction() as connection:
            project = self._get("projects", pid, connection)
            if project is None:
                raise HTTPException(404, "project not found")
            if project["status"] in RUNNING_PROJECT_STATUSES:
                raise HTTPException(409, "project already has a running task")
            identity = self.create_job(
                pid,
                kind,
                arq_job_id,
                params=params,
                config_snapshot=config_snapshot,
                run_id=run_id,
                connection=connection,
            )
            self.update_project(pid, status=project_status, error=None, connection=connection)
            return identity

    def fail_admission(self, job_id: int, previous_status: str, error: str) -> bool:
        """Compensate a queue rejection, never a runner that has already claimed work."""
        with self.transaction() as connection:
            job = self._get("jobs", job_id, connection)
            if job is None or job["status"] != "queued" or job["kind"] == "export":
                return False
            latest = next(
                (
                    row
                    for row in self._list("jobs", pid=job["project_id"], connection=connection)
                    if row["kind"] != "export"
                ),
                None,
            )
            if latest is None or latest["id"] != job_id:
                return False
            job.update(status="error", error=error, updated_at=_now())
            self._put("jobs", job_id, job, connection)
            self.update_project(
                job["project_id"], status=previous_status, error=error, connection=connection
            )
            return True

    def admit_export(
        self, pid: str, fmt: str, options: dict, run_id: str, config_snapshot: dict
    ) -> tuple[int, int]:
        with self.transaction() as connection:
            export_id = self.create_export(pid, fmt, options, connection=connection)
            job_id = self.create_job(
                pid,
                "export",
                run_id,
                run_id=run_id,
                params={"export_id": export_id},
                config_snapshot=config_snapshot,
                connection=connection,
            )
            return export_id, job_id

    def fail_export_admission(self, job_id: int, error: str) -> bool:
        with self.transaction() as connection:
            job = self._get("jobs", job_id, connection)
            if job is None or job["kind"] != "export" or job["status"] != "queued":
                return False
            export_id = job["params"]["export_id"]
            export = self._get("exports", export_id, connection)
            if export is None or export["status"] != "pending":
                return False
            job.update(status="error", error=error, updated_at=_now())
            export.update(status="error", error=error)
            self._put("jobs", job_id, job, connection)
            self._put("exports", export_id, export, connection)
            return True

    def get_job(self, job_id: int) -> dict | None:
        return self._get("jobs", job_id)

    def get_job_by_arq_id(self, arq_job_id: str) -> dict | None:
        with self.transaction() as conn:
            row = conn.execute("SELECT id FROM jobs WHERE arq_job_id=?", (arq_job_id,)).fetchone()
            return self._get("jobs", row[0], conn) if row else None

    def list_jobs(self, pid: str) -> list[dict]:
        return self._list("jobs", pid=pid)

    def all_jobs(self) -> list[dict]:
        """Return durable jobs for startup recovery."""
        return self._list("jobs")

    def set_job_status(
        self,
        job_id: int,
        status: str,
        error: str | None = None,
        *,
        result: dict | None = None,
        expected_status: str | set[str] | None = None,
    ) -> bool:
        """Conditionally transition a job; return False for a stale runner."""
        with self.transaction() as conn:
            job = self._get("jobs", job_id, conn)
            if job is None:
                return False
            expected = {expected_status} if isinstance(expected_status, str) else expected_status
            if expected is not None and job["status"] not in expected:
                return False
            job.update(status=status, error=error, updated_at=_now())
            if result is not None:
                job["result"] = result
            self._put("jobs", job_id, job, conn)
        return True

    def latest_resumable_job(self, pid: str) -> dict | None:
        job = next((j for j in self.list_jobs(pid) if j["kind"] != "export"), None)
        return job if job and job["status"] in {"paused", "error", "interrupted"} else None

    def is_paused(self, pid: str) -> bool:
        return (self._get("projects", pid) or {}).get("status") in {"paused", "pausing"}

    def job_review_id(self, job_id: int) -> str | None:
        job = self.get_job(job_id)
        if not job or job["kind"] == "export":
            return None
        jobs = sorted(
            (row for row in self.list_jobs(job["project_id"]) if row["kind"] != "export"),
            key=lambda row: row["id"],
        )
        starts = [
            datetime.fromisoformat(row["created_at"]).astimezone(timezone.utc) for row in jobs
        ]
        store = self.storage_for(job["project_id"], create=False)
        try:
            events = store.list_events(limit=0)
        finally:
            store.close()
        for event in reversed(events):
            if event.get("event") not in {"review_started", "review_autofix_finished"}:
                continue
            raw = event.get("_ts")
            if not isinstance(raw, str) or not event.get("review_id"):
                continue
            try:
                date = datetime.fromisoformat(raw)
            except ValueError:
                continue
            if date.tzinfo is None:
                continue
            date = date.astimezone(timezone.utc)
            # Older events truncated fractions. Accept their one-second interval
            # only when it intersects one job window; never guess across runs.
            legacy = "." not in raw
            end = date + timedelta(seconds=1) if legacy else date
            owners = [
                row["id"]
                for index, row in enumerate(jobs)
                if (end > starts[index] if legacy else date >= starts[index])
                and (index + 1 == len(jobs) or date < starts[index + 1])
            ]
            if owners == [job_id]:
                return event["review_id"]
        return None

    def chapter_summaries(self, pid: str) -> list[dict]:
        from wenyi_backend.chapter_state import chapter_review_state

        store = self.storage_for(pid, create=False)
        try:
            if not store.exists():
                return []
            review = store.load_latest_review_result() or {}
            result = []
            for entry in store.load_manifest().get("chapters", []):
                chapter = store.load_chapter(entry["index"])
                state, current = chapter_review_state(
                    review, chapter.meta, entry.get("review_status")
                )
                segments = [s for s in chapter.text_segments if s.source]
                result.append(
                    {
                        "index": chapter.index,
                        "title": chapter.title,
                        "title_translated": entry.get("title_translated"),
                        "status": entry.get("status") or "pending",
                        "word_count": len(segments),
                        "target_word_count": sum(s.target is not None for s in segments),
                        "review_status": state or "pending",
                        "review_issue_count": sum(
                            issue.get("chapter") == chapter.index
                            for issue in review.get("issues", [])
                        )
                        if current
                        else 0,
                    }
                )
            return result
        finally:
            store.close()

    def total_word_count(self, pid: str) -> int:
        return sum(ch["word_count"] for ch in self.chapter_summaries(pid))

    def set_chapter_status(self, pid: str, chapter_index: int, status: str) -> None:
        store = self.storage_for(pid, create=False)
        try:
            store.set_chapter_status(chapter_index, status)
        finally:
            store.close()

    def create_export(
        self,
        pid: str,
        fmt: str,
        options: dict,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> int:
        document = {
            "project_id": pid,
            "format": fmt,
            "options": options,
            "status": "pending",
            "path": None,
            "size": None,
            "error": None,
            "created_at": _now(),
        }
        with self.transaction(connection) as conn:
            cursor = conn.execute(
                "INSERT INTO exports(project_id,document) VALUES(?,?)", (pid, json.dumps(document))
            )
            identity = cursor.lastrowid
            assert identity is not None
            document["id"] = identity
            self._put("exports", identity, document, conn)
        return identity

    def set_export_status(
        self,
        export_id: int,
        status: str,
        *,
        path: str | None = None,
        size: int | None = None,
        error: str | None = None,
    ) -> None:
        with self.transaction() as conn:
            export = self._get("exports", export_id, conn)
            if export and export["status"] not in {"done", "deleting"}:
                export.update(status=status, path=path, size=size, error=error)
                self._put("exports", export_id, export, conn)

    def list_exports(self, pid: str) -> list[dict]:
        return sorted(
            (row for row in self._list("exports", pid=pid) if row["status"] != "deleting"),
            key=lambda row: (row.get("completed_at") or row["created_at"], row["id"]),
            reverse=True,
        )[:5]

    def _export_history_lock(self, pid: str):
        from wenyi_core.storage.sqlite import SqliteStorage

        # Only the OS lock is needed; do not open or initialize the domain database.
        if not self.project_dir(pid).is_dir():
            raise FileNotFoundError("Project export directory is missing")
        storage = SqliteStorage(str(self.project_dir(pid)), create=False)
        return storage.export_history_lock()

    def publish_export(self, pid: str, export_id: int, output: str) -> None:
        from wenyi_backend.export_paths import EXPORT_LIMIT, export_path

        root = str(self.workspace / "projects")
        with self._export_history_lock(pid):
            file = export_path(root, pid, str(Path(output).absolute()))
            if file is None or not file.is_file():
                raise ValueError("Export output must be a file in the project's export directory")
            size = file.stat().st_size
            # Publish and retire atomically, before any destructive filesystem operation.
            with self.transaction() as conn:
                export = self._get("exports", export_id, conn)
                if export is None or export["project_id"] != pid or export["status"] == "deleting":
                    raise ValueError("Export record does not belong to this project's live history")
                export.update(
                    status="done",
                    path=file.relative_to(root).as_posix(),
                    size=size,
                    error=None,
                    completed_at=_now(),
                )
                self._put("exports", export_id, export, conn)
                completed = sorted(
                    (
                        row
                        for row in self._list("exports", pid=pid, connection=conn)
                        if row["status"] == "done" and row["path"]
                    ),
                    key=lambda row: (row.get("completed_at") or row["created_at"], row["id"]),
                    reverse=True,
                )
                for old in completed[EXPORT_LIMIT:]:
                    old["status"] = "deleting"
                    self._put("exports", old["id"], old, conn)
            self._cleanup_exports(pid)

    def recover_export_cleanup(self, pid: str) -> None:
        """Retry committed tombstones after an interrupted or failed file cleanup."""
        with self._export_history_lock(pid):
            self._cleanup_exports(pid)

    def _cleanup_exports(self, pid: str) -> None:
        """Called under the history lock, never inside a catalog transaction."""
        from wenyi_backend.export_paths import export_path, log

        root = str(self.workspace / "projects")
        for old in self._list("exports", pid=pid):
            with self._export_leases_lock:
                leased = bool(self._export_leases.get(old["id"], 0))
            if old["status"] != "deleting" or leased:
                continue
            try:
                old_file = export_path(root, pid, old["path"])
                if old_file is None:
                    raise OSError("Export resolves outside its owned directory")
                if old["format"] == "html":
                    assets = old_file.with_name(f"{old_file.stem}.assets")
                    if export_path(root, pid, str(assets)) is None:
                        raise OSError("Export assets resolve outside their owned directory")
                    if assets.exists():
                        shutil.rmtree(assets)
                old_file.unlink(missing_ok=True)
            except OSError:
                log.warning("Could not remove expired export %s", old["id"], exc_info=True)
                continue
            # Failure or process exit here leaves a non-downloadable, retryable tombstone.
            with self.transaction() as conn:
                conn.execute("DELETE FROM exports WHERE id=?", (old["id"],))

    def _release_export(self, pid: str, export_id: int) -> None:
        from wenyi_backend.export_paths import log

        with self._export_leases_lock:
            count = self._export_leases.get(export_id, 0)
            if count > 1:
                self._export_leases[export_id] = count - 1
            else:
                self._export_leases.pop(export_id, None)
        if count != 1:
            return
        # Never hold the memory lock while acquiring a project's filesystem lock.
        # Cleanup is retryable and must not replace the download's response outcome.
        try:
            with self._export_history_lock(pid):
                self._cleanup_exports(pid)
        except Exception:
            log.warning("Could not reclaim exports after closing %s", export_id, exc_info=True)

    def open_export(
        self, pid: str, export_id: int, *, bundle_html: bool = False
    ) -> tuple[BinaryIO, Path]:
        from wenyi_backend.export_paths import export_path

        from .local_export_files import ExportLease, html_bundle, open_regular

        with self._export_history_lock(pid):
            export = self._get("exports", export_id)
            file = (
                export_path(str(self.workspace / "projects"), pid, export["path"])
                if export
                and export["project_id"] == pid
                and export["status"] == "done"
                and export["path"]
                else None
            )
            if file is None or not file.is_file():
                raise FileNotFoundError("Completed export not found")
            # Keep owned assets alive until bundled, without a catalog transaction.
            if bundle_html and file.suffix == ".html":
                return html_bundle(file), file
            stream = open_regular(file)
            try:
                leased = ExportLease(stream, lambda: self._release_export(pid, export_id))
            except BaseException:
                stream.close()
                raise
            with self._export_leases_lock:
                self._export_leases[export_id] = self._export_leases.get(export_id, 0) + 1
            return leased, file

    def project_configs(
        self, connection: sqlite3.Connection | None = None
    ) -> list[tuple[str, dict]]:
        return [(row["id"], row["config"]) for row in self._list("projects", connection=connection)]

    def load_settings(self, connection: sqlite3.Connection | None = None) -> dict | None:
        return self._get("application_settings", 1, connection)

    def save_settings(self, document: dict, connection: sqlite3.Connection) -> None:
        connection.execute(
            "INSERT INTO application_settings VALUES(1,?) "
            "ON CONFLICT(id) DO UPDATE SET document=excluded.document",
            (json.dumps(document),),
        )

    def save_progress(self, pid: str, payload: dict) -> None:
        """Persist only the latest advisory progress; never acquire a domain lock."""
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO progress(id,document) VALUES(?,?) "
                "ON CONFLICT(id) DO UPDATE SET document=excluded.document",
                (pid, json.dumps(payload)),
            )

    def load_progress(self, pid: str) -> dict | None:
        return self._get("progress", pid)

    def close(self) -> None:
        """Connections are transaction-scoped; no application-wide pool remains open."""
