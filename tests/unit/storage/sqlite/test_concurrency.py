"""Concurrency and connection-lifetime contracts for the SQLite repository."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from trans_novel.storage.sqlite_workflows import SQLiteWorkflowRepository
from trans_novel.workflow import RevisionConflict, StatePatch, WorkflowRepositoryBusy


def test_concurrent_dispatchers_claim_disjoint_event_sets(workflow_harness) -> None:
    """Serialized writers must still produce non-overlapping public claims."""
    harness = workflow_harness
    patch = harness.build_event_patch(
        operation_id="operation-concurrent-claim",
        event_ids=("event-c1", "event-c2", "event-c3", "event-c4"),
    )
    harness.repository.commit_patch(harness.workflow_id, patch)
    start = threading.Barrier(2)

    def claim(owner: str):
        """Release both dispatchers together, then claim at most two rows."""
        start.wait(timeout=5)
        return harness.repository.claim_events(
            lease_owner=owner,
            limit=2,
            lease_seconds=1,
        )

    # Both threads share only the repository configuration, never a live connection.
    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(claim, "worker-a")
        second_future = executor.submit(claim, "worker-b")
        first = first_future.result(timeout=5)
        second = second_future.result(timeout=5)

    first_ids = {item.event["event_id"] for item in first}
    second_ids = {item.event["event_id"] for item in second}
    assert len(first_ids) == 2
    assert len(second_ids) == 2
    assert first_ids.isdisjoint(second_ids)
    assert first_ids | second_ids == {"event-c1", "event-c2", "event-c3", "event-c4"}


def test_external_write_lock_maps_timeout_to_repository_busy(tmp_path) -> None:
    """A competing BEGIN IMMEDIATE must fail through the retryable port error."""
    database_path = tmp_path / "busy.sqlite3"
    repository = SQLiteWorkflowRepository(database_path, busy_timeout_seconds=0.02)
    blocker = sqlite3.connect(database_path, isolation_level=None)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        with pytest.raises(WorkflowRepositoryBusy):
            repository.claim_events(
                lease_owner="worker-a",
                limit=1,
                lease_seconds=1,
            )
    finally:
        if blocker.in_transaction:
            blocker.execute("ROLLBACK")
        blocker.close()

    # Releasing the external writer restores normal repository access.
    assert (
        repository.claim_events(
            lease_owner="worker-a",
            limit=1,
            lease_seconds=1,
        )
        == ()
    )


def test_two_repositories_cannot_commit_the_same_revision(workflow_harness) -> None:
    """Serialized writers must still enforce reducer revision compare-and-swap."""
    harness = workflow_harness
    first_repository = harness.repository
    second_repository = SQLiteWorkflowRepository(first_repository.database_path)
    patches = (
        harness.build_event_patch(
            operation_id="prepare:writer-a",
            event_ids=("event-writer-a",),
        ),
        harness.build_event_patch(
            operation_id="prepare:writer-b",
            event_ids=("event-writer-b",),
        ),
    )
    start = threading.Barrier(2)

    def commit(repository: SQLiteWorkflowRepository, patch: StatePatch) -> str:
        """Race one revision-zero operation and classify only the expected loser."""
        start.wait(timeout=5)
        try:
            repository.commit_patch(harness.workflow_id, patch)
        except RevisionConflict:
            return "conflict"
        return "committed"

    # BEGIN IMMEDIATE serializes the writers; the second reducer observes the
    # committed revision and rejects its stale expected_revision instead of overwriting.
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(commit, first_repository, patches[0])
        second = executor.submit(commit, second_repository, patches[1])
        outcomes = {first.result(timeout=5), second.result(timeout=5)}

    assert outcomes == {"committed", "conflict"}
    assert first_repository.get(harness.workflow_id)["revision"] == 1
    with sqlite3.connect(first_repository.database_path) as connection:
        operation_count = connection.execute("SELECT COUNT(*) FROM workflow_operations").fetchone()[
            0
        ]
        outbox_count = connection.execute("SELECT COUNT(*) FROM workflow_outbox").fetchone()[0]
    assert (operation_count, outbox_count) == (1, 1)


def test_public_operations_close_database_handles_for_windows_cleanup(tmp_path) -> None:
    """Every short-lived connection must be closed before the method returns."""
    database_path = tmp_path / "deletable.sqlite3"
    repository = SQLiteWorkflowRepository(database_path)

    # Exercise both a read-write claim transaction and repository construction handles.
    assert (
        repository.claim_events(
            lease_owner="worker-a",
            limit=1,
            lease_seconds=1,
        )
        == ()
    )
    database_path.unlink()
    assert not database_path.exists()
