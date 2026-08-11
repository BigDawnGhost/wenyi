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
from ...workflow.repository import WorkflowRepositoryCorruption
from ...workflow.state import WorkflowState
from ...workflow.validation import validate_workflow_state


def encode_state(state: Mapping[str, object]) -> tuple[WorkflowState, bytes, str]:
    """Validate, detach, and encode a complete workflow snapshot."""
    validate_workflow_state(state)
    normalized = copy_json_value(dict(state), field="WorkflowState")
    encoded = _canonical_bytes(normalized)
    return cast(WorkflowState, normalized), encoded, _sha256(encoded)


def decode_state(value: object, digest: object) -> WorkflowState:
    """Verify a stored snapshot digest, canonical form, and domain invariants."""
    decoded = _decode_canonical(value, digest, field="workflow state")
    if not isinstance(decoded, dict):
        raise WorkflowRepositoryCorruption("persisted workflow state is not a JSON object")
    try:
        validate_workflow_state(decoded)
    except (KeyError, TypeError, ValueError) as error:
        raise WorkflowRepositoryCorruption("persisted workflow state is invalid") from error
    return cast(WorkflowState, decoded)


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
    "encode_event",
    "encode_state",
]
