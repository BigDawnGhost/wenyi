"""SQLite workflow repository core persistence-contract tests.

These tests intentionally inspect both the public repository result and the
three normalized SQLite projections.  A green public API is not sufficient if
snapshot, operation history, or outbox recovery data can silently diverge.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from trans_novel.domain.workflow import StageStatus, WorkflowStatus
from trans_novel.storage.sqlite_workflows import SQLiteWorkflowRepository
from trans_novel.storage.sqlite_workflows import repository as repository_module
from trans_novel.workflow import (
    OperationConflict,
    StatePatch,
    WorkflowAlreadyExists,
    WorkflowNotFound,
    WorkflowRepositoryCorruption,
    WorkflowRepositoryError,
    WorkflowState,
    new_workflow_state,
)

_PROFILE_HASH = "b" * 64


def _artifact(*, sha256: str, uri: str = "artifact://source/book.epub") -> dict[str, object]:
    """Build a valid detached artifact reference for a repository fixture."""
    return {
        "uri": uri,
        "sha256": sha256,
        "media_type": "application/epub+zip",
        "size_bytes": 123,
    }


def _new_state(*, source_hash: str = "a" * 64) -> WorkflowState:
    """Create a pristine state whose source hash controls workflow identity."""
    return new_workflow_state(
        source_artifact=_artifact(sha256=source_hash),
        source_format="epub",
        source_lang="ja",
        target_lang="zh",
        semantic_profile_hash=_PROFILE_HASH,
    )


def _start_patch(
    *,
    operation_id: str = "prepare:start",
    event_id: str = "prepare-started",
    payload: Mapping[str, object] | None = None,
) -> StatePatch:
    """Build one legal first transition with a recoverable domain event."""
    event_payload = {"phase": "prepare"} if payload is None else dict(payload)
    return StatePatch(
        operation_id=operation_id,
        expected_revision=0,
        updates={
            "status": WorkflowStatus.RUNNING.value,
            "preparation": {
                "status": StageStatus.RUNNING.value,
                "normalized_source": None,
            },
        },
        events=(
            {
                "event_id": event_id,
                "event_type": "preparation.started",
                "payload": event_payload,
            },
        ),
    )


@contextmanager
def _open_rows(database_path: Path) -> Iterator[sqlite3.Connection]:
    """Yield and explicitly close a raw handle used for white-box assertions."""
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


# Creation tests protect the boundary between caller-owned mutable dictionaries
# and the repository's detached, canonical snapshot.
def test_create_and_get_return_detached_pristine_snapshots(tmp_path: Path) -> None:
    """Caller mutations must not cross either side of create/get boundaries."""
    database_path = tmp_path / "workflows.sqlite3"
    repository = SQLiteWorkflowRepository(database_path)
    original = _new_state()

    created = repository.create(original)
    original["request"]["source_format"] = "mutated-by-caller"
    created["request"]["source_artifact"]["uri"] = "artifact://mutated/created"

    loaded = repository.get(created["workflow_id"])
    assert loaded["revision"] == 0
    assert loaded["request"]["source_format"] == "epub"
    assert loaded["request"]["source_artifact"]["uri"] == "artifact://source/book.epub"

    loaded["exports"]["requested_formats"].append("pdf")
    assert repository.get(created["workflow_id"])["exports"]["requested_formats"] == []


def test_create_is_strict_and_get_reports_missing_workflow(tmp_path: Path) -> None:
    """Creation must not hide identity collisions and reads must identify absence."""
    repository = SQLiteWorkflowRepository(tmp_path / "workflows.sqlite3")
    state = _new_state()
    repository.create(state)

    with pytest.raises(WorkflowAlreadyExists):
        repository.create(state)
    with pytest.raises(WorkflowNotFound):
        repository.get("wf-" + "0" * 64)


def test_create_rejects_a_valid_but_non_pristine_state(tmp_path: Path) -> None:
    """Only revision-zero states without reducer-owned ledgers can be created."""
    repository = SQLiteWorkflowRepository(tmp_path / "workflows.sqlite3")
    state = _new_state()
    advanced = copy.deepcopy(state)
    advanced["revision"] = 1
    advanced["applied_operations"] = {"prepare:start": "c" * 64}

    with pytest.raises(ValueError, match="pristine|revision-zero"):
        repository.create(advanced)


# Commit tests compare reducer output with all durable projections required for
# cold-start recovery and deterministic outbox delivery.
def test_fresh_commit_atomically_populates_all_three_projections(tmp_path: Path) -> None:
    """One fresh patch must persist matching snapshot, operation, and event rows."""
    database_path = tmp_path / "workflows.sqlite3"
    repository = SQLiteWorkflowRepository(database_path)
    state = repository.create(_new_state())
    patch = _start_patch()

    application = repository.commit_patch(state["workflow_id"], patch)

    assert application.duplicate is False
    assert application.state["revision"] == 1
    assert application.state["claimed_event_ids"] == {"prepare-started": "prepare:start"}
    with _open_rows(database_path) as connection:
        snapshot = connection.execute(
            "SELECT revision, state_json FROM workflow_snapshots WHERE workflow_id = ?",
            (state["workflow_id"],),
        ).fetchone()
        operation = connection.execute(
            """
            SELECT operation_id, committed_revision, patch_fingerprint, event_count
            FROM workflow_operations WHERE workflow_id = ?
            """,
            (state["workflow_id"],),
        ).fetchone()
        outbox = connection.execute(
            """
            SELECT event_id, operation_id, event_index, event_json, delivery_attempt
            FROM workflow_outbox WHERE workflow_id = ?
            """,
            (state["workflow_id"],),
        ).fetchone()

    assert snapshot is not None
    assert snapshot["revision"] == 1
    assert json.loads(snapshot["state_json"])["revision"] == 1
    assert operation is not None
    assert dict(operation) == {
        "operation_id": "prepare:start",
        "committed_revision": 1,
        "patch_fingerprint": application.state["applied_operations"]["prepare:start"],
        "event_count": 1,
    }
    assert outbox is not None
    assert outbox["event_id"] == "prepare-started"
    assert outbox["operation_id"] == "prepare:start"
    assert outbox["event_index"] == 0
    assert json.loads(outbox["event_json"]) == application.events[0]
    assert outbox["delivery_attempt"] == 0


def test_exact_replay_returns_duplicate_without_adding_rows(tmp_path: Path) -> None:
    """A lost commit response can be replayed without advancing durable history."""
    database_path = tmp_path / "workflows.sqlite3"
    repository = SQLiteWorkflowRepository(database_path)
    state = repository.create(_new_state())
    patch = _start_patch()
    first = repository.commit_patch(state["workflow_id"], patch)

    replay = repository.commit_patch(state["workflow_id"], patch)

    assert replay.duplicate is True
    assert replay.state == first.state
    assert replay.events == first.events
    with _open_rows(database_path) as connection:
        counts = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "workflow_snapshots",
                "workflow_operations",
                "workflow_outbox",
            )
        )
    assert counts == (1, 1, 1)


def test_commit_response_loss_replays_from_durable_three_table_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A COMMIT that succeeds before response loss must replay as a duplicate."""
    database_path = tmp_path / "workflows.sqlite3"
    repository = SQLiteWorkflowRepository(database_path)
    state = repository.create(_new_state())
    patch = _start_patch()
    original_transaction = repository_module.write_transaction

    @contextmanager
    def commit_then_lose_response(connection: sqlite3.Connection) -> Iterator[None]:
        """Commit the real transaction, then simulate loss before API return."""
        with original_transaction(connection):
            yield
        raise WorkflowRepositoryError("simulated commit response loss")

    monkeypatch.setattr(
        repository_module,
        "write_transaction",
        commit_then_lose_response,
    )
    with pytest.raises(WorkflowRepositoryError, match="response loss"):
        repository.commit_patch(state["workflow_id"], patch)

    # A new call has only durable state available; it must recover the exact
    # effects without adding a second operation or outbox row.
    monkeypatch.setattr(repository_module, "write_transaction", original_transaction)
    replay = repository.commit_patch(state["workflow_id"], patch)
    assert replay.duplicate is True
    with _open_rows(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM workflow_operations").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM workflow_outbox").fetchone()[0] == 1


