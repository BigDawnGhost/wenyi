"""Durable outbox leasing and acknowledgement inside caller-owned transactions.

State event claims are permanent ownership records.  The functions here deal
only with temporary delivery leases, retained acknowledgements, and ABA-safe
tokens.  They never update a workflow snapshot or its revision.
"""

from __future__ import annotations

import copy
import secrets
import sqlite3
from collections.abc import Sequence

from ...domain.workflow import validate_operation_id, validate_workflow_event
from ...workflow.repository import (
    ClaimedWorkflowEvent,
    OutboxLeaseLost,
    WorkflowNotFound,
    WorkflowRepositoryCorruption,
)
from .codec import canonical_event_bytes
from .integrity import (
    LoadedWorkflow,
    OutboxRecord,
    load_and_audit_workflow,
    validate_lease_token,
)

_TOKEN_GENERATION_ATTEMPTS = 8


def lease_events(
    connection: sqlite3.Connection,
    *,
    lease_owner: str,
    limit: int,
    now_ms: int,
    expires_at_ms: int,
) -> tuple[ClaimedWorkflowEvent, ...]:
    """Lease eligible rows in stable order inside an active write transaction."""
    candidates = connection.execute(
        """
        SELECT outbox_sequence, workflow_id
        FROM workflow_outbox
        WHERE acked_at_ms IS NULL
          AND (lease_token IS NULL OR lease_expires_at_ms <= ?)
        ORDER BY outbox_sequence
        LIMIT ?
        """,
        (now_ms, limit),
    ).fetchall()
    if not candidates:
        return ()

    # Audit every touched workflow before changing delivery metadata.  Broken
    # state ownership or event bytes must never reach a downstream sink.
    loaded_by_id = _load_claim_workflows(connection, candidates)
    records_by_sequence = _records_by_sequence(loaded_by_id)
    token_by_sequence: dict[int, str] = {}
    for candidate in candidates:
        sequence, workflow_id = _candidate_identity(candidate)
        record = records_by_sequence.get(sequence)
        if record is None or record.workflow_id != workflow_id:
            raise WorkflowRepositoryCorruption("claim candidate is absent from audited outbox")
        if record.acked_at_ms is not None or (
            record.lease_token is not None
            and record.lease_expires_at_ms is not None
            and record.lease_expires_at_ms > now_ms
        ):
            raise WorkflowRepositoryCorruption(
                "claim query selected an ineligible audited outbox row"
            )

        token = _new_lease_token(previous_token=record.lease_token)
        cursor = connection.execute(
            """
            UPDATE workflow_outbox
            SET leased_by = ?, lease_token = ?, lease_expires_at_ms = ?,
                delivery_attempt = delivery_attempt + 1
            WHERE outbox_sequence = ?
              AND workflow_id = ?
              AND acked_at_ms IS NULL
              AND (lease_token IS NULL OR lease_expires_at_ms <= ?)
            """,
            (lease_owner, token, expires_at_ms, sequence, workflow_id, now_ms),
        )
        if cursor.rowcount != 1:
            raise WorkflowRepositoryCorruption(
                "eligible outbox row changed inside an exclusive write transaction"
            )
        token_by_sequence[sequence] = token

    # Re-audit the new metadata before commit, then detach mutable event payloads
    # from repository-owned values returned to consumers.
    refreshed = {
        workflow_id: load_and_audit_workflow(connection, workflow_id)
        for workflow_id in loaded_by_id
    }
    refreshed_by_sequence = _records_by_sequence(refreshed)
    claims: list[ClaimedWorkflowEvent] = []
    for candidate in candidates:
        sequence, workflow_id = _candidate_identity(candidate)
        record = refreshed_by_sequence.get(sequence)
        if record is None or record.workflow_id != workflow_id:
            raise WorkflowRepositoryCorruption("fresh lease disappeared from audited outbox")
        claims.append(
            _build_claim(
                record,
                expected_token=token_by_sequence[sequence],
            )
        )
    return tuple(claims)


