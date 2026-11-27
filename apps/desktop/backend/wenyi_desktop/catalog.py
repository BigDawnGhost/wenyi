"""Identify the current Desktop catalog before any store adds tables or writes."""

from __future__ import annotations

import sqlite3
from pathlib import Path

APPLICATION_ID = 0x57594454  # WYDT
FORMAT_VERSION = 2
_FORMAT_ERROR = (
    "This is not a current-format Desktop catalog. "
    "Choose a new Desktop data directory; legacy data is not migrated."
)


class UnsupportedCatalogFormat(RuntimeError):
    """The SQLite catalog is not in a format this Desktop engine admits."""


def _identity(connection: sqlite3.Connection) -> tuple[int, int]:
    application = connection.execute("PRAGMA application_id").fetchone()[0]
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    return application, version


def require_current(database: Path) -> None:
    """A writer child may use an admitted catalog but must not create or rebuild it."""
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        if _identity(connection) != (APPLICATION_ID, FORMAT_VERSION):
            raise UnsupportedCatalogFormat(_FORMAT_ERROR)
    finally:
        connection.close()


def initialize(database: Path) -> None:
    """Accept a current catalog or initialize an empty one, never upgrade an old one."""
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database, timeout=5)
    try:
        connection.execute("BEGIN IMMEDIATE")
        application, version = _identity(connection)
        if (application, version) == (APPLICATION_ID, FORMAT_VERSION):
            return
        populated = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchone()
        if populated or application or version:
            raise UnsupportedCatalogFormat(_FORMAT_ERROR)
        connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={FORMAT_VERSION}")
        connection.commit()
    finally:
        connection.close()
