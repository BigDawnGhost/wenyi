"""Version-one SQLite schema for snapshots, operations, and the event outbox."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ...workflow.repository import (
    UnsupportedWorkflowRepositorySchema,
    WorkflowRepositoryCorruption,
    WorkflowRepositoryError,
)
from .connection import open_connection, write_transaction

SCHEMA_VERSION = 1

# DDL is executed statement by statement because sqlite3.executescript() may
# commit implicitly, which would break the atomic schema-bootstrap boundary.
_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE workflow_snapshots (
        workflow_id TEXT PRIMARY KEY NOT NULL,
        workflow_schema_version INTEGER NOT NULL
            CHECK (typeof(workflow_schema_version) = 'integer' AND workflow_schema_version > 0),
        revision INTEGER NOT NULL
            CHECK (typeof(revision) = 'integer' AND revision >= 0),
        state_json BLOB NOT NULL CHECK (typeof(state_json) = 'blob'),
        state_sha256 TEXT NOT NULL
            CHECK (
                typeof(state_sha256) = 'text'
                AND length(state_sha256) = 64
                AND state_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
        created_at_ms INTEGER NOT NULL
            CHECK (typeof(created_at_ms) = 'integer' AND created_at_ms >= 0),
        updated_at_ms INTEGER NOT NULL
            CHECK (
                typeof(updated_at_ms) = 'integer'
                AND updated_at_ms >= created_at_ms
            )
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE workflow_operations (
        workflow_id TEXT NOT NULL,
        operation_id TEXT NOT NULL,
        committed_revision INTEGER NOT NULL
            CHECK (typeof(committed_revision) = 'integer' AND committed_revision > 0),
        patch_fingerprint TEXT NOT NULL
            CHECK (
                typeof(patch_fingerprint) = 'text'
                AND length(patch_fingerprint) = 64
                AND patch_fingerprint NOT GLOB '*[^0-9a-f]*'
            ),
        event_count INTEGER NOT NULL
            CHECK (typeof(event_count) = 'integer' AND event_count >= 0),
        committed_at_ms INTEGER NOT NULL
            CHECK (typeof(committed_at_ms) = 'integer' AND committed_at_ms >= 0),
        PRIMARY KEY (workflow_id, operation_id),
        UNIQUE (workflow_id, committed_revision),
        FOREIGN KEY (workflow_id)
            REFERENCES workflow_snapshots(workflow_id) ON DELETE RESTRICT
    ) WITHOUT ROWID
    """,
    """
    CREATE TABLE workflow_outbox (
        outbox_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        workflow_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        operation_id TEXT NOT NULL,
        event_index INTEGER NOT NULL
            CHECK (typeof(event_index) = 'integer' AND event_index >= 0),
        event_json BLOB NOT NULL CHECK (typeof(event_json) = 'blob'),
        event_sha256 TEXT NOT NULL
            CHECK (
                typeof(event_sha256) = 'text'
                AND length(event_sha256) = 64
                AND event_sha256 NOT GLOB '*[^0-9a-f]*'
            ),
        created_at_ms INTEGER NOT NULL
            CHECK (typeof(created_at_ms) = 'integer' AND created_at_ms >= 0),
        leased_by TEXT,
        lease_token TEXT,
        lease_expires_at_ms INTEGER,
        delivery_attempt INTEGER NOT NULL DEFAULT 0
            CHECK (typeof(delivery_attempt) = 'integer' AND delivery_attempt >= 0),
        acked_at_ms INTEGER,
        UNIQUE (workflow_id, event_id),
        UNIQUE (workflow_id, operation_id, event_index),
        FOREIGN KEY (workflow_id, operation_id)
            REFERENCES workflow_operations(workflow_id, operation_id) ON DELETE RESTRICT,
        CHECK (
            (
                leased_by IS NULL
                AND lease_token IS NULL
                AND lease_expires_at_ms IS NULL
                AND delivery_attempt = 0
            )
            OR
            (
                leased_by IS NOT NULL
                AND lease_token IS NOT NULL
                AND lease_expires_at_ms IS NOT NULL
                AND delivery_attempt > 0
            )
        ),
        CHECK (
            acked_at_ms IS NULL
            OR (
                typeof(acked_at_ms) = 'integer'
                AND acked_at_ms >= 0
                AND lease_token IS NOT NULL
            )
        )
    )
    """,
    """
    CREATE INDEX workflow_outbox_pending_idx
    ON workflow_outbox (COALESCE(lease_expires_at_ms, 0), outbox_sequence)
    WHERE acked_at_ms IS NULL
    """,
)

_EXPECTED_COLUMNS = {
    "workflow_snapshots": (
        "workflow_id",
        "workflow_schema_version",
        "revision",
        "state_json",
        "state_sha256",
        "created_at_ms",
        "updated_at_ms",
    ),
    "workflow_operations": (
        "workflow_id",
        "operation_id",
        "committed_revision",
        "patch_fingerprint",
        "event_count",
        "committed_at_ms",
    ),
    "workflow_outbox": (
        "outbox_sequence",
        "workflow_id",
        "event_id",
        "operation_id",
        "event_index",
        "event_json",
        "event_sha256",
        "created_at_ms",
        "leased_by",
        "lease_token",
        "lease_expires_at_ms",
        "delivery_attempt",
        "acked_at_ms",
    ),
}