def acknowledge_claims(
    connection: sqlite3.Connection,
    claims: Sequence[ClaimedWorkflowEvent],
    *,
    now_ms: int,
) -> None:
    """Validate an entire claim batch, then acknowledge all current leases."""
    loaded_by_id: dict[str, LoadedWorkflow] = {}
    for claim in claims:
        if claim.workflow_id in loaded_by_id:
            continue
        try:
            loaded_by_id[claim.workflow_id] = load_and_audit_workflow(
                connection,
                claim.workflow_id,
            )
        except WorkflowNotFound as error:
            raise OutboxLeaseLost(
                f"claimed workflow no longer exists: {claim.workflow_id}"
            ) from error

    # No UPDATE occurs until the whole batch has proved its authority.  One
    # stale token therefore rolls the logical batch back without partial ack.
    records_by_identity = {
        (record.workflow_id, record.event_id): record
        for loaded in loaded_by_id.values()
        for record in loaded.outbox
    }
    pending: list[tuple[ClaimedWorkflowEvent, OutboxRecord]] = []
    for claim in claims:
        record = records_by_identity.get((claim.workflow_id, claim.event["event_id"]))
        if record is None:
            raise OutboxLeaseLost(
                f"outbox event no longer exists: {(claim.workflow_id, claim.event['event_id'])!r}"
            )
        _require_matching_claim(record, claim)
        if record.acked_at_ms is not None:
            continue  # Same token replay after acknowledgement response loss.
        if record.lease_expires_at_ms is None or record.lease_expires_at_ms <= now_ms:
            raise OutboxLeaseLost(
                f"outbox lease expired before acknowledgement: {claim.event['event_id']}"
            )
        pending.append((claim, record))

    for claim, record in pending:
        cursor = connection.execute(
            """
            UPDATE workflow_outbox
            SET acked_at_ms = ?
            WHERE outbox_sequence = ?
              AND workflow_id = ?
              AND event_id = ?
              AND acked_at_ms IS NULL
              AND leased_by = ?
              AND lease_token = ?
              AND lease_expires_at_ms = ?
              AND lease_expires_at_ms > ?
              AND delivery_attempt = ?
            """,
            (
                now_ms,
                record.outbox_sequence,
                claim.workflow_id,
                claim.event["event_id"],
                claim.leased_by,
                claim.lease_token,
                claim.lease_expires_at_ms,
                now_ms,
                claim.delivery_attempt,
            ),
        )
        if cursor.rowcount != 1:
            raise OutboxLeaseLost(
                f"outbox lease was replaced during acknowledgement: {claim.event['event_id']}"
            )

    # Acked rows are retained.  Re-audit to prove delivery metadata did not
    # change snapshot, operation, or permanent ownership projections.
    for workflow_id in loaded_by_id:
        load_and_audit_workflow(connection, workflow_id)


def normalize_claim_batch(
    claims: Sequence[ClaimedWorkflowEvent],
) -> tuple[ClaimedWorkflowEvent, ...]:
    """Detach claims and collapse only completely identical duplicate authority."""
    if isinstance(claims, (str, bytes)) or not isinstance(claims, Sequence):
        raise TypeError("claims must be a sequence of ClaimedWorkflowEvent values")
    unique: dict[tuple[str, str], ClaimedWorkflowEvent] = {}
    for claim in claims:
        normalized = _normalize_claim(claim)
        key = (normalized.workflow_id, normalized.event["event_id"])
        previous = unique.get(key)
        if previous is not None and previous != normalized:
            raise OutboxLeaseLost(f"claim batch contains conflicting authority for {key!r}")
        unique[key] = normalized
    return tuple(unique.values())


def _load_claim_workflows(
    connection: sqlite3.Connection,
    candidates: Sequence[sqlite3.Row],
) -> dict[str, LoadedWorkflow]:
    """Audit each workflow represented by claim candidates exactly once."""
    loaded: dict[str, LoadedWorkflow] = {}
    for candidate in candidates:
        _, workflow_id = _candidate_identity(candidate)
        if workflow_id in loaded:
            continue
        try:
            loaded[workflow_id] = load_and_audit_workflow(connection, workflow_id)
        except WorkflowNotFound as error:  # pragma: no cover - foreign keys protect this path.
            raise WorkflowRepositoryCorruption(
                "outbox row references a missing workflow snapshot"
            ) from error
    return loaded


def _build_claim(
    record: OutboxRecord,
    *,
    expected_token: str,
) -> ClaimedWorkflowEvent:
    """Convert one freshly leased audited row into a detached authority value."""
    if (
        record.leased_by is None
        or record.lease_token != expected_token
        or record.lease_expires_at_ms is None
        or record.delivery_attempt <= 0
        or record.acked_at_ms is not None
    ):
        raise WorkflowRepositoryCorruption("fresh outbox lease could not be reconstructed")
    return ClaimedWorkflowEvent(
        workflow_id=record.workflow_id,
        operation_id=record.operation_id,
        committed_revision=record.committed_revision,
        event_index=record.event_index,
        event=copy.deepcopy(record.event),
        leased_by=record.leased_by,
        lease_token=record.lease_token,
        delivery_attempt=record.delivery_attempt,
        lease_expires_at_ms=record.lease_expires_at_ms,
    )


