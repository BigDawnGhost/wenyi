"""NormalizedDocumentV1 的严格形状、确定性和隔离边界测试。"""

from __future__ import annotations

import hashlib
import inspect
import json
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from trans_novel.domain.normalized_document import (
    NORMALIZED_DOCUMENT_MEDIA_TYPE,
    NORMALIZED_DOCUMENT_SCHEMA_VERSION,
    NormalizedDocumentCounts,
    decode_normalized_document_v1,
    encode_normalized_document_v1,
    normalized_document_v1_counts,
    validate_normalized_document_v1,
)

SOURCE_HASH = "a" * 64


def _document() -> dict[str, Any]:
    """构造同时覆盖 EPUB 定位信息、续段和稳定 metadata 的代表性文档。"""
    return {
        "schema_version": 1,
        "source_sha256": SOURCE_HASH,
        "source_format": "epub",
        "source_lang": "ja",
        "title": "銀河鉄道の夜",
        "chapters": [
            {
                "index": 0,
                "title": "第一章",
                "segments": [
                    {
                        "index": 0,
                        "source": "第一章",
                        "kind": "heading",
                        "anchor": "heading-1",
                        "resource_href": "text/chapter.xhtml",
                        "cont": False,
                        "meta": {"toc": {"level": 1}},
                    },
                    {
                        "index": 1,
                        "source": "長い段落の前半",
                        "kind": "text",
                        "anchor": "paragraph-1",
                        "resource_href": "text/chapter.xhtml",
                        "cont": False,
                        "meta": {"annotations": ["ruby", {"offset": 2}]},
                    },
                    {
                        "index": 2,
                        "source": "長い段落の後半",
                        "kind": "text",
                        "anchor": None,
                        "resource_href": "text/chapter.xhtml",
                        "cont": True,
                        "meta": {},
                    },
                    {
                        "index": 3,
                        "source": "  ",
                        "kind": "text",
                        "anchor": None,
                        "resource_href": None,
                        "cont": False,
                        "meta": {},
                    },
                ],
                "href": "text/chapter.xhtml",
                "template": "<html><body>{{ content }}</body></html>",
                "meta": {"spine": 0},
            }
        ],
        "meta": {"resources": {"cover": "images/cover.jpg"}, "tags": ["novel"]},
    }


def test_round_trip_preserves_source_structure_and_exact_public_shape() -> None:
    document = _document()

    payload = encode_normalized_document_v1(document)
    decoded = decode_normalized_document_v1(payload)

    assert decoded == document
    assert set(decoded) == {
        "schema_version",
        "source_sha256",
        "source_format",
        "source_lang",
        "title",
        "chapters",
        "meta",
    }
    assert "source_path" not in payload.decode("utf-8")
    assert "target_lang" not in payload.decode("utf-8")
    assert '"target"' not in payload.decode("utf-8")
    assert NORMALIZED_DOCUMENT_SCHEMA_VERSION == 1
    assert NORMALIZED_DOCUMENT_MEDIA_TYPE == "application/vnd.wenyi.normalized-document.v1+json"


def test_canonical_encoding_is_stable_across_mapping_insertion_order() -> None:
    first = _document()
    second = deepcopy(first)
    second["meta"] = {"tags": ["novel"], "resources": {"cover": "images/cover.jpg"}}
    second["chapters"][0]["segments"][1]["meta"] = {"annotations": [{"offset": 2}, "ruby"]}
    first["chapters"][0]["segments"][1]["meta"] = {"annotations": [{"offset": 2}, "ruby"]}

    first_payload = encode_normalized_document_v1(first)
    second_payload = encode_normalized_document_v1(second)

    assert first_payload == second_payload
    assert hashlib.sha256(first_payload).digest() == hashlib.sha256(second_payload).digest()
    assert (
        encode_normalized_document_v1(decode_normalized_document_v1(first_payload)) == first_payload
    )


