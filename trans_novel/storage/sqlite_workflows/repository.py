"""SQLite implementation of the framework-neutral workflow repository port.

Large artifacts are published before these methods are called.  Each fresh
patch then commits its compact state snapshot, normalized operation history,
and complete event outbox in one short ``BEGIN IMMEDIATE`` transaction.
Leasing and acknowledgement mutate outbox delivery metadata only.
"""

from __future__ import annotations

import copy
import math
import sqlite3
import time
from collections.abc import Sequence
from pathlib import Path
from typing import ContextManager

from ...domain.workflow import (
    validate_operation_id,
    validate_sha256,
)
from ...workflow.patches import PatchApplication, RevisionConflict, StatePatch
from ...workflow.reducers import apply_state_patch
from ...workflow.repository import (
    ClaimedWorkflowEvent,
    WorkflowAlreadyExists,
    WorkflowRepositoryCorruption,
)
from ...workflow.state import WorkflowState
from .codec import encode_event, encode_state
from .connection import (
    open_connection,
    prepare_database_path,
    read_transaction,
    validate_busy_timeout,
    write_transaction,
)
from .integrity import (
    load_and_audit_workflow,
    verify_duplicate_application,
    verify_fresh_operation_delivery,
)
from .outbox import acknowledge_claims, lease_events, normalize_claim_batch
from .schema import initialize_database, verify_database_schema

_MAX_CLAIM_LIMIT = 10_000
_MAX_SQLITE_INTEGER = 9_223_372_036_854_775_807


