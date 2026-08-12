"""Versioned, deterministic source-document artifacts.

``NormalizedDocumentV1`` is the durable boundary between ingestion and the
workflow.  Its exact structural fields record only source-side data.  Opaque
metadata must also be source-derived, but excluding host paths and runtime data
from that metadata is the producer adapter's responsibility.  This module is
framework-neutral and depends only on stable domain validators.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, TypedDict, cast

from .source_format import validate_canonical_source_format
from .workflow import copy_json_value, normalize_language_code, validate_sha256

# These constants version both the JSON shape and the media type stored in the
# content-addressed artifact store.  A shape change requires a new version.
NORMALIZED_DOCUMENT_SCHEMA_VERSION = 1
NORMALIZED_DOCUMENT_MEDIA_TYPE = "application/vnd.wenyi.normalized-document.v1+json"

_DOCUMENT_KEYS = frozenset(
    {
        "schema_version",
        "source_sha256",
        "source_format",
        "source_lang",
        "title",
        "chapters",
        "meta",
    }
)
_CHAPTER_KEYS = frozenset({"index", "title", "segments", "href", "template", "meta"})
_SEGMENT_KEYS = frozenset({"index", "source", "kind", "anchor", "resource_href", "cont", "meta"})
_SEGMENT_KINDS = frozenset({"heading", "text"})


class NormalizedSegmentV1(TypedDict):
    """One source segment with stable, format-relative reconstruction data."""

    index: int
    source: str
    kind: str
    anchor: str | None
    resource_href: str | None
    cont: bool
    meta: dict[str, Any]


class NormalizedChapterV1(TypedDict):
    """One ordered chapter in a normalized source document."""

    index: int
    title: str
    segments: list[NormalizedSegmentV1]
    href: str | None
    template: str | None
    meta: dict[str, Any]


class NormalizedDocumentV1(TypedDict):
    """The exact version-one JSON shape for an immutable source document."""

    schema_version: int
    source_sha256: str
    source_format: str
    source_lang: str
    title: str
    chapters: list[NormalizedChapterV1]
    meta: dict[str, Any]


@dataclass(frozen=True, slots=True)
class NormalizedDocumentCounts:
    """Workflow counters derived from a validated normalized document."""

    chapter_count: int
    source_segment_count: int


class _JsonPayloadError(ValueError):
    """Internal marker for malformed JSON that must receive a safe public error."""


def validate_normalized_document_v1(
    value: Mapping[str, object],
) -> NormalizedDocumentV1:
    """Validate the exact V1 shape and return a deeply detached JSON value."""
    document = _require_mapping(value, field="document")
    _require_exact_keys(document, _DOCUMENT_KEYS, field="document")

    # Identity fields are stored in canonical form so a logical document has
    # one byte representation across machines and workflow invocations.
    schema_version = _require_native_int(document["schema_version"], field="schema_version")
    if schema_version != NORMALIZED_DOCUMENT_SCHEMA_VERSION:
        raise ValueError("schema_version must be the supported normalized document version")
    source_sha256 = validate_sha256(document["source_sha256"], field="source_sha256")
    source_format = _validate_source_format(document["source_format"])
    source_lang = _validate_canonical_language(document["source_lang"])
    title = _require_utf8_string(document["title"], field="title")
    meta = _copy_stable_mapping(document["meta"], field="meta")

    # Chapters and segments are addresses, not sortable display records.  Their
    # stored indexes therefore have to equal their positions exactly.
    chapters_value = _require_list(document["chapters"], field="chapters")
    anchors: set[str] = set()
    chapters = [
        _validate_chapter(chapter, position=position, anchors=anchors)
        for position, chapter in enumerate(chapters_value)
    ]

    return {
        "schema_version": schema_version,
        "source_sha256": source_sha256,
        "source_format": source_format,
        "source_lang": source_lang,
        "title": title,
        "chapters": chapters,
        "meta": meta,
    }


def encode_normalized_document_v1(value: Mapping[str, object]) -> bytes:
    """Encode a validated V1 document as canonical UTF-8 JSON bytes."""
    validated = validate_normalized_document_v1(value)
    return _encode_validated_document(validated)


def decode_normalized_document_v1(payload: bytes) -> NormalizedDocumentV1:
    """Decode canonical V1 bytes, rejecting duplicate keys and alternate encodings."""
    if type(payload) is not bytes:
        raise ValueError("normalized document payload must be native bytes")

    # Parsing uses an object hook so duplicate keys cannot disappear before the
    # exact-shape validator sees them.  Parse errors never echo source content.
    try:
        text = payload.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_mapping_without_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _JsonPayloadError):
        raise ValueError("normalized document payload must be valid strict UTF-8 JSON") from None

    if not isinstance(value, Mapping):
        raise ValueError("normalized document payload root must be a JSON object")
    validated = validate_normalized_document_v1(cast(Mapping[str, object], value))

    # Canonical-byte comparison rejects pretty printing, escaped Unicode, key
    # reordering, and other encodings that would create a second artifact hash.
    if _encode_validated_document(validated) != payload:
        raise ValueError("normalized document payload must use canonical UTF-8 JSON encoding")
    return validated


def normalized_document_v1_counts(
    value: Mapping[str, object],
) -> NormalizedDocumentCounts:
    """Derive chapter and nonblank-source counts from a validated V1 document."""
    document = validate_normalized_document_v1(value)
    source_segment_count = sum(
        1
        for chapter in document["chapters"]
        for segment in chapter["segments"]
        if segment["source"].strip()
    )
    return NormalizedDocumentCounts(
        chapter_count=len(document["chapters"]),
        source_segment_count=source_segment_count,
    )


def _validate_chapter(
    value: object,
    *,
    position: int,
    anchors: set[str],
) -> NormalizedChapterV1:
    """Validate one chapter while sharing the document-wide anchor registry."""
    field = f"chapters[{position}]"
    chapter = _require_mapping(value, field=field)
    _require_exact_keys(chapter, _CHAPTER_KEYS, field=field)
    index = _require_native_int(chapter["index"], field=f"{field}.index")
    if index != position:
        raise ValueError(f"{field}.index must equal its zero-based list position")

    segments_value = _require_list(chapter["segments"], field=f"{field}.segments")
    segments: list[NormalizedSegmentV1] = []
    for segment_position, segment in enumerate(segments_value):
        segments.append(
            _validate_segment(
                segment,
                chapter_position=position,
                position=segment_position,
                previous=segments[-1] if segments else None,
                anchors=anchors,
            )
        )
    return {
        "index": index,
        "title": _require_utf8_string(chapter["title"], field=f"{field}.title"),
        "segments": segments,
        "href": _require_optional_utf8_string(chapter["href"], field=f"{field}.href"),
        "template": _require_optional_utf8_string(chapter["template"], field=f"{field}.template"),
        "meta": _copy_stable_mapping(chapter["meta"], field=f"{field}.meta"),
    }


def _validate_segment(
    value: object,
    *,
    chapter_position: int,
    position: int,
    previous: NormalizedSegmentV1 | None,
    anchors: set[str],
) -> NormalizedSegmentV1:
    """Validate one segment and enforce global anchor and continuation rules."""
    field = f"chapters[{chapter_position}].segments[{position}]"
    segment = _require_mapping(value, field=field)
    _require_exact_keys(segment, _SEGMENT_KEYS, field=field)
    index = _require_native_int(segment["index"], field=f"{field}.index")
    if index != position:
        raise ValueError(f"{field}.index must equal its zero-based list position")

    kind = _require_utf8_string(segment["kind"], field=f"{field}.kind")
    if kind not in _SEGMENT_KINDS:
        raise ValueError(f"{field}.kind must be text or heading")
    anchor = _require_optional_utf8_string(segment["anchor"], field=f"{field}.anchor")
    resource_href = _require_optional_utf8_string(
        segment["resource_href"], field=f"{field}.resource_href"
    )
    source = _require_utf8_string(segment["source"], field=f"{field}.source")
    cont = segment["cont"]
    if type(cont) is not bool:
        raise ValueError(f"{field}.cont must be a native boolean")

    # ``anchor`` is Wenyi's generated reconstruction ID, not an original HTML
    # fragment ID; original resource-scoped identifiers remain in metadata.
    if anchor is not None:
        if anchor in anchors:
            raise ValueError("normalized document anchors must be globally unique")
        anchors.add(anchor)

    # A continuation must be adjacent to the source chunk it extends.  Heading
    # predecessors remain valid because splitting a long heading keeps the
    # first chunk as ``heading`` and marks later chunks as ``text``.
    if cont and (
        previous is None
        or kind != "text"
        or anchor is not None
        or not source.strip()
        or not previous["source"].strip()
        or previous["resource_href"] != resource_href
    ):
        raise ValueError(
            f"{field}.cont must extend an adjacent nonblank segment in the same resource"
        )

    return {
        "index": index,
        "source": source,
        "kind": kind,
        "anchor": anchor,
        "resource_href": resource_href,
        "cont": cont,
        "meta": _copy_stable_mapping(segment["meta"], field=f"{field}.meta"),
    }


def _require_mapping(value: object, *, field: str) -> Mapping[str, object]:
    """Accept mapping inputs while rejecting scalar and sequence lookalikes."""
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a JSON object")
    return cast(Mapping[str, object], value)


def _require_exact_keys(
    value: Mapping[str, object],
    expected: frozenset[str],
    *,
    field: str,
) -> None:
    """Reject both missing and extension fields without echoing their contents."""
    if any(type(key) is not str for key in value) or set(value) != expected:
        raise ValueError(f"{field} must contain exactly the V1 fields")


def _require_list(value: object, *, field: str) -> list[object]:
    """Require a native JSON array and return it for ordered validation."""
    if type(value) is not list:
        raise ValueError(f"{field} must be a native JSON array")
    return cast(list[object], value)


def _require_native_int(value: object, *, field: str) -> int:
    """Reject booleans and integer subclasses at durable numeric boundaries."""
    if type(value) is not int or value < 0:
        raise ValueError(f"{field} must be a nonnegative native integer")
    return value


def _require_utf8_string(value: object, *, field: str) -> str:
    """Return a native string, including empty strings, when UTF-8 encodable."""
    if type(value) is not str:
        raise ValueError(f"{field} must be a native string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{field} must be valid UTF-8 text") from None
    return value


def _require_optional_utf8_string(value: object, *, field: str) -> str | None:
    """Validate one nullable, nonblank structural string."""
    if value is None:
        return None
    string = _require_utf8_string(value, field=field)
    if not string.strip():
        raise ValueError(f"{field} must be null or a nonblank string")
    return string


def _copy_stable_mapping(value: object, *, field: str) -> dict[str, Any]:
    """Copy metadata while replacing potentially sensitive nested errors."""
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be a stable JSON object")
    try:
        copied = copy_json_value(dict(value), field=field)
    except ValueError:
        raise ValueError(f"{field} must contain only stable JSON values") from None
    return cast(dict[str, Any], copied)


def _validate_source_format(value: object) -> str:
    """Require an already-canonical reader-family name."""
    try:
        source_format = validate_canonical_source_format(value)
    except ValueError:
        raise ValueError("source_format must be a supported canonical reader family")
    return source_format


def _validate_canonical_language(value: object) -> str:
    """Require a resolved language identity without silently rewriting the artifact."""
    try:
        normalized = normalize_language_code(value, field="source_lang")
    except ValueError:
        raise ValueError("source_lang must be a resolved canonical language code") from None
    if value != normalized:
        raise ValueError("source_lang must already use its canonical language code")
    return normalized


def _mapping_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a parsed JSON object while rejecting duplicate member names."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _JsonPayloadError("duplicate key")
        result[key] = value
    return result


def _reject_json_constant(_: str) -> None:
    """Reject the nonstandard NaN and Infinity tokens accepted by ``json``."""
    raise _JsonPayloadError("non-finite constant")


def _encode_validated_document(value: NormalizedDocumentV1) -> bytes:
    """Produce the single canonical byte representation for a validated value."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


__all__ = [
    "NORMALIZED_DOCUMENT_MEDIA_TYPE",
    "NORMALIZED_DOCUMENT_SCHEMA_VERSION",
    "NormalizedChapterV1",
    "NormalizedDocumentCounts",
    "NormalizedDocumentV1",
    "NormalizedSegmentV1",
    "decode_normalized_document_v1",
    "encode_normalized_document_v1",
    "normalized_document_v1_counts",
    "validate_normalized_document_v1",
]
