"""Immutable translation-batch artifact contract.

The payload records only translated targets and the stable coordinates needed
to attach them to ``Chapter.text_segments``.  It deliberately excludes live
segment objects, clients, stores, and graph-runtime values so the same bytes
can be verified by any execution backend.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import TypedDict, cast

from .workflow import copy_json_value, validate_sha256

TRANSLATION_BATCH_ARTIFACT_SCHEMA_VERSION = 1
TRANSLATION_BATCH_ARTIFACT_MEDIA_TYPE = "application/vnd.wenyi.translation-batch.v1+json"

_BATCH_KEY_PATTERN = re.compile(
    r"^(?P<chapter>0|[1-9][0-9]*):"
    r"(?P<start>0|[1-9][0-9]*):"
    r"(?P<stop>0|[1-9][0-9]*)$",
    flags=re.ASCII,
)
_WORKFLOW_ID_PATTERN = re.compile(r"^wf-[0-9a-f]{64}$", flags=re.ASCII)


class TranslationBatchArtifact(TypedDict):
    """Detached targets for one non-empty contiguous text-segment range."""

    schema_version: int
    workflow_id: str
    document_sha256: str
    chapter_index: int
    start_index: int
    stop_index: int
    targets: list[str]


def build_translation_batch_key(
    chapter_index: object,
    start_index: object,
    stop_index: object,
) -> str:
    """Build the canonical ``chapter:start:stop`` checkpoint key."""
    chapter = _require_non_negative_int(chapter_index, field="chapter_index")
    start = _require_non_negative_int(start_index, field="start_index")
    stop = _require_non_negative_int(stop_index, field="stop_index")
    if start >= stop:
        raise ValueError("translation batch range must satisfy start_index < stop_index")
    return f"{chapter}:{start}:{stop}"


def parse_translation_batch_key(value: object) -> tuple[int, int, int]:
    """Parse a canonical batch key without accepting aliases or leading zeros."""
    if type(value) is not str:
        raise ValueError("translation batch key must be a native string")
    match = _BATCH_KEY_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError(
            "translation batch key must be canonical ASCII chapter:start:stop integers"
        )
    coordinates = tuple(int(match.group(name)) for name in ("chapter", "start", "stop"))
    chapter, start, stop = cast(tuple[int, int, int], coordinates)
    if start >= stop:
        raise ValueError("translation batch key must satisfy start < stop")
    return chapter, start, stop


def validate_translation_batch_artifact(
    value: Mapping[str, object],
) -> TranslationBatchArtifact:
    """Validate and detach one translation-batch payload.

    Target positions use the enclosing chapter's ``text_segments`` sequence,
    not a segment's potentially sparse domain ``index`` field.
    """
    expected_keys = {
        "schema_version",
        "workflow_id",
        "document_sha256",
        "chapter_index",
        "start_index",
        "stop_index",
        "targets",
    }
    if set(value) != expected_keys:
        missing = sorted(expected_keys - set(value))
        extra = sorted(set(value) - expected_keys)
        raise ValueError(
            f"translation batch artifact fields do not match: missing={missing}, extra={extra}"
        )
    # ``bool`` subclasses ``int`` in Python, so equality alone would accept
    # ``True`` as schema version 1 and create a second non-canonical shape.
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != TRANSLATION_BATCH_ARTIFACT_SCHEMA_VERSION
    ):
        raise ValueError(
            "translation batch artifact supports only "
            f"schema_version={TRANSLATION_BATCH_ARTIFACT_SCHEMA_VERSION}"
        )

    workflow_id = value["workflow_id"]
    if type(workflow_id) is not str or _WORKFLOW_ID_PATTERN.fullmatch(workflow_id) is None:
        raise ValueError("translation batch workflow_id must be canonical wf-<sha256>")
    document_sha256 = validate_sha256(
        value["document_sha256"],
        field="translation batch document_sha256",
    )
    chapter = _require_non_negative_int(value["chapter_index"], field="chapter_index")
    start = _require_non_negative_int(value["start_index"], field="start_index")
    stop = _require_non_negative_int(value["stop_index"], field="stop_index")
    if start >= stop:
        raise ValueError("translation batch range must satisfy start_index < stop_index")

    # A checkpoint is publishable only when every covered source position has
    # a durable, non-empty UTF-8 target.  Copying each string detaches the
    # returned payload from the caller-owned list.
    targets_value = value["targets"]
    if isinstance(targets_value, (str, bytes)) or not isinstance(targets_value, Sequence):
        raise ValueError("translation batch targets must be a sequence of strings")
    targets = [
        _require_non_empty_utf8_text(target, field=f"targets[{index}]")
        for index, target in enumerate(targets_value)
    ]
    if not targets:
        raise ValueError("translation batch targets must not be empty")
    if len(targets) != stop - start:
        raise ValueError("translation batch targets count must equal stop_index - start_index")

    return {
        "schema_version": TRANSLATION_BATCH_ARTIFACT_SCHEMA_VERSION,
        "workflow_id": workflow_id,
        "document_sha256": document_sha256,
        "chapter_index": chapter,
        "start_index": start,
        "stop_index": stop,
        "targets": targets,
    }


def encode_translation_batch_artifact(value: Mapping[str, object]) -> bytes:
    """Return the sole canonical UTF-8 JSON representation of a valid payload."""
    normalized = validate_translation_batch_artifact(value)
    return _canonical_json_bytes(normalized)


def decode_translation_batch_artifact(value: object) -> TranslationBatchArtifact:
    """Decode canonical bytes and return a fully detached validated payload."""
    if type(value) is not bytes:
        raise ValueError("translation batch artifact must be immutable bytes")
    try:
        decoded = json.loads(value.decode("utf-8"))
        stable = copy_json_value(decoded, field="translation batch artifact")
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError("translation batch artifact must be stable UTF-8 JSON") from error
    if not isinstance(stable, dict):
        raise ValueError("translation batch artifact must be a JSON object")
    if _canonical_json_bytes(stable) != value:
        raise ValueError("translation batch artifact JSON must use canonical encoding")
    return validate_translation_batch_artifact(stable)


def _canonical_json_bytes(value: object) -> bytes:
    """Serialize stable JSON with deterministic key order and no whitespace."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _require_non_negative_int(value: object, *, field: str) -> int:
    """Reject booleans and negative coordinates before formatting an identity."""
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _require_non_empty_utf8_text(value: object, *, field: str) -> str:
    """Validate target text without trimming or otherwise changing its content."""
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field} must be a non-empty native string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{field} must be encodable as UTF-8") from error
    return value


__all__ = [
    "TRANSLATION_BATCH_ARTIFACT_MEDIA_TYPE",
    "TRANSLATION_BATCH_ARTIFACT_SCHEMA_VERSION",
    "TranslationBatchArtifact",
    "build_translation_batch_key",
    "decode_translation_batch_artifact",
    "encode_translation_batch_artifact",
    "parse_translation_batch_key",
    "validate_translation_batch_artifact",
]
