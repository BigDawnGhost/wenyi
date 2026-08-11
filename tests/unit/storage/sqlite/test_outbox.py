"""Contract tests for durable outbox leasing and acknowledgement semantics."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import replace

import pytest

from trans_novel.storage.sqlite_workflows import outbox as outbox_module
from trans_novel.storage.sqlite_workflows import repository as repository_module
from trans_novel.workflow import OutboxLeaseLost


def _delivery_rows(database_path) -> list[sqlite3.Row]:
    """Read delivery-only columns without depending on repository internals."""
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(
            """
            SELECT event_id, leased_by, lease_token, lease_expires_at_ms,
                   delivery_attempt, acked_at_ms
            FROM workflow_outbox
            ORDER BY outbox_sequence
            """
        ).fetchall()
    finally:
        connection.close()


def _advance_clock_after_write_lock(
    monkeypatch: pytest.MonkeyPatch,
    workflow_harness,
    *,
    now_ms: int,
) -> None:
    """Model time passing while BEGIN IMMEDIATE waits, then grant the writer."""
    original_write_transaction = repository_module.write_transaction

    @contextmanager
    def advancing_transaction(connection):
        """Advance the manual clock only after the real writer lock is held."""
        with original_write_transaction(connection):
            workflow_harness.clock.now_ms = now_ms
            yield

    monkeypatch.setattr(repository_module, "write_transaction", advancing_transaction)


def test_unexpired_claims_do_not_overlap_or_become_claimable_again(workflow_harness) -> None:
    """One active lease must exclude every later dispatcher until expiry."""
    harness = workflow_harness
    patch = harness.build_event_patch(
        operation_id="operation-three-events",
        event_ids=("event-1", "event-2", "event-3"),
    )
    harness.repository.commit_patch(harness.workflow_id, patch)

    # The first dispatcher owns two rows; the second may only take the untouched row.
    first = harness.repository.claim_events(
        lease_owner="worker-a",
        limit=2,
        lease_seconds=1,
    )
    second = harness.repository.claim_events(
        lease_owner="worker-b",
        limit=2,
        lease_seconds=1,
    )

    first_ids = {claim.event["event_id"] for claim in first}
    second_ids = {claim.event["event_id"] for claim in second}
    assert first_ids == {"event-1", "event-2"}
    assert second_ids == {"event-3"}
    assert first_ids.isdisjoint(second_ids)
    assert (
        harness.repository.claim_events(
            lease_owner="worker-c",
            limit=3,
            lease_seconds=1,
        )
        == ()
    )


def test_expired_lease_gets_new_token_and_rejects_old_acknowledgement(
    workflow_harness,
) -> None:
    """A reclaimed row increments its attempt and invalidates prior authority."""
    harness = workflow_harness
    patch = harness.build_event_patch(
        operation_id="operation-reclaim",
        event_ids=("event-reclaim",),
    )
    harness.repository.commit_patch(harness.workflow_id, patch)
    original = harness.repository.claim_events(
        lease_owner="worker-a",
        limit=1,
        lease_seconds=1,
    )[0]

    # The lease is still exclusive one millisecond before its deadline.
    harness.clock.now_ms = original.lease_expires_at_ms - 1
    assert (
        harness.repository.claim_events(
            lease_owner="worker-b",
            limit=1,
            lease_seconds=1,
        )
        == ()
    )

    # At the deadline, a new token invalidates the old worker's authority.
    harness.clock.now_ms = original.lease_expires_at_ms
    replacement = harness.repository.claim_events(
        lease_owner="worker-b",
        limit=1,
        lease_seconds=1,
    )[0]
    assert replacement.lease_token != original.lease_token
    assert replacement.delivery_attempt == original.delivery_attempt + 1

    with pytest.raises(OutboxLeaseLost):
        harness.repository.acknowledge_events((original,))

    harness.repository.acknowledge_events((replacement,))
    harness.repository.acknowledge_events((replacement,))  # Lost ack response replay.
    assert (
        harness.repository.claim_events(
            lease_owner="worker-c",
            limit=1,
            lease_seconds=1,
        )
        == ()
    )


def test_claim_samples_lease_clock_after_acquiring_write_lock(
    workflow_harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Writer-lock wait time must not consume the lease returned to a dispatcher."""
    harness = workflow_harness
    harness.repository.commit_patch(
        harness.workflow_id,
        harness.build_event_patch(
            operation_id="operation-lock-delayed-claim",
            event_ids=("event-lock-delayed-claim",),
        ),
    )
    _advance_clock_after_write_lock(monkeypatch, harness, now_ms=5_000)

    claim = harness.repository.claim_events(
        lease_owner="worker-a",
        limit=1,
        lease_seconds=1,
    )[0]

    assert claim.lease_expires_at_ms == 6_000