def test_canonical_encoding_has_a_versioned_golden_digest() -> None:
    minimal = {
        "schema_version": 1,
        "source_sha256": SOURCE_HASH,
        "source_format": "text",
        "source_lang": "ja",
        "title": "",
        "chapters": [],
        "meta": {},
    }
    expected = (
        b'{"chapters":[],"meta":{},"schema_version":1,"source_format":"text",'
        b'"source_lang":"ja","source_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
        b'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","title":""}'
    )

    assert encode_normalized_document_v1(minimal) == expected
    assert (
        hashlib.sha256(expected).hexdigest()
        == "5ca3d1b7aeadeabdd85f0b2822c37c3b366dbdcfa098d6ba6046aaeab92eb207"
    )


def test_validation_and_decoding_return_deeply_detached_values() -> None:
    original = _document()
    validated = validate_normalized_document_v1(original)
    encoded = encode_normalized_document_v1(original)
    first = decode_normalized_document_v1(encoded)
    second = decode_normalized_document_v1(encoded)

    original["meta"]["tags"].append("mutated")
    original["chapters"][0]["segments"][0]["meta"]["toc"]["level"] = 9
    first["meta"]["tags"].append("first-only")

    assert validated["meta"]["tags"] == ["novel"]
    assert validated["chapters"][0]["segments"][0]["meta"] == {"toc": {"level": 1}}
    assert second["meta"]["tags"] == ["novel"]
    assert decode_normalized_document_v1(encoded)["meta"]["tags"] == ["novel"]


@pytest.mark.parametrize("extra_field", ["source_path", "target_lang"])
def test_document_rejects_runtime_or_target_side_extension_fields(extra_field: str) -> None:
    document = _document()
    document[extra_field] = "TOP-SECRET-RUNTIME-VALUE"

    with pytest.raises(ValueError, match="exactly") as captured:
        validate_normalized_document_v1(document)

    assert "TOP-SECRET" not in str(captured.value)


def test_segment_rejects_translated_target_extension() -> None:
    document = _document()
    document["chapters"][0]["segments"][0]["target"] = "译文"

    with pytest.raises(ValueError, match="exactly"):
        validate_normalized_document_v1(document)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.pop("title"),
        lambda value: value["chapters"][0].pop("href"),
        lambda value: value["chapters"][0]["segments"][0].pop("anchor"),
        lambda value: value["chapters"][0].update({"unknown": None}),
        lambda value: value["chapters"][0]["segments"][0].update({"unknown": None}),
    ],
)
def test_every_schema_layer_rejects_missing_or_extra_fields(mutation: Any) -> None:
    document = _document()
    mutation(document)

    with pytest.raises(ValueError, match="exactly"):
        validate_normalized_document_v1(document)


@pytest.mark.parametrize("version", [True, 0, 2, -1])
def test_schema_version_is_an_exact_native_version(version: object) -> None:
    document = _document()
    document["schema_version"] = version

    with pytest.raises(ValueError, match="schema_version"):
        validate_normalized_document_v1(document)


@pytest.mark.parametrize("source_hash", ["A" * 64, "short", True])
def test_source_hash_must_be_canonical_sha256(source_hash: object) -> None:
    document = _document()
    document["source_sha256"] = source_hash

    with pytest.raises(ValueError, match="source_sha256"):
        validate_normalized_document_v1(document)


@pytest.mark.parametrize("source_format", ["txt", "markdown", "xhtml", "EPUB", ""])
def test_source_format_must_be_a_canonical_reader_family(source_format: object) -> None:
    document = _document()
    document["source_format"] = source_format

    with pytest.raises(ValueError, match="source_format"):
        validate_normalized_document_v1(document)


@pytest.mark.parametrize("source_lang", ["JA", "eng", "ja_JP", "auto", "mul", "unknown"])
def test_source_language_must_be_resolved_and_already_canonical(source_lang: object) -> None:
    document = _document()
    document["source_lang"] = source_lang

    with pytest.raises(ValueError, match="source_lang"):
        validate_normalized_document_v1(document)


@pytest.mark.parametrize("index", [True, -1, 1])
def test_chapter_index_is_the_exact_list_position(index: object) -> None:
    document = _document()
    document["chapters"][0]["index"] = index

    with pytest.raises(ValueError, match=r"chapters\[0\]\.index"):
        validate_normalized_document_v1(document)


