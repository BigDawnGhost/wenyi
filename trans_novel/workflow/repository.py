"""Framework-neutral persistence ports for workflow state and artifacts.

Large payloads cross the boundary through :class:`ArtifactRef`; workflow
snapshots and their domain-event outbox cross through :class:`WorkflowRepository`.
Concrete adapters may use a filesystem, SQLite, object storage, or another
backend, but backend paths, clients, connections, and open handles must never
enter workflow state.

This module deliberately defines contracts only.  It does not depend on
RunStore, a CLI, or a graph runtime, and it does not turn current JSONL logs
into a second source of truth.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import BinaryIO, ContextManager, Protocol

from ..domain.workflow import ArtifactRef, WorkflowEvent
from .patches import PatchApplication, StatePatch
from .state import WorkflowState


class ArtifactStoreError(Exception):
    """Base class for failures reported through the artifact-store boundary."""


class ArtifactNotFound(ArtifactStoreError):
    """Raised when a valid artifact reference has no stored payload."""


class ArtifactCorruption(ArtifactStoreError):
    """Raised when stored bytes disagree with an artifact reference."""


class InvalidArtifactReference(ArtifactStoreError):
    """Raised when an ``ArtifactRef`` is malformed or unsupported by a store."""


class WorkflowRepositoryError(Exception):
    """Base class for failures reported by the workflow-repository boundary."""


class WorkflowNotFound(WorkflowRepositoryError):
    """Raised when no workflow snapshot exists for a valid workflow identity."""


class WorkflowAlreadyExists(WorkflowRepositoryError):
    """Raised when strict creation finds an existing workflow identity."""


class WorkflowRepositoryCorruption(WorkflowRepositoryError):
    """Raised when persisted state, operation history, and outbox disagree."""


class WorkflowRepositoryBusy(WorkflowRepositoryError):
    """Raised when a backend cannot acquire its write transaction before timeout."""


class UnsupportedWorkflowRepositorySchema(WorkflowRepositoryError):
    """Raised when an adapter finds a repository schema newer than it supports."""


class UnsupportedWorkflowStateSchema(WorkflowRepositoryError):
    """Raised when a stored workflow snapshot has no lossless state migration."""


class OutboxLeaseLost(WorkflowRepositoryError):
    """Raised when an event lease was replaced before its claimant acknowledged it."""


@dataclass(frozen=True, slots=True)
class ClaimedWorkflowEvent:
    """Detached domain event plus the lease authority required to acknowledge it.

    ``lease_token`` changes on every lease, including when the same worker
    reclaims an expired event.  This prevents a delayed acknowledgement from
    an older delivery attempt from confirming a newer claim (the ABA problem).
    """

    workflow_id: str
    operation_id: str
    committed_revision: int
    event_index: int
    event: WorkflowEvent
    leased_by: str
    lease_token: str
    delivery_attempt: int
    lease_expires_at_ms: int


class ArtifactStore(Protocol):
    """Port for publishing and reading immutable, content-addressed artifacts.

    Implementations must treat a returned :class:`ArtifactRef` as immutable:
    publishing the same bytes again may reuse an existing object, but must not
    mutate bytes already addressable through an earlier reference.

    Methods accept or return ``ArtifactRef`` whenever an artifact crosses the
    storage boundary.  No implementation-specific path, key, or client object
    may leak into workflow state.
    """

    def put_bytes(self, data: bytes, *, media_type: str) -> ArtifactRef:
        """Publish ``data`` and return its detached immutable reference.

        The method must not retain a mutable view of caller-owned data.  A
        successful return means the referenced bytes are available for later
        verification and reading.
        """
        ...

    def put_stream(self, stream: BinaryIO, *, media_type: str) -> ArtifactRef:
        """Publish bytes from the stream's current position through EOF.

        ``stream`` need not be seekable.  The caller retains ownership of the
        input stream, so the store must not close it.
        """
        ...

    def put_json(
        self,
        value: object,
        *,
        media_type: str = "application/json",
    ) -> ArtifactRef:
        """Publish a stable JSON value using deterministic UTF-8 encoding.

        Implementations must reject values that cannot make a lossless stable
        JSON round trip, including non-finite floats and non-string mapping
        keys.  Equivalent accepted values must produce the same bytes and thus
        the same content identity.
        """
        ...

    def verify(self, ref: ArtifactRef) -> ArtifactRef:
        """Fully verify ``ref`` and return a detached normalized reference.

        Verification covers the reference shape, backend address, byte count,
        and SHA-256 digest.  Invalid references, missing objects, and content
        mismatches are reported as ``InvalidArtifactReference``,
        ``ArtifactNotFound``, and ``ArtifactCorruption`` respectively.
        """
        ...

    def open_binary(self, ref: ArtifactRef) -> ContextManager[BinaryIO]:
        """Return a context manager that opens the bytes identified by ``ref``.

        The reference must be validated and missing content must raise
        ``ArtifactNotFound``.  Opening is not a substitute for ``verify``:
        callers that require a complete digest check must verify explicitly.
        The context manager owns and closes the returned reader.
        """
        ...

    def contains(self, ref: ArtifactRef) -> bool:
        """Return whether the backend contains the object addressed by ``ref``.

        A malformed or backend-incompatible reference raises
        ``InvalidArtifactReference``.  This is an existence probe rather than a
        content-integrity check; use ``verify`` before trusting stored bytes.
        """
        ...


class WorkflowRepository(Protocol):
    """Port for durable workflow snapshots and their transactional event outbox.

    State, operation history, and complete event payloads are one atomic commit.
    Event leasing and acknowledgement are delivery metadata only: they must not
    change ``WorkflowState`` or advance its revision.
    """

    def create(self, state: WorkflowState) -> WorkflowState:
        """Strictly insert a pristine initial state and return a detached copy.

        ``state`` must have revision zero and empty ``applied_operations`` and
        ``claimed_event_ids`` ledgers.  Its source artifact must already be in
        the artifact store.  Existing workflow identities raise
        :class:`WorkflowAlreadyExists`; get-or-create policy belongs to the
        application layer so identity conflicts are never hidden.
        """
        ...

    def get(self, workflow_id: str) -> WorkflowState:
        """Load, cross-check, and return a detached complete workflow snapshot."""
        ...

    def commit_patch(self, workflow_id: str, patch: StatePatch) -> PatchApplication:
        """Apply ``patch`` and atomically commit state, operation, and outbox rows.

        Every ``patch.created_artifacts`` reference must already have been
        published by the artifact store; this transaction never performs blob
        I/O.  A revision conflict may therefore leave an immutable unreferenced
        artifact, which a later garbage collector may safely reclaim.

        Reducer errors such as ``RevisionConflict`` and ``OperationConflict``
        propagate unchanged.  Replaying an identical patch does not reset an
        event's acknowledgement, lease, or delivery-attempt count.
        """
        ...

    def claim_events(
        self,
        *,
        lease_owner: str,
        limit: int,
        lease_seconds: float,
    ) -> tuple[ClaimedWorkflowEvent, ...]:
        """Lease pending or expired outbox events in stable enqueue order.

        A process crash after this method returns only delays delivery until the
        lease expires.  The consumer must call an ``append_if_absent`` event sink
        keyed by ``(workflow_id, event_id)`` before acknowledging the claims.
        Selection order is deterministic; parallel consumers may finish their
        sink writes in a different order.
        """
        ...

    def acknowledge_events(self, claims: Sequence[ClaimedWorkflowEvent]) -> None:
        """Atomically acknowledge a batch after its idempotent sink has succeeded.

        Repeating the same acknowledged claim token is a no-op.  Stale,
        mismatched, expired, or superseded authority raises
        :class:`OutboxLeaseLost`; a missing retained projection raises
        :class:`WorkflowRepositoryCorruption`.  Either failure rolls back the
        entire acknowledgement batch.
        """
        ...


__all__ = [
    "ArtifactCorruption",
    "ArtifactNotFound",
    "ArtifactStore",
    "ArtifactStoreError",
    "ClaimedWorkflowEvent",
    "InvalidArtifactReference",
    "OutboxLeaseLost",
    "UnsupportedWorkflowRepositorySchema",
    "UnsupportedWorkflowStateSchema",
    "WorkflowAlreadyExists",
    "WorkflowNotFound",
    "WorkflowRepository",
    "WorkflowRepositoryBusy",
    "WorkflowRepositoryCorruption",
    "WorkflowRepositoryError",
]
