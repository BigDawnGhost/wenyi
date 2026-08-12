"""Canonical UTF-8 JSON codecs for persisted workflow state and events."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any, cast

from ...domain.workflow import (
    WorkflowEvent,
    copy_json_value,
    validate_sha256,
    validate_workflow_event,
)
from ...workflow.repository import (
    UnsupportedWorkflowStateSchema,
    WorkflowRepositoryCorruption,
)
from ...workflow.state import WORKFLOW_SCHEMA_VERSION, WorkflowState
from ...workflow.validation import validate_workflow_state


def encode_state(state: Mapping[str, object]) -> tuple[WorkflowState, bytes, str]:
    """Validate, detach, and encode a complete workflow snapshot."""
    validate_workflow_state(state)
    normalized = copy_json_value(dict(state), field="WorkflowState")
    encoded = _canonical_bytes(normalized)
    return cast(WorkflowState, normalized), encoded, _sha256(encoded)


def decode_state(value: object, digest: object) -> WorkflowState:
    """Verify stored bytes and migrate lossless schema-v1 snapshots in memory."""
    return decode_state_with_source_version(value, digest)[0]


def decode_state_with_source_version(
    value: object,
    digest: object,
) -> tuple[WorkflowState, int]:
    """Decode state and retain the verified pre-migration schema for column audit."""
    decoded = _decode_canonical(value, digest, field="workflow state")
    if not isinstance(decoded, dict):
        raise WorkflowRepositoryCorruption("persisted workflow state is not a JSON object")
    source_version = decoded.get("schema_version")
    if type(source_version) is not int:
        raise WorkflowRepositoryCorruption("persisted workflow schema_version is invalid")
    decoded = _migrate_state(decoded)
    try:
        validate_workflow_state(decoded)
    except (KeyError, TypeError, ValueError) as error:
        raise WorkflowRepositoryCorruption("persisted workflow state is invalid") from error
    return cast(WorkflowState, decoded), source_version


def _migrate_state(state: dict[str, Any]) -> dict[str, Any]:
    """Upgrade an old compact state only when no partial batch evidence is lost.

    SQLite ``user_version`` remains 1 because no table, index, or transactional
    guarantee changes.  This is an application JSON-schema migration applied
    after the original bytes and canonical digest have already been verified.
    """
    version = state.get("schema_version")
    if type(version) is not int:
        raise WorkflowRepositoryCorruption("persisted workflow schema_version is invalid")
    if version == WORKFLOW_SCHEMA_VERSION:
        return state
    if version != 1:
        raise UnsupportedWorkflowStateSchema(
            f"workflow state schema {version} has no migration to {WORKFLOW_SCHEMA_VERSION}"
        )

    cursor = state.get("cursor")
    translation = state.get("translation")
    if not isinstance(cursor, dict) or not isinstance(translation, dict):
        raise WorkflowRepositoryCorruption("persisted workflow v1 slices are invalid")
    offset = cursor.get("segment_offset")
    if type(offset) is not int and offset is not None:
        raise WorkflowRepositoryCorruption("persisted workflow v1 segment_offset is invalid")
    if isinstance(offset, int) and not isinstance(offset, bool) and offset > 0:
        raise UnsupportedWorkflowStateSchema(
            "workflow state schema 1 with segment_offset > 0 cannot recover "
            "translation batch artifact identity"
        )
    if "batch_artifacts" in translation:
        raise WorkflowRepositoryCorruption(
            "persisted workflow schema 1 unexpectedly contains batch_artifacts"
        )

    # Completed chapters are already protected by immutable chapter artifacts;
    # pending and offset-zero work has no durable partial range to reconstruct.
    migrated = cast(dict[str, Any], copy_json_value(state, field="workflow state v1"))
    migrated["schema_version"] = WORKFLOW_SCHEMA_VERSION
    migrated_cursor = cast(dict[str, Any], migrated["cursor"])
    if (
        migrated_cursor.get("phase") == "translate_chapters"
        and migrated_cursor.get("chapter_index") is not None
        and migrated_cursor.get("segment_offset") is None
    ):
        # Schema v1 used None for both "not positioned" and "chapter starts at
        # zero".  In the translation phase a non-null chapter disambiguates the
        # latter, so v2 can normalize it losslessly to the canonical zero prefix.
        migrated_cursor["segment_offset"] = 0
    migrated_translation = cast(dict[str, Any], migrated["translation"])
    migrated_translation["batch_artifacts"] = {}
    return migrated


def encode_event(event: Mapping[str, object]) -> tuple[WorkflowEvent, bytes, str]:
    """Validate, detach, and encode one complete workflow event."""
    normalized = validate_workflow_event(event)
    encoded = _canonical_bytes(normalized)
    return normalized, encoded, _sha256(encoded)


def decode_event(value: object, digest: object) -> WorkflowEvent:
    """Verify a stored event digest, canonical form, and domain shape."""
    decoded = _decode_canonical(value, digest, field="workflow event")
    if not isinstance(decoded, dict):
        raise WorkflowRepositoryCorruption("persisted workflow event is not a JSON object")
    try:
        return validate_workflow_event(decoded)
    except (KeyError, TypeError, ValueError) as error:
        raise WorkflowRepositoryCorruption("persisted workflow event is invalid") from error


def canonical_event_bytes(event: Mapping[str, object]) -> bytes:
    """Return canonical bytes for exact replay and acknowledgement comparisons."""
    return encode_event(event)[1]


def _decode_canonical(value: object, digest: object, *, field: str) -> Any:
    """Reject non-BLOB, corrupt, non-UTF-8, or non-canonical persisted JSON."""
    if type(value) is not bytes:
        raise WorkflowRepositoryCorruption(f"persisted {field} is not a SQLite BLOB")
    try:
        expected_digest = validate_sha256(digest, field=f"{field} digest")
    except ValueError as error:
        raise WorkflowRepositoryCorruption(f"persisted {field} digest is invalid") from error
    actual_digest = _sha256(value)
    if not hmac.compare_digest(actual_digest, expected_digest):
        raise WorkflowRepositoryCorruption(f"persisted {field} digest does not match its bytes")

    try:
        decoded = json.loads(value.decode("utf-8"))
        normalized = copy_json_value(decoded, field=field)
        canonical = _canonical_bytes(normalized)
    except (UnicodeDecodeError, ValueError) as error:
        raise WorkflowRepositoryCorruption(f"persisted {field} is not stable UTF-8 JSON") from error
    if canonical != value:
        raise WorkflowRepositoryCorruption(f"persisted {field} JSON is not canonical")
    return normalized


def _canonical_bytes(value: object) -> bytes:
    """Serialize an already normalized JSON value with the one repository encoding."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    """Return the lowercase digest stored next to canonical JSON bytes."""
    return hashlib.sha256(value).hexdigest()


__all__ = [
    "canonical_event_bytes",
    "decode_event",
    "decode_state",
    "decode_state_with_source_version",
    "encode_event",
    "encode_state",
]