@pytest.mark.parametrize("index", [True, -1, 0, 4])
def test_segment_index_is_the_exact_list_position(index: object) -> None:
    document = _document()
    document["chapters"][0]["segments"][1]["index"] = index

    with pytest.raises(ValueError, match=r"segments\[1\]\.index"):
        validate_normalized_document_v1(document)


def test_duplicate_anchor_is_rejected_across_chapters() -> None:
    document = _document()
    duplicate_chapter = deepcopy(document["chapters"][0])
    duplicate_chapter["index"] = 1
    duplicate_chapter["segments"] = [deepcopy(duplicate_chapter["segments"][0])]
    duplicate_chapter["segments"][0]["index"] = 0
    duplicate_chapter["segments"][0]["resource_href"] = "text/other.xhtml"
    document["chapters"].append(duplicate_chapter)

    with pytest.raises(ValueError, match="globally unique"):
        validate_normalized_document_v1(document)


@pytest.mark.parametrize("anchor", ["", " ", "\n\t"])
def test_anchor_uses_one_canonical_absence_representation(anchor: str) -> None:
    document = _document()
    document["chapters"][0]["segments"][0]["anchor"] = anchor

    with pytest.raises(ValueError, match="nonblank"):
        validate_normalized_document_v1(document)


@pytest.mark.parametrize(
    ("segment_index", "overrides"),
    [
        (0, {"cont": True, "anchor": None, "kind": "text"}),
        (1, {"cont": True, "anchor": None, "kind": "heading"}),
        (1, {"cont": True, "anchor": "continuation-anchor", "kind": "text"}),
        (1, {"cont": 1}),
        (1, {"kind": "unknown"}),
    ],
)
def test_segment_kind_and_continuation_rules_are_explicit(
    segment_index: int,
    overrides: dict[str, object],
) -> None:
    document = _document()
    document["chapters"][0]["segments"][segment_index].update(overrides)

    with pytest.raises(ValueError):
        validate_normalized_document_v1(document)


@pytest.mark.parametrize("blank_side", ["current", "previous"])
def test_continuation_requires_adjacent_nonblank_source(blank_side: str) -> None:
    document = _document()
    previous = document["chapters"][0]["segments"][0]
    current = document["chapters"][0]["segments"][1]
    current.update({"cont": True, "anchor": None, "kind": "text"})
    (current if blank_side == "current" else previous)["source"] = "  "

    with pytest.raises(ValueError, match="adjacent nonblank"):
        validate_normalized_document_v1(document)


def test_continuation_cannot_cross_a_physical_resource() -> None:
    document = _document()
    current = document["chapters"][0]["segments"][1]
    current.update(
        {
            "cont": True,
            "anchor": None,
            "kind": "text",
            "resource_href": "text/other.xhtml",
        }
    )

    with pytest.raises(ValueError, match="same resource"):
        validate_normalized_document_v1(document)


def test_long_heading_may_continue_as_an_unanchored_text_chunk() -> None:
    document = _document()
    continuation = document["chapters"][0]["segments"][1]
    continuation.update({"cont": True, "anchor": None, "kind": "text"})

    validated = validate_normalized_document_v1(document)

    assert validated["chapters"][0]["segments"][0]["kind"] == "heading"
    assert validated["chapters"][0]["segments"][1]["cont"] is True


def test_multiple_continuation_chunks_form_one_valid_adjacent_chain() -> None:
    document = _document()
    first_continuation = document["chapters"][0]["segments"][1]
    second_continuation = document["chapters"][0]["segments"][2]
    first_continuation.update({"cont": True, "anchor": None, "kind": "text"})
    second_continuation.update({"cont": True, "anchor": None, "kind": "text"})

    validated = validate_normalized_document_v1(document)

    assert [segment["cont"] for segment in validated["chapters"][0]["segments"][:3]] == [
        False,
        True,
        True,
    ]


