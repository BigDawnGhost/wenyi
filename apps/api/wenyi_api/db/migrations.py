"""Versioned PostgreSQL schema migrations.

The schema evolves through ordered Migration records persisted in the
schema_migrations table. Fresh databases start at the baseline migration;
databases created before versioning existed (a bare schema.sql run) are
detected by the missing version rows and upgraded to the idempotent baseline
without touching existing data. Startup refuses a database whose recorded
schema is newer than the migrations shipped with the application.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Any

_BASELINE_SQL = (pathlib.Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")

_VERSION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""

_SELECT_VERSIONS_SQL = "SELECT version FROM schema_migrations"
_RECORD_VERSION_SQL = "INSERT INTO schema_migrations(version) VALUES (%s)"


@dataclass(frozen=True)
class Migration:
    """One ordered schema step with a positive, strictly increasing version id."""

    version: int
    sql: str


MIGRATIONS: tuple[Migration, ...] = (Migration(version=1, sql=_BASELINE_SQL),)

LATEST_VERSION = MIGRATIONS[-1].version


class SchemaVersionError(RuntimeError):
    """The database schema is newer than the migrations shipped with this build."""


def validate_migration_order(migrations: tuple[Migration, ...] = MIGRATIONS) -> None:
    """Fail fast when versions are duplicated, unordered or not positive."""
    versions = [migration.version for migration in migrations]
    if not versions or versions[0] < 1 or versions != sorted(set(versions)):
        raise SchemaVersionError("migration versions must be positive and strictly increasing")


def applied_versions(conn: Any) -> set[int]:
    """Read recorded versions, creating the version table when absent."""
    conn.execute(_VERSION_TABLE_SQL)
    rows = conn.execute(_SELECT_VERSIONS_SQL).fetchall()
    return {int(row[0]) for row in rows}


def run_migrations(conn: Any, migrations: tuple[Migration, ...] = MIGRATIONS) -> list[int]:
    """Apply pending migrations in order and return the recorded versions.

    Statements run inside the caller's transaction so an interrupted upgrade
    rolls back completely and the next startup retries from the same state.
    A database recorded ahead of LATEST_VERSION is rejected instead of guessed
    at, and legacy databases (tables without version rows) receive the
    idempotent baseline, which only adds missing objects.
    """
    validate_migration_order(migrations)
    known = applied_versions(conn)
    if known and max(known) > LATEST_VERSION:
        raise SchemaVersionError(
            f"database schema v{max(known)} is newer than application schema "
            f"v{LATEST_VERSION}; upgrade the application before connecting"
        )
    pending = [migration for migration in migrations if migration.version not in known]
    for migration in pending:
        conn.execute(migration.sql)
        conn.execute(_RECORD_VERSION_SQL, (migration.version,))
    return sorted(known | {migration.version for migration in pending})
