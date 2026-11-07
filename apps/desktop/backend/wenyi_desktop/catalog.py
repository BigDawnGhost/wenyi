"""Identify the current Desktop catalog before any store adds tables or writes."""

from __future__ import annotations

import sqlite3
from pathlib import Path

APPLICATION_ID = 0x57594454  # WYDT
FORMAT_VERSION = 2


def initialize(database: Path) -> None:
    """Accept a current catalog or initialize an empty one, never upgrade an old one."""
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database, timeout=5)
    try:
        connection.execute("BEGIN IMMEDIATE")
        application = connection.execute("PRAGMA application_id").fetchone()[0]
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if (application, version) == (APPLICATION_ID, FORMAT_VERSION):
            return
        populated = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchone()
        if populated or application or version:
            raise RuntimeError(
                "This is not a current-format Desktop catalog. "
                "Choose a new Desktop data directory; legacy data is not migrated."
            )
        connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={FORMAT_VERSION}")
        connection.commit()
    finally:
        connection.close()
