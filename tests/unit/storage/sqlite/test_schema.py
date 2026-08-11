"""SQLite workflow schema versioning and dependency-isolation tests."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from trans_novel.storage.sqlite_workflows import SQLiteWorkflowRepository
from trans_novel.storage.sqlite_workflows.schema import SCHEMA_VERSION
from trans_novel.workflow import (
    UnsupportedWorkflowRepositorySchema,
    WorkflowRepositoryCorruption,
)


# Bootstrap assertions keep the repository database self-identifying and make
# accidental coupling to the legacy glossary schema immediately visible.
def test_schema_bootstrap_creates_versioned_workflow_tables(tmp_path: Path) -> None:
    """A fresh database must advertise schema v1 and only workflow-owned tables."""
    database_path = tmp_path / "workflows.sqlite3"
    SQLiteWorkflowRepository(database_path)

    with sqlite3.connect(database_path) as connection:
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    assert user_version == SCHEMA_VERSION
    assert str(journal_mode).lower() == "wal"
    assert {
        "workflow_snapshots",
        "workflow_operations",
        "workflow_outbox",
    } <= table_names
    assert "glossary" not in table_names


def test_future_schema_version_is_rejected_before_repository_use(tmp_path: Path) -> None:
    """Opening a newer database must stop instead of guessing a downgrade path."""
    database_path = tmp_path / "future.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")

    with pytest.raises(UnsupportedWorkflowRepositorySchema):
        SQLiteWorkflowRepository(database_path)


def test_schema_v1_rejects_same_named_but_wrong_partial_index(tmp_path: Path) -> None:
    """An index name alone cannot impersonate the versioned delivery predicate."""
    database_path = tmp_path / "wrong-index.sqlite3"
    SQLiteWorkflowRepository(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP INDEX workflow_outbox_pending_idx")
        connection.execute(
            "CREATE INDEX workflow_outbox_pending_idx ON workflow_outbox (outbox_sequence)"
        )

    with pytest.raises(WorkflowRepositoryCorruption, match="schema v1"):
        SQLiteWorkflowRepository(database_path)


def test_public_operation_rechecks_schema_marker_after_initialization(tmp_path: Path) -> None:
    """A reused repository object must not trust a marker checked only at startup."""
    database_path = tmp_path / "changed-marker.sqlite3"
    repository = SQLiteWorkflowRepository(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA user_version = 0")

    with pytest.raises(WorkflowRepositoryCorruption, match="schema marker"):
        repository.claim_events(
            lease_owner="schema-check-worker",
            limit=1,
            lease_seconds=1,
        )


# Import isolation runs in a clean interpreter so earlier test imports cannot
# conceal an accidental dependency on orchestration, CLI, RunStore, or LangGraph.
def test_sqlite_repository_import_has_no_runtime_engine_dependencies() -> None:
    """The storage adapter must import without loading current or future runtimes."""
    script = """
import sys
import trans_novel.storage.sqlite_workflows

forbidden = (
    "trans_novel.cli",
    "trans_novel.pipeline.orchestrator",
    "trans_novel.pipeline.runstore",
    "langgraph",
)
loaded = [name for name in forbidden if name in sys.modules]
if loaded:
    raise SystemExit(f"unexpected runtime imports: {loaded}")
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