class SQLiteWorkflowRepository:
    """Persist workflow state and its durable outbox in a dedicated SQLite file.

    The adapter is intentionally local-machine storage: WAL databases must not
    be placed on filesystems that cannot provide SQLite's shared-memory and
    locking guarantees.  Repository objects are thread-safe because they hold
    no live connection; each public operation opens and closes its own handle.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_seconds: float = 30.0,
    ) -> None:
        """Bind a persistent database path and initialize or verify schema v1."""
        timeout_seconds, timeout_ms = validate_busy_timeout(busy_timeout_seconds)
        self.database_path = prepare_database_path(path)
        self._busy_timeout_seconds = timeout_seconds
        self._busy_timeout_ms = timeout_ms
        initialize_database(
            self.database_path,
            busy_timeout_seconds=timeout_seconds,
            busy_timeout_ms=timeout_ms,
        )

    def create(self, state: WorkflowState) -> WorkflowState:
        """Strictly insert one pristine state and return a detached snapshot."""
        normalized, state_json, state_sha256 = encode_state(state)
        if (
            normalized["revision"] != 0
            or normalized["applied_operations"]
            or normalized["claimed_event_ids"]
        ):
            raise ValueError(
                "create accepts only revision-zero state with empty operation and event ledgers"
            )
        # Legacy identity_version=1 exists only as an in-memory migration view.
        # New rows must use the format-bound identity produced by the v3 factory.
        if normalized["request"]["identity_version"] != 2:
            raise ValueError("create accepts only workflow identity_version=2")
        workflow_id = _validate_workflow_id(normalized["workflow_id"])
        timestamp_ms = _now_ms()

        with self._connection() as connection:
            with write_transaction(connection):
                verify_database_schema(connection)
                existing = connection.execute(
                    "SELECT 1 FROM workflow_snapshots WHERE workflow_id = ?",
                    (workflow_id,),
                ).fetchone()
                if existing is not None:
                    raise WorkflowAlreadyExists(f"workflow already exists: {workflow_id}")
                connection.execute(
                    """
                    INSERT INTO workflow_snapshots (
                        workflow_id, workflow_schema_version, revision,
                        state_json, state_sha256, created_at_ms, updated_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workflow_id,
                        normalized["schema_version"],
                        normalized["revision"],
                        state_json,
                        state_sha256,
                        timestamp_ms,
                        timestamp_ms,
                    ),
                )
                committed = load_and_audit_workflow(connection, workflow_id)
        return copy.deepcopy(committed.state)

    def get(self, workflow_id: str) -> WorkflowState:
        """Load a consistent snapshot and verify every normalized projection."""
        normalized_id = _validate_workflow_id(workflow_id)
        with self._connection() as connection:
            with read_transaction(connection):
                verify_database_schema(connection)
                loaded = load_and_audit_workflow(connection, normalized_id)
        return copy.deepcopy(loaded.state)

    def commit_patch(self, workflow_id: str, patch: StatePatch) -> PatchApplication:
        """Apply the reducer, then atomically persist a fresh or duplicate result."""
        normalized_id = _validate_workflow_id(workflow_id)
        if not isinstance(patch, StatePatch):
            raise TypeError("patch must be a StatePatch")

        with self._connection() as connection:
            with write_transaction(connection):
                verify_database_schema(connection)
                loaded = load_and_audit_workflow(connection, normalized_id)

                # Reducer duplicate detection deliberately precedes its revision
                # and lifecycle checks.  The repository must preserve that order.
                application = apply_state_patch(loaded.state, patch)
                if application.duplicate:
                    verify_duplicate_application(
                        loaded,
                        operation_id=patch.operation_id,
                        application=application,
                    )
                else:
                    self._commit_fresh_application(
                        connection,
                        workflow_id=normalized_id,
                        patch=patch,
                        application=application,
                    )
        return _detach_application(application)

    def claim_events(
        self,
        *,
        lease_owner: str,
        limit: int,
        lease_seconds: float,
    ) -> tuple[ClaimedWorkflowEvent, ...]:
        """Lease eligible events in stable enqueue order without changing state."""
        normalized_owner = _validate_actor_id(lease_owner, field="lease_owner")
        normalized_limit = _validate_claim_limit(limit)
        lease_duration_ms = _validate_lease_duration(lease_seconds)

        with self._connection() as connection:
            with write_transaction(connection):
                verify_database_schema(connection)
                # Lease authority begins only after SQLite grants the writer.
                # Time spent waiting for BEGIN IMMEDIATE must not consume or
                # accidentally extend the lease represented by this commit.
                now_ms = _now_ms()
                if lease_duration_ms > _MAX_SQLITE_INTEGER - now_ms:
                    raise ValueError("lease_seconds overflows SQLite's integer timestamp range")
                claims = lease_events(
                    connection,
                    lease_owner=normalized_owner,
                    limit=normalized_limit,
                    now_ms=now_ms,
                    expires_at_ms=now_ms + lease_duration_ms,
                )
        return claims

    def acknowledge_events(self, claims: Sequence[ClaimedWorkflowEvent]) -> None:
        """Atomically acknowledge a validated batch of current delivery leases."""
        normalized_claims = normalize_claim_batch(claims)
        if not normalized_claims:
            return

        with self._connection() as connection:
            with write_transaction(connection):
                verify_database_schema(connection)
                # Expiry is judged at the instant this transaction owns the
                # writer, never against a timestamp sampled before lock wait.
                acknowledge_claims(connection, normalized_claims, now_ms=_now_ms())

    def _commit_fresh_application(
        self,
        connection: sqlite3.Connection,
        *,
        workflow_id: str,
        patch: StatePatch,
        application: PatchApplication,
    ) -> None:
        """Write one reducer result into all three projections in this transaction."""
        candidate, state_json, state_sha256 = encode_state(application.state)
        committed_revision = candidate["revision"]
        current_revision = committed_revision - 1
        fingerprint = candidate["applied_operations"].get(patch.operation_id)
        try:
            normalized_fingerprint = validate_sha256(
                fingerprint,
                field="committed patch fingerprint",
            )
        except ValueError as error:  # pragma: no cover - reducer owns this invariant.
            raise WorkflowRepositoryCorruption(
                "reducer omitted its operation fingerprint"
            ) from error
        timestamp_ms = _now_ms()

        # Keep the revision predicate even under BEGIN IMMEDIATE: it is the
        # final defense if transaction behavior changes in a future adapter.
        cursor = connection.execute(
            """
            UPDATE workflow_snapshots
            SET workflow_schema_version = ?, revision = ?,
                state_json = ?, state_sha256 = ?,
                updated_at_ms = CASE
                    WHEN updated_at_ms > ? THEN updated_at_ms ELSE ?
                END
            WHERE workflow_id = ? AND revision = ?
            """,
            (
                candidate["schema_version"],
                committed_revision,
                state_json,
                state_sha256,
                timestamp_ms,
                timestamp_ms,
                workflow_id,
                current_revision,
            ),
        )
        if cursor.rowcount != 1:
            raise RevisionConflict(
                f"workflow revision changed before commit: expected {current_revision}"
            )
        connection.execute(
            """
            INSERT INTO workflow_operations (
                workflow_id, operation_id, committed_revision,
                patch_fingerprint, event_count, committed_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                workflow_id,
                patch.operation_id,
                committed_revision,
                normalized_fingerprint,
                len(application.events),
                timestamp_ms,
            ),
        )
        for event_index, event in enumerate(application.events):
            normalized_event, event_json, event_sha256 = encode_event(event)
            connection.execute(
                """
                INSERT INTO workflow_outbox (
                    workflow_id, event_id, operation_id, event_index,
                    event_json, event_sha256, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workflow_id,
                    normalized_event["event_id"],
                    patch.operation_id,
                    event_index,
                    event_json,
                    event_sha256,
                    timestamp_ms,
                ),
            )

        committed = load_and_audit_workflow(connection, workflow_id)
        if committed.state != candidate:
            raise WorkflowRepositoryCorruption("committed state differs from reducer candidate")
        verify_fresh_operation_delivery(
            committed,
            operation_id=patch.operation_id,
            events=application.events,
        )

    def _connection(self) -> ContextManager[sqlite3.Connection]:
        """Open one configured short-lived handle for a public operation."""
        return open_connection(
            self.database_path,
            busy_timeout_seconds=self._busy_timeout_seconds,
            busy_timeout_ms=self._busy_timeout_ms,
        )


