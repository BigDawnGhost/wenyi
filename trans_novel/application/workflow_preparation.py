"""Repository-authoritative preparation phase for the new workflow runtime.

The runner coordinates ports only.  It does not import legacy readers,
``RunStore``, concrete storage, or LangGraph.  Source parsing/conversion lives
behind ``SourceDocumentNormalizer``; durable business progress is committed
only through ``WorkflowRepository`` and large payloads live in ``ArtifactStore``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

from ..domain.normalized_document import (
    NORMALIZED_DOCUMENT_MEDIA_TYPE,
    NormalizedDocumentCounts,
    NormalizedDocumentV1,
    decode_normalized_document_v1,
    encode_normalized_document_v1,
    normalized_document_v1_counts,
    validate_normalized_document_v1,
)
from ..domain.workflow import (
    ArtifactRef,
    StageStatus,
    WorkflowPhase,
    WorkflowStatus,
    copy_json_value,
    validate_artifact_ref,
)
from ..workflow.patches import StatePatch
from ..workflow.repository import (
    ArtifactCorruption,
    ArtifactNotFound,
    ArtifactStore,
    ArtifactStoreError,
    InvalidArtifactReference,
    WorkflowRepository,
)
from ..workflow.state import WorkflowState
from .runtime import ExecutionContext

_PREPARE_START_OPERATION = "prepare:start"
_PREPARE_COMPLETE_OPERATION = "prepare:complete"


@dataclass(frozen=True, slots=True)
class SourceNormalizationResult:
    """Reader-ready source reference plus its source-only logical document.

    ``normalized_source`` and ``document`` have different responsibilities:
    the first is suitable for later reader/export recovery, while the second
    becomes a canonical logical-document artifact used by workflow phases.
    """

    normalized_source: ArtifactRef
    document: NormalizedDocumentV1


class SourceDocumentNormalizer(Protocol):
    """Normalize one verified source without exposing a concrete reader here."""

    def normalize(
        self,
        *,
        source_artifact: ArtifactRef,
        source_sha256: str,
        source_format: str,
        source_lang: str,
        artifacts: ArtifactStore,
        context: ExecutionContext,
    ) -> SourceNormalizationResult:
        """Return source recovery data and one detached normalized document."""
        ...


class PreparationFailure(Exception):
    """A safe preparation failure that may cross the durable state boundary.

    The stable fields deliberately exclude exception causes, tracebacks, source
    paths, provider responses, and document text.  ``details`` must contain only
    stable JSON values and is copied before it can enter ``WorkflowState``.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool,
        details: Mapping[str, object] | None = None,
    ) -> None:
        stable_code = _require_safe_failure_text(code, field="code")
        stable_message = _require_safe_failure_text(message, field="message")
        if type(retryable) is not bool:
            raise ValueError("preparation failure retryable must be a native bool")
        if details is not None and not isinstance(details, Mapping):
            raise ValueError("preparation failure details must be a mapping")
        stable_details = copy_json_value(
            dict(details) if details is not None else {},
            field="PreparationFailure.details",
        )
        if not isinstance(stable_details, dict):  # pragma: no cover - dict input guarantees this.
            raise ValueError("preparation failure details must be a mapping")

        super().__init__(stable_message)
        self.code = stable_code
        self.safe_message = stable_message
        self.retryable = retryable
        self.details = stable_details