def test_reusing_operation_id_for_different_effects_is_rejected(tmp_path: Path) -> None:
    """An operation identity cannot be rebound to a different patch fingerprint."""
    database_path = tmp_path / "workflows.sqlite3"
    repository = SQLiteWorkflowRepository(database_path)
    state = repository.create(_new_state())
    repository.commit_patch(state["workflow_id"], _start_patch())
    conflicting_patch = _start_patch(payload={"phase": "different"})

    with pytest.raises(OperationConflict):
        repository.commit_patch(state["workflow_id"], conflicting_patch)

    with _open_rows(database_path) as connection:
        operation_count = connection.execute("SELECT COUNT(*) FROM workflow_operations").fetchone()[
            0
        ]
        outbox_count = connection.execute("SELECT COUNT(*) FROM workflow_outbox").fetchone()[0]
    assert (operation_count, outbox_count) == (1, 1)


def test_same_event_id_is_scoped_to_each_workflow(tmp_path: Path) -> None:
    """Outbox event ownership is unique within, rather than across, workflows."""
    database_path = tmp_path / "workflows.sqlite3"
    repository = SQLiteWorkflowRepository(database_path)
    first = repository.create(_new_state(source_hash="a" * 64))
    second = repository.create(_new_state(source_hash="c" * 64))

    repository.commit_patch(first["workflow_id"], _start_patch())
    repository.commit_patch(second["workflow_id"], _start_patch())

    with _open_rows(database_path) as connection:
        owners = connection.execute(
            """
            SELECT workflow_id, event_id FROM workflow_outbox
            WHERE event_id = ? ORDER BY workflow_id
            """,
            ("prepare-started",),
        ).fetchall()
    assert [row["workflow_id"] for row in owners] == sorted(
        [first["workflow_id"], second["workflow_id"]]
    )