def _normalize_claim(claim: ClaimedWorkflowEvent) -> ClaimedWorkflowEvent:
    """Validate every authority field before opening a write transaction."""
    if not isinstance(claim, ClaimedWorkflowEvent):
        raise TypeError("claims must contain ClaimedWorkflowEvent values")
    workflow_id = _validate_identifier(claim.workflow_id, field="claim.workflow_id")
    operation_id = _validate_identifier(claim.operation_id, field="claim.operation_id")
    leased_by = _validate_identifier(claim.leased_by, field="claim.leased_by")
    try:
        lease_token = validate_lease_token(claim.lease_token, field="claim.lease_token")
        event = validate_workflow_event(claim.event)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("claim contains an invalid lease token or event") from error
    return ClaimedWorkflowEvent(
        workflow_id=workflow_id,
        operation_id=operation_id,
        committed_revision=_require_integer(
            claim.committed_revision,
            minimum=1,
            field="claim.committed_revision",
        ),
        event_index=_require_integer(
            claim.event_index,
            minimum=0,
            field="claim.event_index",
        ),
        event=event,
        leased_by=leased_by,
        lease_token=lease_token,
        delivery_attempt=_require_integer(
            claim.delivery_attempt,
            minimum=1,
            field="claim.delivery_attempt",
        ),
        lease_expires_at_ms=_require_integer(
            claim.lease_expires_at_ms,
            minimum=0,
            field="claim.lease_expires_at_ms",
        ),
    )


def _require_matching_claim(record: OutboxRecord, claim: ClaimedWorkflowEvent) -> None:
    """Reject mutated or ABA-stale authority before acknowledgement."""
    if (
        record.workflow_id != claim.workflow_id
        or record.operation_id != claim.operation_id
        or record.committed_revision != claim.committed_revision
        or record.event_index != claim.event_index
        or record.event_json != canonical_event_bytes(claim.event)
        or record.leased_by != claim.leased_by
        or record.lease_token != claim.lease_token
        or record.delivery_attempt != claim.delivery_attempt
        or record.lease_expires_at_ms != claim.lease_expires_at_ms
    ):
        raise OutboxLeaseLost(
            f"outbox lease authority no longer matches: "
            f"{(claim.workflow_id, claim.event['event_id'])!r}"
        )


def _candidate_identity(candidate: sqlite3.Row) -> tuple[int, str]:
    """Read a claim candidate without coercing corrupted SQLite values."""
    try:
        return (
            _require_integer(candidate["outbox_sequence"], minimum=1, field="outbox_sequence"),
            _validate_identifier(candidate["workflow_id"], field="workflow_id"),
        )
    except ValueError as error:
        raise WorkflowRepositoryCorruption("claim candidate identity is invalid") from error


def _records_by_sequence(
    loaded_by_id: dict[str, LoadedWorkflow],
) -> dict[int, OutboxRecord]:
    """Index an audited batch once instead of scanning retained history per event."""
    records: dict[int, OutboxRecord] = {}
    for loaded in loaded_by_id.values():
        for record in loaded.outbox:
            if record.outbox_sequence in records:
                raise WorkflowRepositoryCorruption("outbox sequence is not globally unique")
            records[record.outbox_sequence] = record
    return records


def _new_lease_token(*, previous_token: str | None) -> str:
    """Generate a schema-v1 token that is explicitly different from the old lease."""
    for _ in range(_TOKEN_GENERATION_ATTEMPTS):
        token = secrets.token_hex(16)
        if token != previous_token:
            return token
    raise WorkflowRepositoryCorruption("could not generate a fresh outbox lease token")


def _validate_identifier(value: object, *, field: str) -> str:
    """Apply the shared path-safe grammar to persisted delivery identities."""
    try:
        return validate_operation_id(value, field=field)
    except ValueError as error:
        raise ValueError(f"{field} is invalid") from error


def _require_integer(value: object, *, minimum: int, field: str) -> int:
    """Reject booleans and enforce a lower bound on claim authority integers."""
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


__all__ = ["acknowledge_claims", "lease_events", "normalize_claim_batch"]