@dataclass(frozen=True, slots=True)
class PreparationPhaseRunner:
    """Complete or safely fail one authoritative preparation phase invocation."""

    normalizer: SourceDocumentNormalizer

    def __call__(
        self,
        state: WorkflowState,
        *,
        repository: WorkflowRepository,
        artifacts: ArtifactStore,
        context: ExecutionContext,
    ) -> None:
        """Run ``start -> normalize -> publish -> complete`` durably.

        Artifact publication precedes the final SQLite commit.  A crash in that
        interval can leave an immutable unreferenced object, but can never leave
        workflow state pointing at missing bytes.  Replaying the stable operation
        IDs is idempotent; repository conflicts are propagated to the caller.
        """
        self._require_prepare_state(state)
        workflow_id = state["workflow_id"]

        # Pending workflows need an explicit control commit before performing
        # expensive parsing.  A lost response is harmless: an exact replay sees
        # the operation ledger before its stale expected revision.
        if state["status"] == WorkflowStatus.PENDING.value:
            repository.commit_patch(workflow_id, _start_patch(state["revision"]))

        # Never continue from the caller-owned snapshot after a commit.  This
        # reload is the sole revision and request identity used below.
        current = repository.get(workflow_id)
        if not _is_active_prepare(current):
            # Another worker may have committed the same stable start operation
            # and completed or stopped preparation before this reload.  The
            # caller will perform its own authoritative post-run read.
            return
        self._require_running_prepare_state(current)

        try:
            result = self._normalize(current, artifacts=artifacts, context=context)
            document_ref, document, counts = self._publish_and_cold_verify_document(
                current,
                result,
                artifacts=artifacts,
            )
        except PreparationFailure as failure:
            self._commit_failure(current, failure, repository=repository)
            return
        except ArtifactStoreError as error:
            # Storage exceptions may contain host paths in their cause chain.
            # Map only the stable category and never persist ``str(error)``.
            failure = _safe_artifact_failure(error)
            self._commit_failure(current, failure, repository=repository)
            return

        created_artifacts = _created_artifacts(
            source=current["request"]["source_artifact"],
            normalized=result.normalized_source,
            document=document_ref,
        )
        repository.commit_patch(
            workflow_id,
            StatePatch(
                operation_id=_PREPARE_COMPLETE_OPERATION,
                expected_revision=current["revision"],
                updates={
                    "cursor": {
                        "phase": WorkflowPhase.UNDERSTAND.value,
                        "chapter_index": None,
                        "segment_offset": None,
                        "review_round": None,
                    },
                    "book": {
                        "document_artifact": document_ref,
                        "chapter_count": counts.chapter_count,
                        "source_segment_count": counts.source_segment_count,
                    },
                    "preparation": {
                        "status": StageStatus.COMPLETED.value,
                        "normalized_source": result.normalized_source,
                    },
                },
                events=(
                    {
                        "event_id": "prepare-completed",
                        "event_type": "preparation.completed",
                        "payload": {
                            "chapter_count": counts.chapter_count,
                            "source_segment_count": counts.source_segment_count,
                        },
                    },
                ),
                created_artifacts=created_artifacts,
            ),
        )

        # ``document`` is intentionally kept local: phase consumers reload its
        # immutable reference instead of receiving an in-memory second truth.
        del document

    def _normalize(
        self,
        state: WorkflowState,
        *,
        artifacts: ArtifactStore,
        context: ExecutionContext,
    ) -> SourceNormalizationResult:
        """Verify the request source, call the port, and bind its identities."""
        request = state["request"]
        source_ref = artifacts.verify(request["source_artifact"])
        if source_ref != request["source_artifact"]:
            raise PreparationFailure(
                "source_reference_mismatch",
                "verified source reference does not match the workflow request",
                retryable=False,
            )

        try:
            result = self.normalizer.normalize(
                source_artifact=source_ref,
                source_sha256=request["source_sha256"],
                source_format=request["source_format"],
                source_lang=request["source_lang"],
                artifacts=artifacts,
                context=context,
            )
        except PreparationFailure:
            raise
        except ArtifactStoreError:
            # A normalizer may publish a converted source through the injected
            # store.  Keep storage failure categories intact for safe mapping.
            raise

        if not isinstance(result, SourceNormalizationResult):
            raise PreparationFailure(
                "invalid_normalization_result",
                "source normalizer returned an invalid result",
                retryable=False,
            )
        try:
            normalized_ref = validate_artifact_ref(result.normalized_source)
        except (TypeError, ValueError) as error:
            raise PreparationFailure(
                "invalid_normalized_source_reference",
                "source normalizer returned an invalid artifact reference",
                retryable=False,
            ) from error
        verified_normalized = artifacts.verify(normalized_ref)
        if verified_normalized != normalized_ref:
            raise PreparationFailure(
                "normalized_source_reference_mismatch",
                "verified normalized source reference changed identity",
                retryable=False,
            )
        document = self._validate_document_identity(state, result.document)
        return SourceNormalizationResult(
            normalized_source=verified_normalized,
            document=document,
        )

    @staticmethod
    def _validate_document_identity(
        state: WorkflowState,
        value: Mapping[str, object],
    ) -> NormalizedDocumentV1:
        """Validate a detached document and bind it to the immutable request."""
        try:
            document = validate_normalized_document_v1(value)
        except (TypeError, ValueError) as error:
            raise PreparationFailure(
                "invalid_normalized_document",
                "normalized document is invalid",
                retryable=False,
            ) from error
        request = state["request"]
        expected = (
            request["source_sha256"],
            request["source_format"],
            request["source_lang"],
        )
        actual = (
            document["source_sha256"],
            document["source_format"],
            document["source_lang"],
        )
        if actual != expected:
            raise PreparationFailure(
                "normalized_document_identity_mismatch",
                "normalized document identity does not match the workflow request",
                retryable=False,
            )
        return document

    def _publish_and_cold_verify_document(
        self,
        state: WorkflowState,
        result: SourceNormalizationResult,
        *,
        artifacts: ArtifactStore,
    ) -> tuple[ArtifactRef, NormalizedDocumentV1, NormalizedDocumentCounts]:
        """Publish canonical bytes, then verify and decode from a fresh reader."""
        encoded = encode_normalized_document_v1(result.document)
        published = artifacts.put_bytes(encoded, media_type=NORMALIZED_DOCUMENT_MEDIA_TYPE)
        document_ref = artifacts.verify(published)
        if document_ref["media_type"] != NORMALIZED_DOCUMENT_MEDIA_TYPE:
            raise PreparationFailure(
                "normalized_document_media_type_mismatch",
                "normalized document artifact has an unexpected media type",
                retryable=False,
            )
        try:
            with artifacts.open_binary(document_ref) as reader:
                cold_document = decode_normalized_document_v1(reader.read())
        except (TypeError, ValueError) as error:
            # A successful publish followed by an invalid cold read is a
            # durable-content contract failure, not a parser diagnostic.
            raise PreparationFailure(
                "invalid_published_normalized_document",
                "published normalized document could not be verified",
                retryable=False,
            ) from error
        cold_document = self._validate_document_identity(state, cold_document)
        if cold_document != result.document:
            raise PreparationFailure(
                "normalized_document_content_mismatch",
                "published normalized document differs from its source value",
                retryable=False,
            )
        counts = normalized_document_v1_counts(cold_document)
        return document_ref, cold_document, counts

    @staticmethod
    def _commit_failure(
        state: WorkflowState,
        failure: PreparationFailure,
        *,
        repository: WorkflowRepository,
    ) -> None:
        """Commit a control-only failure without claiming orphaned artifacts."""
        revision = state["revision"]
        repository.commit_patch(
            state["workflow_id"],
            StatePatch(
                operation_id=f"prepare:failed:{revision}",
                expected_revision=revision,
                updates={
                    "status": WorkflowStatus.FAILED.value,
                    "failure": {
                        "code": failure.code,
                        "message": failure.safe_message,
                        "retryable": failure.retryable,
                        "details": failure.details,
                    },
                    "preparation": {
                        **state["preparation"],
                        "status": StageStatus.FAILED.value,
                    },
                },
                events=(
                    {
                        "event_id": f"prepare-failed-{revision}",
                        "event_type": "workflow.failed",
                        "payload": {
                            "code": failure.code,
                            "retryable": failure.retryable,
                        },
                    },
                ),
            ),
        )

    @staticmethod
    def _require_prepare_state(state: WorkflowState) -> None:
        """Reject dispatch mistakes before performing I/O or repository writes."""
        if state["cursor"]["phase"] != WorkflowPhase.PREPARE.value:
            raise ValueError("preparation runner requires cursor.phase='prepare'")
        if state["status"] not in {
            WorkflowStatus.PENDING.value,
            WorkflowStatus.RUNNING.value,
        }:
            raise ValueError("preparation runner requires an active workflow")

    @staticmethod
    def _require_running_prepare_state(state: WorkflowState) -> None:
        """Require the unambiguous post-start snapshot used for normalization."""
        PreparationPhaseRunner._require_prepare_state(state)
        if state["status"] != WorkflowStatus.RUNNING.value:
            raise ValueError("preparation start did not leave workflow running")
        if state["preparation"]["status"] != StageStatus.RUNNING.value:
            raise ValueError("preparation start did not leave its stage running")