def test_unicode_payload_and_multi_event_order_survive_cold_reload(tmp_path: Path) -> None:
    """Canonical UTF-8 payloads and patch-local event ordinals survive reopening."""
    database_path = tmp_path / "workflows.sqlite3"
    repository = SQLiteWorkflowRepository(database_path)
    state = repository.create(_new_state())
    events = (
        {
            "event_id": "prepare-started",
            "event_type": "preparation.started",
            "payload": {"书名": "银河铁道之夜", "作者": "宮沢賢治", "emoji": "🌌"},
        },
        {
            "event_id": "source-profiled",
            "event_type": "preparation.profiled",
            "payload": {"语言": ["日本語", "简体中文"], "nested": {"标点": "「」"}},
        },
    )
    patch = StatePatch(
        operation_id="prepare:start",
        expected_revision=0,
        updates={
            "status": WorkflowStatus.RUNNING.value,
            "preparation": {
                "status": StageStatus.RUNNING.value,
                "normalized_source": None,
            },
        },
        events=events,
    )

    committed = repository.commit_patch(state["workflow_id"], patch)
    reopened = SQLiteWorkflowRepository(database_path)
    claims = reopened.claim_events(lease_owner="dispatcher:test", limit=10, lease_seconds=30)

    assert reopened.get(state["workflow_id"]) == committed.state
    assert [claim.event_index for claim in claims] == [0, 1]
    assert [claim.event for claim in claims] == list(events)


def test_tampered_outbox_sequence_cannot_reorder_committed_operations(tmp_path: Path) -> None:
    """Delivery sequence must remain revision-first, then patch-local event order."""
    database_path = tmp_path / "workflows.sqlite3"
    repository = SQLiteWorkflowRepository(database_path)
    state = repository.create(_new_state())
    first = repository.commit_patch(state["workflow_id"], _start_patch())
    repository.commit_patch(
        state["workflow_id"],
        StatePatch(
            operation_id="prepare:progress",
            expected_revision=first.state["revision"],
            updates={"preparation": copy.deepcopy(first.state["preparation"])},
            events=(
                {
                    "event_id": "prepare-progressed",
                    "event_type": "preparation.progressed",
                    "payload": {"phase": "prepare"},
                },
            ),
        ),
    )

    # Swap the two public sequence keys through a temporary negative value;
    # every other state, operation, event, and ownership projection stays valid.
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE workflow_outbox SET outbox_sequence = -1 WHERE outbox_sequence = 1"
        )
        connection.execute(
            "UPDATE workflow_outbox SET outbox_sequence = 1 WHERE outbox_sequence = 2"
        )
        connection.execute(
            "UPDATE workflow_outbox SET outbox_sequence = 2 WHERE outbox_sequence = -1"
        )

    with pytest.raises(WorkflowRepositoryCorruption, match="sequence"):
        repository.get(state["workflow_id"])


