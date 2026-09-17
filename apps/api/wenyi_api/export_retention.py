"""Publish completed exports and retain a bounded, project-scoped download history."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import BinaryIO

from psycopg import Connection
from psycopg_pool import ConnectionPool

EXPORT_LIMIT = 5
log = logging.getLogger(__name__)


def export_history_lock(connection: Connection, pid: str, *, shared: bool = False) -> None:
    """Protect publication/deletion and the opening of download file handles."""
    statement = (
        "SELECT pg_advisory_xact_lock_shared(hashtextextended(%s,0))"
        if shared
        else "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))"
    )
    connection.execute(statement, (f"wenyi:export-history:{pid}",))


def export_path(data_dir: str, pid: str, stored: str) -> Path | None:
    """Reject paths and symlinks outside this project's generated export tree."""
    root = Path(data_dir).resolve()
    directory = root / pid / "exports"
    candidate = root / stored
    resolved = candidate.resolve()
    if (
        directory.resolve() != directory
        or resolved != candidate.absolute()
        or not resolved.is_relative_to(directory)
        or resolved == directory
    ):
        return None
    return resolved


def publish_export(
    pool: ConnectionPool, pid: str, export_id: int, output: str, *, data_dir: str
) -> None:
    file = export_path(data_dir, pid, str(Path(output).absolute()))
    if file is None or not file.is_file():
        raise ValueError("Export output must be a file in the project's export directory")
    stored = file.relative_to(Path(data_dir).resolve()).as_posix()
    size = file.stat().st_size
    with pool.connection() as conn:
        export_history_lock(conn, pid)
        published = conn.execute(
            """UPDATE exports SET status='done', path=%s, size=%s, error=NULL,
                   completed_at=clock_timestamp()
               WHERE id=%s AND project_id=%s RETURNING id""",
            (stored, size, export_id, pid),
        ).fetchone()
        if published is None:
            raise ValueError("Export record does not belong to this project")
        expired = conn.execute(
            """SELECT id,path,format FROM exports
               WHERE project_id=%s AND status='done' AND path IS NOT NULL
               ORDER BY COALESCE(completed_at,created_at) DESC,id DESC OFFSET %s""",
            (pid, EXPORT_LIMIT),
        ).fetchall()
        for old_id, old_path, fmt in expired:
            old_file = export_path(data_dir, pid, old_path)
            if old_file is None:
                log.warning("Refusing to remove export %s outside its owned directory", old_id)
                continue
            try:
                if fmt == "html":
                    assets = old_file.with_name(f"{old_file.stem}.assets")
                    if assets.exists():
                        if export_path(data_dir, pid, str(assets)) is None:
                            raise OSError("Export assets resolve outside their owned directory")
                        shutil.rmtree(assets)
                old_file.unlink(missing_ok=True)
            except OSError:
                # Retry at the next successful publication without failing the new export.
                log.warning("Could not remove expired export %s", old_id, exc_info=True)
                continue
            conn.execute("DELETE FROM exports WHERE id=%s AND project_id=%s", (old_id, pid))


def open_export(
    pool: ConnectionPool, pid: str, export_id: int, *, data_dir: str
) -> tuple[BinaryIO, Path]:
    """Open before releasing the shared lock so later eviction cannot interrupt a download."""
    with pool.connection() as conn:
        export_history_lock(conn, pid, shared=True)
        row = conn.execute(
            "SELECT path FROM exports WHERE id=%s AND project_id=%s AND status='done'",
            (export_id, pid),
        ).fetchone()
        file = export_path(data_dir, pid, row[0]) if row and row[0] else None
        if file is None or not file.is_file():
            raise FileNotFoundError("Completed export not found")
        return file.open("rb"), file