_EXPECTED_SCHEMA_OBJECTS = (
    ("table", "workflow_snapshots", "workflow_snapshots", _SCHEMA_STATEMENTS[0]),
    ("table", "workflow_operations", "workflow_operations", _SCHEMA_STATEMENTS[1]),
    ("table", "workflow_outbox", "workflow_outbox", _SCHEMA_STATEMENTS[2]),
    ("index", "workflow_outbox_pending_idx", "workflow_outbox", _SCHEMA_STATEMENTS[3]),
)


def initialize_database(
    database_path: Path,
    *,
    busy_timeout_seconds: float,
    busy_timeout_ms: int,
) -> None:
    """Create schema v1 atomically or verify an existing supported database."""
    with open_connection(
        database_path,
        busy_timeout_seconds=busy_timeout_seconds,
        busy_timeout_ms=busy_timeout_ms,
        require_wal=False,
    ) as connection:
        # Reject future versions before changing the persistent journal mode.
        _require_supported_version(_read_user_version(connection))
        journal_mode = str(connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]).lower()
        if journal_mode != "wal":
            raise WorkflowRepositoryError(
                f"SQLite refused WAL mode for workflow repository: {journal_mode!r}"
            )

        with write_transaction(connection):
            # Another initializer may have completed while this connection
            # waited for the writer; always re-read the version under the lock.
            current_version = _read_user_version(connection)
            _require_supported_version(current_version)
            if current_version == 0:
                _create_pristine_schema(connection)
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            verify_database_schema(connection)


def verify_database_schema(connection: sqlite3.Connection) -> None:
    """Verify the exact supported schema inside the caller's transaction.

    Initialization alone is not sufficient because a long-lived repository
    object opens a new connection for every operation.  Write callers invoke
    this after ``BEGIN IMMEDIATE`` so no other connection can install a trigger
    or weaken the schema between verification and the protected write.
    """
    current_version = _read_user_version(connection)
    _require_supported_version(current_version)
    if current_version != SCHEMA_VERSION:
        raise WorkflowRepositoryCorruption(
            f"workflow repository schema marker is {current_version}, expected {SCHEMA_VERSION}"
        )
    _verify_schema_layout(connection)


def _create_pristine_schema(connection: sqlite3.Connection) -> None:
    """Create reserved tables only in an otherwise unversioned namespace."""
    reserved_names = tuple(_EXPECTED_COLUMNS)
    placeholders = ",".join("?" for _ in reserved_names)
    existing = connection.execute(
        f"SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ({placeholders})",
        reserved_names,
    ).fetchall()
    if existing:
        names = sorted(str(row[0]) for row in existing)
        raise WorkflowRepositoryCorruption(
            f"unversioned database already contains workflow tables: {names}"
        )
    for statement in _SCHEMA_STATEMENTS:
        connection.execute(statement)


def _verify_schema_layout(connection: sqlite3.Connection) -> None:
    """Fail closed unless schema-v1 objects exactly match their versioned SQL."""
    # sqlite_schema retains each original CREATE statement.  Normalizing only
    # whitespace verifies PK/UNIQUE/FK/CHECK, WITHOUT ROWID, AUTOINCREMENT, and
    # the partial-index predicate as one versioned signature.
    for expected_type, object_name, table_name, expected_sql in _EXPECTED_SCHEMA_OBJECTS:
        row = connection.execute(
            "SELECT type, tbl_name, sql FROM sqlite_master WHERE name = ?",
            (object_name,),
        ).fetchone()
        if row is None or row[0] != expected_type or row[1] != table_name:
            raise WorkflowRepositoryCorruption(
                f"workflow schema object {object_name!r} is missing or has the wrong type"
            )
        if type(row[2]) is not str or _normalize_schema_sql(row[2]) != _normalize_schema_sql(
            expected_sql
        ):
            raise WorkflowRepositoryCorruption(
                f"workflow schema object {object_name!r} does not match schema v1"
            )

    # Schema v1 owns no triggers.  A trigger could otherwise rewrite an event
    # after INSERT and before the adapter's post-write audit.
    reserved_tables = tuple(_EXPECTED_COLUMNS)
    placeholders = ",".join("?" for _ in reserved_tables)
    triggers = connection.execute(
        f"SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name IN ({placeholders})",
        reserved_tables,
    ).fetchall()
    if triggers:
        trigger_names = sorted(str(row[0]) for row in triggers)
        raise WorkflowRepositoryCorruption(
            f"workflow schema contains unexpected triggers: {trigger_names}"
        )

    # Keep a direct column-order diagnostic even though the SQL signature
    # already proves the complete table and index definitions.
    for table_name, expected_columns in _EXPECTED_COLUMNS.items():
        rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        actual_columns = tuple(str(row[1]) for row in rows)
        if actual_columns != expected_columns:
            raise WorkflowRepositoryCorruption(
                f"workflow table {table_name!r} has unexpected columns: {actual_columns}"
            )


def _normalize_schema_sql(value: str) -> str:
    """Remove insignificant whitespace and a trailing terminator from CREATE SQL."""
    return " ".join(value.strip().removesuffix(";").split())


def _read_user_version(connection: sqlite3.Connection) -> int:
    """Read the repository schema marker as a plain integer."""
    value = connection.execute("PRAGMA user_version").fetchone()[0]
    if type(value) is not int or value < 0:
        raise WorkflowRepositoryCorruption("SQLite user_version is invalid")
    return value


def _require_supported_version(version: int) -> None:
    """Reject future schemas instead of guessing a downgrade path."""
    if version > SCHEMA_VERSION:
        raise UnsupportedWorkflowRepositorySchema(
            f"workflow repository schema {version} is newer than supported {SCHEMA_VERSION}"
        )


__all__ = ["SCHEMA_VERSION", "initialize_database", "verify_database_schema"]
