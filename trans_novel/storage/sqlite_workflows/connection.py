"""Short-lived SQLite connections and explicit transaction boundaries.

The project supports Python 3.10, so this module intentionally uses
``isolation_level=None`` plus explicit ``BEGIN`` statements instead of the
newer ``Connection.autocommit`` API.  A repository instance stores only a
database path and configuration; connections are never shared across threads
or inherited as instance state across a process fork.
"""

from __future__ import annotations

import math
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ...workflow.repository import WorkflowRepositoryBusy, WorkflowRepositoryError

_MAX_SQLITE_TIMEOUT_MS = 2_147_483_647
_SQLITE_BUSY_PRIMARY_CODES = {5, 6}  # SQLITE_BUSY and SQLITE_LOCKED.


def prepare_database_path(path: str | Path) -> Path:
    """Return a dedicated local database path and create only its parent."""
    if not isinstance(path, (str, Path)):
        raise TypeError("path must be a string or pathlib.Path")
    if str(path) == ":memory:" or str(path).startswith("file:"):
        raise ValueError("workflow repository requires a persistent filesystem path")

    try:
        database_path = Path(path).resolve(strict=False)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        if database_path.exists() and not database_path.is_file():
            raise ValueError(f"workflow database path is not a file: {database_path}")
    except OSError as error:
        raise WorkflowRepositoryError(f"cannot prepare workflow database path: {path!s}") from error
    return database_path


def validate_busy_timeout(value: object) -> tuple[float, int]:
    """Normalize a positive timeout to seconds and SQLite-safe milliseconds."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise ValueError("busy_timeout_seconds must be a positive finite number")
    milliseconds = math.ceil(float(value) * 1000)
    if milliseconds > _MAX_SQLITE_TIMEOUT_MS:
        raise ValueError("busy_timeout_seconds exceeds SQLite's supported millisecond range")
    return float(value), max(1, milliseconds)


@contextmanager
def open_connection(
    database_path: Path,
    *,
    busy_timeout_seconds: float,
    busy_timeout_ms: int,
    require_wal: bool = True,
) -> Iterator[sqlite3.Connection]:
    """Open, configure, yield, and always close one repository connection."""
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            str(database_path),
            timeout=busy_timeout_seconds,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row

        # These settings are per connection.  FULL preserves the durability
        # contract; foreign keys make the normalized projections fail closed.
        connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise WorkflowRepositoryError("SQLite foreign-key enforcement is unavailable")
        if connection.execute("PRAGMA synchronous").fetchone()[0] != 2:
            raise WorkflowRepositoryError("SQLite synchronous=FULL could not be enabled")
        if require_wal:
            journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            if journal_mode != "wal":
                raise WorkflowRepositoryError(
                    f"workflow database is not using WAL mode: {journal_mode!r}"
                )
        yield connection
    except sqlite3.Error as error:
        raise translate_sqlite_error(error) from error
    finally:
        if connection is not None:
            error_already_propagating = sys.exc_info()[0] is not None
            try:
                connection.close()
            except sqlite3.Error as error:
                # Never replace an exception already propagating from the
                # transaction.  Without one, surface close failure normally.
                if not error_already_propagating:
                    raise translate_sqlite_error(error) from error


@contextmanager
def read_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """Hold a consistent WAL read snapshot across state and projection checks."""
    connection.execute("BEGIN")
    try:
        yield
    except BaseException:
        _rollback_best_effort(connection)
        raise
    else:
        try:
            connection.execute("COMMIT")
        except BaseException:
            _rollback_best_effort(connection)
            raise


@contextmanager
def write_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    """Acquire SQLite's single writer before reading data that will be changed."""
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        _rollback_best_effort(connection)
        raise
    else:
        try:
            connection.execute("COMMIT")
        except BaseException:
            _rollback_best_effort(connection)
            raise


def translate_sqlite_error(error: sqlite3.Error) -> WorkflowRepositoryError:
    """Map only lock contention to the retryable repository error."""
    code = getattr(error, "sqlite_errorcode", None)
    primary_code = code & 0xFF if isinstance(code, int) else None
    message = str(error).casefold()
    if primary_code in _SQLITE_BUSY_PRIMARY_CODES or "locked" in message or "busy" in message:
        return WorkflowRepositoryBusy("workflow repository remained busy until its timeout")
    return WorkflowRepositoryError("SQLite workflow repository operation failed")


def _rollback_best_effort(connection: sqlite3.Connection) -> None:
    """Rollback an open transaction; closing the connection is the final fallback."""
    if not connection.in_transaction:
        return
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        # The outer connection context always closes the handle, which also
        # abandons an uncommitted transaction if explicit rollback is broken.
        pass


__all__ = [
    "open_connection",
    "prepare_database_path",
    "read_transaction",
    "validate_busy_timeout",
    "write_transaction",
]