def _start_patch(expected_revision: int) -> StatePatch:
    """Build the stable pending-to-running preparation operation."""
    return StatePatch(
        operation_id=_PREPARE_START_OPERATION,
        expected_revision=expected_revision,
        updates={
            "status": WorkflowStatus.RUNNING.value,
            "preparation": {
                "status": StageStatus.RUNNING.value,
                "normalized_source": None,
            },
        },
        events=(
            {
                "event_id": "prepare-started",
                "event_type": "preparation.started",
                "payload": {"phase": WorkflowPhase.PREPARE.value},
            },
        ),
    )


def _is_active_prepare(state: WorkflowState) -> bool:
    """Return whether this snapshot still belongs to the executable prepare phase."""
    return state["cursor"]["phase"] == WorkflowPhase.PREPARE.value and state["status"] in {
        WorkflowStatus.PENDING.value,
        WorkflowStatus.RUNNING.value,
    }


def _safe_artifact_failure(error: ArtifactStoreError) -> PreparationFailure:
    """Map storage exception classes to stable messages without path leakage."""
    if isinstance(error, ArtifactNotFound):
        return PreparationFailure(
            "source_artifact_not_found",
            "a required preparation artifact is missing",
            retryable=False,
        )
    if isinstance(error, ArtifactCorruption):
        return PreparationFailure(
            "source_artifact_corrupt",
            "a required preparation artifact failed integrity verification",
            retryable=False,
        )
    if isinstance(error, InvalidArtifactReference):
        return PreparationFailure(
            "invalid_artifact_reference",
            "preparation received an invalid artifact reference",
            retryable=False,
        )
    return PreparationFailure(
        "artifact_store_unavailable",
        "artifact storage is temporarily unavailable",
        retryable=True,
    )


def _require_safe_failure_text(value: object, *, field: str) -> str:
    """Validate one stable UTF-8 failure field before it reaches state."""
    if type(value) is not str or not value.strip():
        raise ValueError(f"preparation failure {field} must be a non-empty string")
    stable = value.strip()
    try:
        stable.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"preparation failure {field} must be valid UTF-8") from None
    return stable


def _created_artifacts(
    *,
    source: ArtifactRef,
    normalized: ArtifactRef,
    document: ArtifactRef,
) -> tuple[ArtifactRef, ...]:
    """Return newly published refs in stable order without identity duplicates."""
    created: list[ArtifactRef] = []
    seen: set[tuple[str, str]] = {(source["uri"], source["sha256"])}
    for ref in (normalized, document):
        identity = (ref["uri"], ref["sha256"])
        if identity in seen:
            continue
        seen.add(identity)
        created.append(ref)
    return tuple(created)


__all__ = [
    "PreparationFailure",
    "PreparationPhaseRunner",
    "SourceDocumentNormalizer",
    "SourceNormalizationResult",
]
