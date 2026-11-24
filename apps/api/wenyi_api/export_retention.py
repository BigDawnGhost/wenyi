"""Publish completed exports and retain a bounded, project-scoped download history."""

from __future__ import annotations

import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator

from psycopg import Connection
from psycopg_pool import ConnectionPool
from wenyi_backend.export_paths import EXPORT_LIMIT, export_path, log
from wenyi_core.storage.io_path import to_io_path


def export_history_lock(connection: Connection, pid: str, *, shared: bool = False) -> None:
    """Protect publication/deletion and the opening of download file handles."""
    statement = (
        "SELECT pg_advisory_xact_lock_shared(hashtextextended(%s,0))"
        if shared
        else "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))"
    )
    connection.execute(statement, (f"wenyi:export-history:{pid}",))


@contextmanager
def export_history_session_lock(
    pool: ConnectionPool, pid: str, *, blocking: bool = False
) -> Iterator[Connection]:
    """Exclude export admission/publication/opening without a transaction during cleanup."""
    key = f"wenyi:export-history:{pid}"
    with pool.connection() as connection:
        acquired = False
        try:
            if blocking:
                # pg_advisory_lock() returns void: reaching this line means the lock
                # is held. Reading the result as a boolean would misreport failure and
                # leave the lock attached to the pooled connection.
                connection.execute("SELECT pg_advisory_lock(hashtextextended(%s,0))", (key,))
                acquired = True
            else:
                row = connection.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s,0))", (key,)
                ).fetchone()
                acquired = bool(row and row[0])
            connection.commit()
            if not acquired:
                raise BlockingIOError("Project export history is busy")
            yield connection
        finally:
            if acquired:
                try:
                    connection.rollback()
                    connection.execute("SELECT pg_advisory_unlock(hashtextextended(%s,0))", (key,))
                    connection.commit()
                except BaseException:
                    # A pooled session must not retain the lock if unlock fails.
                    connection.close()
                    raise


def publish_export(
    pool: ConnectionPool,
    pid: str,
    export_id: int,
    output: str,
    *,
    data_dir: str,
    job_id: int | None = None,
) -> None:
    """Commit publication and retirement before touching any superseded files."""
    file = export_path(data_dir, pid, str(Path(output).absolute()))
    if file is None or not Path(to_io_path(file)).is_file():
        raise ValueError("Export output must be a file in the project's export directory")
    stored = file.relative_to(Path(data_dir).resolve()).as_posix()
    size = Path(to_io_path(file)).stat().st_size
    with pool.connection() as conn:
        export_history_lock(conn, pid)
        if job_id is not None:
            job = conn.execute(
                """UPDATE jobs SET status='done', error=NULL, updated_at=now()
                   WHERE id=%s AND project_id=%s AND kind='export' AND status='running'
                     AND params->'export_id'=to_jsonb(%s::bigint) RETURNING id""",
                (job_id, pid, export_id),
            ).fetchone()
            if job is None:
                raise ValueError("Export job is not running or does not own this export")
        published = conn.execute(
            """UPDATE exports SET status='done', path=%s, size=%s, error=NULL,
                   completed_at=clock_timestamp()
               WHERE id=%s AND project_id=%s AND status IN ('pending','running')
               RETURNING id""",
            (stored, size, export_id, pid),
        ).fetchone()
        if published is None:
            raise ValueError("Export record does not belong to this project's pending history")
        expired = conn.execute(
            """UPDATE exports SET status='deleting' WHERE id IN (
                   SELECT id FROM exports
                   WHERE project_id=%s AND status='done' AND path IS NOT NULL
                   ORDER BY COALESCE(completed_at,created_at) DESC,id DESC OFFSET %s
               ) RETURNING id""",
            (pid, EXPORT_LIMIT),
        ).fetchone()
    if expired:
        _try_cleanup(pool, pid, data_dir, blocking=True)


def recover_export_cleanup(pool: ConnectionPool, data_dir: str) -> None:
    """Find committed retirements even when the publishing worker has stopped."""
    with pool.connection() as conn:
        projects = conn.execute(
            "SELECT DISTINCT project_id FROM exports WHERE status='deleting'"
        ).fetchall()
    for (pid,) in projects:
        _try_cleanup(pool, pid, data_dir, blocking=False)


def _try_cleanup(pool: ConnectionPool, pid: str, data_dir: str, *, blocking: bool) -> None:
    try:
        _remove_expired_files(pool, pid, data_dir, blocking=blocking)
    except BlockingIOError:
        # Another operation owns this project's history; maintenance will retry.
        pass
    except Exception:
        log.warning("Could not reclaim retired exports for project %s", pid, exc_info=True)


def _remove_expired_files(pool: ConnectionPool, pid: str, data_dir: str, *, blocking: bool) -> None:
    with export_history_session_lock(pool, pid, blocking=blocking) as conn:
        expired = conn.execute(
            "SELECT id,path,format FROM exports WHERE project_id=%s AND status='deleting'",
            (pid,),
        ).fetchall()
        conn.commit()
        removed = []
        for old_id, old_path, fmt in expired:
            try:
                old_file = export_path(data_dir, pid, old_path)
                if old_file is None:
                    raise OSError("Export resolves outside its owned directory")
                if fmt == "html":
                    assets = old_file.with_name(f"{old_file.stem}.assets")
                    if export_path(data_dir, pid, str(assets)) is None:
                        raise OSError("Export assets resolve outside their owned directory")
                    if Path(to_io_path(assets)).exists():
                        shutil.rmtree(to_io_path(assets))
                Path(to_io_path(old_file)).unlink(missing_ok=True)
            except (OSError, RuntimeError):
                log.warning("Could not remove expired export %s", old_id, exc_info=True)
            else:
                removed.append(old_id)
        if removed:
            # Missing files are fine on retry after an interruption or a failed commit.
            conn.execute(
                "DELETE FROM exports WHERE project_id=%s AND id=ANY(%s) AND status='deleting'",
                (pid, removed),
            )
            conn.commit()


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
        if file is None or not Path(to_io_path(file)).is_file():
            raise FileNotFoundError("Completed export not found")
        return Path(to_io_path(file)).open("rb"), file