@pytest.mark.parametrize(
    "invalid_meta",
    [
        {"value": (1, 2)},
        {"value": b"bytes"},
        {1: "integer key"},
        {"value": float("nan")},
        {"value": float("inf")},
        {"value": chr(0xD800)},
        {chr(0xD800): "value"},
        {"value": Path("runtime/path")},
    ],
)
def test_metadata_accepts_only_stable_utf8_json(invalid_meta: object) -> None:
    document = _document()
    document["meta"] = invalid_meta

    with pytest.raises(ValueError, match="stable JSON"):
        validate_normalized_document_v1(document)


def test_validation_errors_do_not_echo_source_text_or_metadata_keys() -> None:
    document = _document()
    document["chapters"][0]["segments"][0]["source"] = "TOP-SECRET-BODY"
    document["meta"] = {"TOP-SECRET-KEY": object()}

    with pytest.raises(ValueError) as captured:
        validate_normalized_document_v1(document)

    assert "TOP-SECRET" not in str(captured.value)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"chapters":[],"chapters":[]}',
        b'{"outer":{"key":1,"key":2}}',
        b'{"value":NaN}',
        b"\xff",
        b"[]",
    ],
)
def test_decoder_rejects_duplicate_keys_nonstandard_constants_and_invalid_roots(
    payload: bytes,
) -> None:
    with pytest.raises(ValueError):
        decode_normalized_document_v1(payload)


def test_decoder_rejects_valid_but_noncanonical_json_bytes() -> None:
    document = _document()
    pretty_payload = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
    escaped_payload = json.dumps(
        document,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    with pytest.raises(ValueError, match="canonical"):
        decode_normalized_document_v1(pretty_payload)
    with pytest.raises(ValueError, match="canonical"):
        decode_normalized_document_v1(escaped_payload)


def test_decoder_accepts_only_native_bytes() -> None:
    payload = encode_normalized_document_v1(_document())

    with pytest.raises(ValueError, match="native bytes"):
        decode_normalized_document_v1(bytearray(payload))  # type: ignore[arg-type]


def test_counts_follow_nonblank_translation_work_units() -> None:
    document = _document()

    assert normalized_document_v1_counts(document) == NormalizedDocumentCounts(1, 3)

    empty = _document()
    empty["chapters"] = []
    assert normalized_document_v1_counts(empty) == NormalizedDocumentCounts(0, 0)

    media_only = _document()
    media_only["chapters"][0]["segments"] = []
    assert normalized_document_v1_counts(media_only) == NormalizedDocumentCounts(1, 0)


def test_clean_import_keeps_codec_free_of_runtime_and_ingest_dependencies() -> None:
    # A separate interpreter proves the result does not depend on this test
    # process already having imported Pydantic or application modules.
    script = """
import importlib
import sys

module = importlib.import_module("trans_novel.domain.normalized_document")
forbidden = (
    "langgraph",
    "pydantic",
    "trans_novel.cli",
    "trans_novel.ingest",
    "trans_novel.llm",
    "trans_novel.pipeline",
    "trans_novel.services",
    "trans_novel.storage",
    "trans_novel.workflow",
)
loaded = sorted(name for name in sys.modules if name.startswith(forbidden))
assert not loaded, loaded
assert module.__all__
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_public_codec_symbols_have_documented_stable_roles() -> None:
    import trans_novel.domain as domain
    import trans_novel.domain.normalized_document as module

    callable_names = [
        "NormalizedChapterV1",
        "NormalizedDocumentCounts",
        "NormalizedDocumentV1",
        "NormalizedSegmentV1",
        "decode_normalized_document_v1",
        "encode_normalized_document_v1",
        "normalized_document_v1_counts",
        "validate_normalized_document_v1",
    ]

    assert module.__all__ == [
        "NORMALIZED_DOCUMENT_MEDIA_TYPE",
        "NORMALIZED_DOCUMENT_SCHEMA_VERSION",
        *callable_names,
    ]
    assert all(inspect.getdoc(getattr(module, name)) for name in callable_names)
    assert all(getattr(domain, name) is getattr(module, name) for name in module.__all__)
    assert set(module.__all__) <= set(domain.__all__)