def test_ack_samples_expiry_clock_after_acquiring_write_lock(
    workflow_harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lease expiring during writer wait must not be acknowledged afterward."""
    harness = workflow_harness
    harness.repository.commit_patch(
        harness.workflow_id,
        harness.build_event_patch(
            operation_id="operation-lock-delayed-ack",
            event_ids=("event-lock-delayed-ack",),
        ),
    )
    claim = harness.repository.claim_events(
        lease_owner="worker-a",
        limit=1,
        lease_seconds=1,
    )[0]
    harness.clock.now_ms = claim.lease_expires_at_ms - 1
    _advance_clock_after_write_lock(
        monkeypatch,
        harness,
        now_ms=claim.lease_expires_at_ms,
    )

    with pytest.raises(OutboxLeaseLost):
        harness.repository.acknowledge_events((claim,))

    assert _delivery_rows(harness.repository.database_path)[0]["acked_at_ms"] is None


def test_reclaim_explicitly_avoids_reusing_previous_random_token(
    workflow_harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even a random-source collision must produce different lease authority."""
    harness = workflow_harness
    harness.repository.commit_patch(
        harness.workflow_id,
        harness.build_event_patch(
            operation_id="operation-token-collision",
            event_ids=("event-token-collision",),
        ),
    )
    original = harness.repository.claim_events(
        lease_owner="worker-a",
        limit=1,
        lease_seconds=1,
    )[0]
    replacement_token = "f" * 32 if original.lease_token != "f" * 32 else "e" * 32
    generated_tokens = iter((original.lease_token, replacement_token))
    monkeypatch.setattr(outbox_module.secrets, "token_hex", lambda _: next(generated_tokens))
    harness.clock.now_ms = original.lease_expires_at_ms

    replacement = harness.repository.claim_events(
        lease_owner="worker-b",
        limit=1,
        lease_seconds=1,
    )[0]

    assert replacement.lease_token == replacement_token
    assert replacement.lease_token != original.lease_token


def test_batch_acknowledgement_rolls_back_when_one_claim_is_stale(workflow_harness) -> None:
    """Validation of one bad authority must prevent every ack in the batch."""
    harness = workflow_harness
    patch = harness.build_event_patch(
        operation_id="operation-batch-ack",
        event_ids=("event-batch-1", "event-batch-2"),
    )
    harness.repository.commit_patch(harness.workflow_id, patch)
    claims = harness.repository.claim_events(
        lease_owner="worker-a",
        limit=2,
        lease_seconds=1,
    )

    # Mutate only the second authority with another syntactically valid token.
    stale_second = replace(claims[1], lease_token="f" * 32)
    with pytest.raises(OutboxLeaseLost):
        harness.repository.acknowledge_events((claims[0], stale_second))

    rows = _delivery_rows(harness.repository.database_path)
    assert [row["acked_at_ms"] for row in rows] == [None, None]


def test_duplicate_patch_after_ack_does_not_resurrect_delivery(workflow_harness) -> None:
    """Reducer replay must preserve acknowledgement, token, and attempt metadata."""
    harness = workflow_harness
    patch = harness.build_event_patch(
        operation_id="operation-duplicate-after-ack",
        event_ids=("event-acked",),
    )
    first_application = harness.repository.commit_patch(harness.workflow_id, patch)
    claim = harness.repository.claim_events(
        lease_owner="worker-a",
        limit=1,
        lease_seconds=1,
    )[0]
    harness.repository.acknowledge_events((claim,))
    before = dict(_delivery_rows(harness.repository.database_path)[0])

    duplicate_application = harness.repository.commit_patch(harness.workflow_id, patch)
    after = dict(_delivery_rows(harness.repository.database_path)[0])

    assert first_application.duplicate is False
    assert duplicate_application.duplicate is True
    assert after == before
    assert (
        harness.repository.claim_events(
            lease_owner="worker-b",
            limit=1,
            lease_seconds=1,
        )
        == ()
    )


class _AppendIfAbsentSink:
    """Minimal idempotent sink keyed by the repository's public event identity."""

    def __init__(self) -> None:
        self.visible: set[tuple[str, str]] = set()

    def append_if_absent(self, workflow_id: str, event_id: str) -> bool:
        """Append once and report whether this call made the event visible."""
        key = (workflow_id, event_id)
        if key in self.visible:
            return False
        self.visible.add(key)
        return True


def test_sink_success_before_ack_is_deduplicated_after_reclaim(workflow_harness) -> None:
    """A crash between sink and ack may redeliver but must not duplicate output."""
    harness = workflow_harness
    patch = harness.build_event_patch(
        operation_id="operation-sink-before-ack",
        event_ids=("event-once",),
    )
    harness.repository.commit_patch(harness.workflow_id, patch)
    sink = _AppendIfAbsentSink()

    # First delivery reaches the sink, then the worker crashes before acknowledgement.
    first = harness.repository.claim_events(
        lease_owner="worker-a",
        limit=1,
        lease_seconds=1,
    )[0]
    assert sink.append_if_absent(first.workflow_id, first.event["event_id"]) is True

    # Re-delivery after expiry is harmless because the sink owns visible-event deduplication.
    harness.clock.now_ms = first.lease_expires_at_ms
    replacement = harness.repository.claim_events(
        lease_owner="worker-b",
        limit=1,
        lease_seconds=1,
    )[0]
    assert sink.append_if_absent(replacement.workflow_id, replacement.event["event_id"]) is False
    harness.repository.acknowledge_events((replacement,))

    assert sink.visible == {(harness.workflow_id, "event-once")}
    assert (
        harness.repository.claim_events(
            lease_owner="worker-c",
            limit=1,
            lease_seconds=1,
        )
        == ()
    )