def test_trigger_rewritten_fresh_event_rolls_back_all_three_projections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-insert payload drift must fail before state, operation, or outbox commit."""
    database_path = tmp_path / "workflows.sqlite3"
    repository = SQLiteWorkflowRepository(database_path)
    state = repository.create(_new_state())
    rewritten_event = {
        "event_id": "prepare-started",
        "event_type": "preparation.started",
        "payload": {"phase": "silently-rewritten"},
    }
    rewritten_json = json.dumps(
        rewritten_event,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    rewritten_sha256 = hashlib.sha256(rewritten_json).hexdigest()

    # Install the trigger immediately after the in-transaction schema check.
    # This models an adapter defect inside the protected write boundary and
    # proves the post-write event binding remains an independent defense.
    original_verify = repository_module.verify_database_schema

    def verify_then_install_rewriter(connection: sqlite3.Connection) -> None:
        """Verify the valid schema, then inject one transaction-local SQL defect."""
        original_verify(connection)
        connection.execute(
            f"""
            CREATE TRIGGER rewrite_fresh_event
            AFTER INSERT ON workflow_outbox
            BEGIN
                UPDATE workflow_outbox
                SET event_json = X'{rewritten_json.hex()}',
                    event_sha256 = '{rewritten_sha256}'
                WHERE outbox_sequence = NEW.outbox_sequence;
            END
            """
        )

    monkeypatch.setattr(
        repository_module,
        "verify_database_schema",
        verify_then_install_rewriter,
    )

    with pytest.raises(WorkflowRepositoryCorruption, match="payload"):
        repository.commit_patch(state["workflow_id"], _start_patch())

    monkeypatch.setattr(repository_module, "verify_database_schema", original_verify)
    assert repository.get(state["workflow_id"])["revision"] == 0
    with _open_rows(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM workflow_operations").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM workflow_outbox").fetchone()[0] == 0


def test_schema_trigger_added_after_initialization_blocks_public_write(tmp_path: Path) -> None:
    """Every write transaction must reject DDL drift before changing projections."""
    database_path = tmp_path / "workflows.sqlite3"
    repository = SQLiteWorkflowRepository(database_path)
    state = repository.create(_new_state())

    # This legal metadata combination would make a new event permanently
    # invisible if the repository trusted only its initialization-time check.
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER suppress_fresh_delivery
            AFTER INSERT ON workflow_outbox
            BEGIN
                UPDATE workflow_outbox
                SET leased_by = 'injected-worker',
                    lease_token = 'ffffffffffffffffffffffffffffffff',
                    lease_expires_at_ms = 9999999999999,
                    delivery_attempt = 1,
                    acked_at_ms = 0
                WHERE outbox_sequence = NEW.outbox_sequence;
            END
            """
        )

    with pytest.raises(WorkflowRepositoryCorruption, match="unexpected triggers"):
        repository.commit_patch(state["workflow_id"], _start_patch())

    # Remove only the deliberate test corruption, then prove the rejected
    # transaction left the original pristine workflow intact.
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TRIGGER suppress_fresh_delivery")
    assert repository.get(state["workflow_id"])["revision"] == 0


def test_fresh_commit_rejects_predelivered_metadata_after_schema_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-write audit must reject delivery suppression even after schema passed."""
    database_path = tmp_path / "workflows.sqlite3"
    repository = SQLiteWorkflowRepository(database_path)
    state = repository.create(_new_state())
    original_verify = repository_module.verify_database_schema

    def verify_then_install_suppressor(connection: sqlite3.Connection) -> None:
        """Inject a suppressing trigger after the transaction owns the writer."""
        original_verify(connection)
        connection.execute(
            """
            CREATE TRIGGER suppress_after_schema_check
            AFTER INSERT ON workflow_outbox
            BEGIN
                UPDATE workflow_outbox
                SET leased_by = 'injected-worker',
                    lease_token = 'ffffffffffffffffffffffffffffffff',
                    lease_expires_at_ms = 9999999999999,
                    delivery_attempt = 1,
                    acked_at_ms = 0
                WHERE outbox_sequence = NEW.outbox_sequence;
            END
            """
        )

    monkeypatch.setattr(
        repository_module,
        "verify_database_schema",
        verify_then_install_suppressor,
    )
    with pytest.raises(WorkflowRepositoryCorruption, match="pre-delivered"):
        repository.commit_patch(state["workflow_id"], _start_patch())

    monkeypatch.setattr(repository_module, "verify_database_schema", original_verify)
    assert repository.get(state["workflow_id"])["revision"] == 0


# Corruption tests mutate one redundant projection at a time and require every
# public read to fail closed instead of trusting whichever copy was read first.
@pytest.mark.parametrize(
    ("statement", "replacement"),
    [
        (
            "UPDATE workflow_snapshots SET state_sha256 = ? WHERE workflow_id = ?",
            "0" * 64,
        ),
        (
            "UPDATE workflow_operations SET patch_fingerprint = ? WHERE workflow_id = ?",
            "0" * 64,
        ),
        (
            "UPDATE workflow_outbox SET event_sha256 = ? WHERE workflow_id = ?",
            "0" * 64,
        ),
    ],
    ids=("snapshot", "operation", "outbox"),
)
def test_tampered_projection_is_reported_as_corruption(
    tmp_path: Path,
    statement: str,
    replacement: str,
) -> None:
    """Digest or projection-ledger tampering must fail every audited read closed."""
    database_path = tmp_path / "workflows.sqlite3"
    repository = SQLiteWorkflowRepository(database_path)
    state = repository.create(_new_state())
    repository.commit_patch(state["workflow_id"], _start_patch())

    with sqlite3.connect(database_path) as connection:
        connection.execute(statement, (replacement, state["workflow_id"]))

    with pytest.raises(WorkflowRepositoryCorruption):
        repository.get(state["workflow_id"])
