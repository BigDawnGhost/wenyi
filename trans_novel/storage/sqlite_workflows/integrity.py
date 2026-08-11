"""Cross-table integrity checks for one persisted workflow.

SQLite constraints protect local row shapes.  This module verifies the wider
domain projection: a snapshot revision owns one contiguous operation ledger,
and every permanent event ownership claim has one complete retained outbox
row.  Normal repository reads fail closed instead of trying to repair history.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from ...domain.workflow import (
    WorkflowEvent,
    validate_operation_id,
    validate_sha256,
)
from ...workflow.patches import PatchApplication
from ...workflow.repository import (
    WorkflowNotFound,
    WorkflowRepositoryCorruption,
)
from ...workflow.state import WorkflowState
from .codec import canonical_event_bytes, decode_event, decode_state

_LEASE_TOKEN_PATTERN = re.compile(r"^[0-9a-f]{32}$")


@dataclass(frozen=True, slots=True)
class OperationRecord:
    """Normalized relational history for one committed state patch."""

    operation_id: str
    committed_revision: int
    patch_fingerprint: str
    event_count: int
    committed_at_ms: int


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    """A fully checked outbox row, including mutable delivery metadata."""

    outbox_sequence: int
    workflow_id: str
    event_id: str
    operation_id: str
    committed_revision: int
    event_index: int
    event: WorkflowEvent
    event_json: bytes
    created_at_ms: int
    leased_by: str | None
    lease_token: str | None
    lease_expires_at_ms: int | None
    delivery_attempt: int
    acked_at_ms: int | None


@dataclass(frozen=True, slots=True)
class LoadedWorkflow:
    """One detached snapshot and the normalized projections that prove it."""

    state: WorkflowState
    operations: dict[str, OperationRecord]
    outbox: tuple[OutboxRecord, ...]


def load_and_audit_workflow(
    connection: sqlite3.Connection,
    workflow_id: str,
) -> LoadedWorkflow:
    """Load one workflow and verify all state/operation/outbox invariants."""
    snapshot_row = connection.execute(
        """
        SELECT workflow_id, workflow_schema_version, revision,
               state_json, state_sha256, created_at_ms, updated_at_ms
        FROM workflow_snapshots
        WHERE workflow_id = ?
        """,
        (workflow_id,),
    ).fetchone()
    if snapshot_row is None:
        raise WorkflowNotFound(f"workflow does not exist: {workflow_id}")

    state = _audit_snapshot_row(snapshot_row, requested_workflow_id=workflow_id)
    operations = _load_operations(connection, workflow_id=workflow_id, state=state)
    outbox = _load_outbox(
        connection,
        workflow_id=workflow_id,
        state=state,
        operations=operations,
    )
    return LoadedWorkflow(state=state, operations=operations, outbox=outbox)


def verify_duplicate_application(
    loaded: LoadedWorkflow,
    *,
    operation_id: str,
    application: PatchApplication,
) -> None:
    """Prove that a replay exactly matches its retained operation effects."""
    if not application.duplicate or application.state != loaded.state:
        raise WorkflowRepositoryCorruption("reducer duplicate result disagrees with stored state")
    verify_operation_events(
        loaded,
        operation_id=operation_id,
        events=application.events,
    )


def verify_operation_events(
    loaded: LoadedWorkflow,
    *,
    operation_id: str,
    events: tuple[WorkflowEvent, ...],
) -> None:
    """Bind retained event bytes to the reducer effects for one operation."""
    operation = loaded.operations.get(operation_id)
    if operation is None:
        raise WorkflowRepositoryCorruption(
            f"operation is absent from relational history: {operation_id}"
        )

    persisted = tuple(row for row in loaded.outbox if row.operation_id == operation_id)
    if len(persisted) != len(events) or len(persisted) != operation.event_count:
        raise WorkflowRepositoryCorruption(
            f"operation {operation_id!r} has an incomplete event history"
        )
    for event_index, (row, event) in enumerate(zip(persisted, events)):
        if row.event_index != event_index or row.event_json != canonical_event_bytes(event):
            raise WorkflowRepositoryCorruption(
                f"operation {operation_id!r} event payload does not match outbox"
            )


def verify_fresh_operation_delivery(
    loaded: LoadedWorkflow,
    *,
    operation_id: str,
    events: tuple[WorkflowEvent, ...],
) -> None:
    """Bind fresh event effects and prove none were pre-leased or acknowledged."""
    verify_operation_events(loaded, operation_id=operation_id, events=events)
    for row in loaded.outbox:
        if row.operation_id != operation_id:
            continue
        if (
            row.leased_by is not None
            or row.lease_token is not None
            or row.lease_expires_at_ms is not None
            or row.delivery_attempt != 0
            or row.acked_at_ms is not None
        ):
            raise WorkflowRepositoryCorruption(
                f"fresh operation {operation_id!r} contains pre-delivered outbox metadata"
            )


def validate_lease_token(value: object, *, field: str = "lease_token") -> str:
    """Validate the schema-v1 random token representation."""
    if type(value) is not str or _LEASE_TOKEN_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field} must be 32 lowercase hexadecimal characters")
    return value


def _audit_snapshot_row(row: sqlite3.Row, *, requested_workflow_id: str) -> WorkflowState:
    """Verify redundant snapshot columns against canonical state JSON."""
    stored_workflow_id = _require_text(row["workflow_id"], field="snapshot.workflow_id")
    if stored_workflow_id != requested_workflow_id:
        raise WorkflowRepositoryCorruption("snapshot primary key changed during lookup")
    state = decode_state(row["state_json"], row["state_sha256"])
    schema_version = _require_int(
        row["workflow_schema_version"],
        minimum=1,
        field="snapshot.workflow_schema_version",
    )
    revision = _require_int(row["revision"], minimum=0, field="snapshot.revision")
    created_at_ms = _require_int(
        row["created_at_ms"],
        minimum=0,
        field="snapshot.created_at_ms",
    )
    _require_int(
        row["updated_at_ms"],
        minimum=created_at_ms,
        field="snapshot.updated_at_ms",
    )
    if (
        state["workflow_id"] != stored_workflow_id
        or state["schema_version"] != schema_version
        or state["revision"] != revision
    ):
        raise WorkflowRepositoryCorruption(
            "snapshot identity, schema, or revision column disagrees with state JSON"
        )
    return state


def _load_operations(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    state: WorkflowState,
) -> dict[str, OperationRecord]:
    """Verify the normalized operation ledger is exact and revision-contiguous."""
    rows = connection.execute(
        """
        SELECT operation_id, committed_revision, patch_fingerprint,
               event_count, committed_at_ms
        FROM workflow_operations
        WHERE workflow_id = ?
        ORDER BY committed_revision
        """,
        (workflow_id,),
    ).fetchall()
    if len(rows) != state["revision"]:
        raise WorkflowRepositoryCorruption("operation row count disagrees with state revision")

    operations: dict[str, OperationRecord] = {}
    for expected_revision, row in enumerate(rows, start=1):
        operation_id = _require_operation_id(
            row["operation_id"],
            field="operation.operation_id",
        )
        committed_revision = _require_int(
            row["committed_revision"],
            minimum=1,
            field="operation.committed_revision",
        )
        if committed_revision != expected_revision:
            raise WorkflowRepositoryCorruption("operation revisions are not contiguous")
        try:
            fingerprint = validate_sha256(
                row["patch_fingerprint"],
                field="operation.patch_fingerprint",
            )
        except ValueError as error:
            raise WorkflowRepositoryCorruption("operation fingerprint is invalid") from error
        event_count = _require_int(
            row["event_count"],
            minimum=0,
            field="operation.event_count",
        )
        committed_at_ms = _require_int(
            row["committed_at_ms"],
            minimum=0,
            field="operation.committed_at_ms",
        )
        operations[operation_id] = OperationRecord(
            operation_id=operation_id,
            committed_revision=committed_revision,
            patch_fingerprint=fingerprint,
            event_count=event_count,
            committed_at_ms=committed_at_ms,
        )

    projected = {name: record.patch_fingerprint for name, record in operations.items()}
    if projected != state["applied_operations"]:
        raise WorkflowRepositoryCorruption("operation rows disagree with state operation ledger")
    return operations


def _load_outbox(
    connection: sqlite3.Connection,
    *,
    workflow_id: str,
    state: WorkflowState,
    operations: dict[str, OperationRecord],
) -> tuple[OutboxRecord, ...]:
    """Verify complete event payloads, ownership, ordering, and lease metadata."""
    rows = connection.execute(
        """
        SELECT outbox_sequence, workflow_id, event_id, operation_id,
               event_index, event_json, event_sha256, created_at_ms,
               leased_by, lease_token, lease_expires_at_ms,
               delivery_attempt, acked_at_ms
        FROM workflow_outbox
        WHERE workflow_id = ?
        ORDER BY outbox_sequence
        """,
        (workflow_id,),
    ).fetchall()

    records: list[OutboxRecord] = []
    indexes_by_operation: dict[str, list[int]] = {name: [] for name in operations}
    projected_claims: dict[str, str] = {}
    for row in rows:
        record = _audit_outbox_row(row, workflow_id=workflow_id, operations=operations)
        records.append(record)
        indexes_by_operation[record.operation_id].append(record.event_index)
        if record.event_id in projected_claims:
            raise WorkflowRepositoryCorruption("outbox contains duplicate event ownership")
        projected_claims[record.event_id] = record.operation_id

    # event_count and zero-based ordinals let a cold process prove no event was
    # lost between snapshot update and transaction commit.
    for operation_id, operation in operations.items():
        actual_indexes = sorted(indexes_by_operation[operation_id])
        if actual_indexes != list(range(operation.event_count)):
            raise WorkflowRepositoryCorruption(
                f"operation {operation_id!r} has a non-contiguous outbox projection"
            )

    # Sequence is the stable claim/enqueue order.  Per-operation ordinals alone
    # do not prove that two committed operations have not been reordered;
    # parallel consumers may still finish their sink writes out of order.
    actual_order = [
        (operations[record.operation_id].committed_revision, record.event_index)
        for record in records
    ]
    if actual_order != sorted(actual_order):
        raise WorkflowRepositoryCorruption(
            "outbox sequence disagrees with committed revision and event index order"
        )
    if projected_claims != state["claimed_event_ids"]:
        raise WorkflowRepositoryCorruption("outbox rows disagree with state event ownership")
    return tuple(records)


def _audit_outbox_row(
    row: sqlite3.Row,
    *,
    workflow_id: str,
    operations: dict[str, OperationRecord],
) -> OutboxRecord:
    """Normalize one row and bind its event bytes to relational identity columns."""
    sequence = _require_int(
        row["outbox_sequence"],
        minimum=1,
        field="outbox.outbox_sequence",
    )
    row_workflow_id = _require_text(row["workflow_id"], field="outbox.workflow_id")
    if row_workflow_id != workflow_id:
        raise WorkflowRepositoryCorruption("outbox workflow identity is inconsistent")
    event_id = _require_operation_id(row["event_id"], field="outbox.event_id")
    operation_id = _require_operation_id(
        row["operation_id"],
        field="outbox.operation_id",
    )
    operation = operations.get(operation_id)
    if operation is None:
        raise WorkflowRepositoryCorruption("outbox owner operation is missing")
    event_index = _require_int(row["event_index"], minimum=0, field="outbox.event_index")
    event = decode_event(row["event_json"], row["event_sha256"])
    if event["event_id"] != event_id:
        raise WorkflowRepositoryCorruption("outbox event_id column disagrees with event JSON")
    created_at_ms = _require_int(
        row["created_at_ms"],
        minimum=0,
        field="outbox.created_at_ms",
    )

    delivery_attempt = _require_int(
        row["delivery_attempt"],
        minimum=0,
        field="outbox.delivery_attempt",
    )
    leased_by = row["leased_by"]
    lease_token = row["lease_token"]
    lease_expires_at_ms = row["lease_expires_at_ms"]
    lease_values = (leased_by, lease_token, lease_expires_at_ms)
    if all(value is None for value in lease_values):
        if delivery_attempt != 0:
            raise WorkflowRepositoryCorruption("unleased outbox row has delivery attempts")
    elif all(value is not None for value in lease_values):
        leased_by = _require_operation_id(leased_by, field="outbox.leased_by")
        try:
            lease_token = validate_lease_token(lease_token, field="outbox.lease_token")
        except ValueError as error:
            raise WorkflowRepositoryCorruption("outbox lease token is invalid") from error
        lease_expires_at_ms = _require_int(
            lease_expires_at_ms,
            minimum=0,
            field="outbox.lease_expires_at_ms",
        )
        if delivery_attempt == 0:
            raise WorkflowRepositoryCorruption("leased outbox row has no delivery attempt")
    else:
        raise WorkflowRepositoryCorruption("outbox lease metadata is only partially populated")

    acked_at_ms = row["acked_at_ms"]
    if acked_at_ms is not None:
        acked_at_ms = _require_int(
            acked_at_ms,
            minimum=0,
            field="outbox.acked_at_ms",
        )
        if lease_token is None:
            raise WorkflowRepositoryCorruption("acknowledged outbox row has no lease authority")

    event_json = row["event_json"]
    assert type(event_json) is bytes  # decode_event already enforced the BLOB type.
    return OutboxRecord(
        outbox_sequence=sequence,
        workflow_id=workflow_id,
        event_id=event_id,
        operation_id=operation_id,
        committed_revision=operation.committed_revision,
        event_index=event_index,
        event=event,
        event_json=event_json,
        created_at_ms=created_at_ms,
        leased_by=leased_by,
        lease_token=lease_token,
        lease_expires_at_ms=lease_expires_at_ms,
        delivery_attempt=delivery_attempt,
        acked_at_ms=acked_at_ms,
    )


def _require_int(value: object, *, minimum: int, field: str) -> int:
    """Require a non-boolean SQLite integer within a field's lower bound."""
    if type(value) is not int or value < minimum:
        raise WorkflowRepositoryCorruption(f"{field} is not an integer >= {minimum}")
    return value


def _require_text(value: object, *, field: str) -> str:
    """Require non-empty UTF-8 text from a persisted identity column."""
    if type(value) is not str or not value.strip():
        raise WorkflowRepositoryCorruption(f"{field} is not non-empty text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise WorkflowRepositoryCorruption(f"{field} is not UTF-8 encodable") from error
    return value


def _require_operation_id(value: object, *, field: str) -> str:
    """Apply the domain identifier rule to a persisted operation-like key."""
    try:
        return validate_operation_id(value, field=field)
    except ValueError as error:
        raise WorkflowRepositoryCorruption(f"{field} is invalid") from error


__all__ = [
    "LoadedWorkflow",
    "OperationRecord",
    "OutboxRecord",
    "load_and_audit_workflow",
    "validate_lease_token",
    "verify_duplicate_application",
    "verify_fresh_operation_delivery",
    "verify_operation_events",
]