def _validate_workflow_id(value: object) -> str:
    """Use the shared stable identifier grammar for repository lookup keys."""
    try:
        return validate_operation_id(value, field="workflow_id")
    except ValueError as error:
        raise ValueError("workflow_id is invalid") from error


def _validate_actor_id(value: object, *, field: str) -> str:
    """Validate operation and lease-owner identifiers through one domain rule."""
    try:
        return validate_operation_id(value, field=field)
    except ValueError as error:
        raise ValueError(f"{field} is invalid") from error


def _validate_claim_limit(value: object) -> int:
    """Bound one dispatcher transaction so delivery cannot monopolize the writer."""
    limit = _require_integer(value, minimum=1, field="limit")
    if limit > _MAX_CLAIM_LIMIT:
        raise ValueError(f"limit cannot exceed {_MAX_CLAIM_LIMIT}")
    return limit


def _validate_lease_duration(value: object) -> int:
    """Convert a positive finite lease duration to at least one millisecond."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise ValueError("lease_seconds must be a positive finite number")
    milliseconds = math.ceil(float(value) * 1000)
    if milliseconds > _MAX_SQLITE_INTEGER:
        raise ValueError("lease_seconds exceeds SQLite's integer range")
    return max(1, milliseconds)


def _require_integer(value: object, *, minimum: int, field: str) -> int:
    """Reject booleans and enforce the lower bound for public integer inputs."""
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _detach_application(application: PatchApplication) -> PatchApplication:
    """Prevent callers from mutating reducer-owned nested state and effects."""
    return PatchApplication(
        state=copy.deepcopy(application.state),
        events=copy.deepcopy(application.events),
        created_artifacts=copy.deepcopy(application.created_artifacts),
        duplicate=application.duplicate,
    )


def _now_ms() -> int:
    """Return a non-negative wall-clock timestamp suitable for durable leases."""
    return max(0, time.time_ns() // 1_000_000)


__all__ = ["SQLiteWorkflowRepository"]
