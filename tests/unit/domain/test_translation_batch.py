"""Translation-batch artifact identity and canonical codec tests."""

from __future__ import annotations

import json

import pytest

from trans_novel.domain.translation_batch import (
    TRANSLATION_BATCH_ARTIFACT_SCHEMA_VERSION,
    build_translation_batch_key,
    decode_translation_batch_artifact,
    encode_translation_batch_artifact,
    parse_translation_batch_key,
    validate_translation_batch_artifact,
)


def _payload() -> dict[str, object]:
    """Build one valid detached batch covering two text-segment positions."""
    return {
        "schema_version": TRANSLATION_BATCH_ARTIFACT_SCHEMA_VERSION,
        "workflow_id": "wf-" + "a" * 64,
        "document_sha256": "b" * 64,
        "chapter_index": 2,
        "start_index": 3,
        "stop_index": 5,
        "targets": ["译文甲", "译文乙"],
    }


def test_batch_key_is_canonical_ascii_and_round_trips() -> None:
    """Coordinates have one spelling, preventing duplicate checkpoint identities."""
    key = build_translation_batch_key(12, 0, 9)

    assert key == "12:0:9"
    assert parse_translation_batch_key(key) == (12, 0, 9)


@pytest.mark.parametrize(
    "value",
    ["01:0:1", "1:00:1", "1:0:01", "１:0:1", "1:-1:2", "1:2:2", "1:3:2"],
)
def test_batch_key_rejects_aliases_and_empty_ranges(value: str) -> None:
    """No leading-zero, Unicode-digit, negative, or non-increasing key is accepted."""
    with pytest.raises(ValueError):
        parse_translation_batch_key(value)


def test_codec_returns_canonical_utf8_and_detaches_targets() -> None:
    """The encoded bytes have stable content identity and decoded lists are independent."""
    payload = _payload()
    encoded = encode_translation_batch_artifact(payload)
    decoded = decode_translation_batch_artifact(encoded)

    assert encoded == json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    assert decoded == payload
    decoded["targets"].append("后来修改")
    assert payload["targets"] == ["译文甲", "译文乙"]


def test_codec_rejects_noncanonical_or_non_utf8_bytes() -> None:
    """Artifact identity cannot depend on whitespace, key order, or replacement decoding."""
    with pytest.raises(ValueError, match="canonical"):
        decode_translation_batch_artifact(
            json.dumps(_payload(), ensure_ascii=False).encode("utf-8")
        )
    with pytest.raises(ValueError, match="UTF-8"):
        decode_translation_batch_artifact(b"\xff")


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"schema_version": 2}, "schema_version"),
        ({"workflow_id": "wf-not-a-digest"}, "workflow_id"),
        ({"document_sha256": "A" * 64}, "document_sha256"),
        ({"chapter_index": True}, "chapter_index"),
        ({"start_index": 5}, "start_index < stop_index"),
        ({"targets": []}, "must not be empty"),
        ({"targets": ["only one"]}, "count"),
        ({"targets": ["译文甲", "  "]}, r"targets\[1\]"),
    ],
)
def test_payload_rejects_ambiguous_or_incomplete_checkpoints(
    mutation: dict[str, object],
    message: str,
) -> None:
    """Only complete, exactly addressed translated ranges are publishable."""
    payload = {**_payload(), **mutation}

    with pytest.raises(ValueError, match=message):
        validate_translation_batch_artifact(payload)


def test_payload_rejects_unknown_fields() -> None:
    """Artifact schema changes must be explicit rather than silently ignored."""
    payload = {**_payload(), "future": True}

    with pytest.raises(ValueError, match="extra"):
        validate_translation_batch_artifact(payload)
