"""Map legacy ingest models into the source-only normalized document contract.

This adapter is intentionally one-way.  It selects source-derived fields from
``Document`` instead of serializing the whole legacy model, so runtime paths,
target-language configuration, and translated text cannot enter a durable
``NormalizedDocumentV1`` artifact.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from ..domain.normalized_document import (
    NORMALIZED_DOCUMENT_SCHEMA_VERSION,
    NormalizedDocumentV1,
    encode_normalized_document_v1,
    validate_normalized_document_v1,
)
from ..domain.source_format import validate_canonical_source_format
from ..domain.workflow import copy_json_value, normalize_language_code, validate_sha256
from .models import Document

_DOCUMENT_META_KEYS = {
    "text": frozenset(),
    "html": frozenset({"chapter_tags", "head_html"}),
    "pdf": frozenset({"chapter_tags", "head_html"}),
    "fb2": frozenset({"fb2_resources", "fb2_cover_image"}),
    "epub": frozenset(
        {
            "epub_schema",
            "opf_path",
            "toc_paths",
            "toc_entries",
            "epub_resources",
            "epub_split_strategy",
            "epub_split_toc_path",
            "epub_annotation_contexts",
        }
    ),
}
_CHAPTER_META_KEYS = {
    "text": frozenset({"heading_level"}),
    "html": frozenset(),
    "pdf": frozenset(),
    "fb2": frozenset({"fb2_images"}),
    "epub": frozenset({"epub_split_strategy", "toc_entry_id"}),
}
_SEGMENT_META_KEYS = {
    "text": frozenset(),
    "html": frozenset({"epub_inline", "epub_annotations"}),
    "pdf": frozenset({"epub_inline", "epub_annotations"}),
    "fb2": frozenset(),
    "epub": frozenset({"epub_annotations"}),
}

_INLINE_KEYS = frozenset({"version", "source_length", "nodes"})
_INLINE_NODE_KEYS = frozenset({"id", "tag", "placement", "offset"})
_ANNOTATION_KEYS = frozenset({"version", "source_length", "items"})
_ANNOTATION_ITEM_KEYS = frozenset(
    {
        "id",
        "mode",
        "source_start",
        "source_end",
        "source_text",
        "marker_text",
        "raw_href",
        "target_key",  # A source-document locator, not translated content.
        "relation",
    }
)
_TOC_KEYS = frozenset(
    {
        "entry_id",
        "toc_path",
        "node_index",
        "node_id",
        "parent_index",
        "depth",
        "kind",
        "title",
        "raw_href",
        "resource_href",
        "fragment",
        "target_key",
        "external",
        "segment_anchor",
        "boundary_position",
        "inherited_boundary_from",
    }
)
_ANNOTATION_CONTEXT_KEYS = frozenset(
    {"target_key", "resource_href", "fragment", "source_blocks", "segment_anchors"}
)
_FB2_RESOURCE_KEYS = frozenset({"id", "content_type"})
_FB2_IMAGE_KEYS = frozenset({"id", "position"})
_EPUB_RESOURCE_KEYS = frozenset({"index", "href"})


def normalized_document_v1_from_ingest(
    document: Document,
    *,
    source_sha256: str,
    source_format: str,
) -> NormalizedDocumentV1:
    """Build a detached V1 source artifact from one resolved ingest document."""
    source_hash = validate_sha256(source_sha256, field="source_sha256")
    canonical_format = validate_canonical_source_format(source_format)
    if document.fmt != canonical_format:
        raise ValueError("ingest document format does not match source_format")
    canonical_language = normalize_language_code(document.source_lang, field="source_lang")
    if document.source_lang != canonical_language:
        raise ValueError("ingest document source_lang must already be canonical")

    # Every nested object is rebuilt field-by-field.  In particular, neither
    # Document.source_path/target_lang nor Segment.target is read into payload.
    chapters: list[dict[str, Any]] = []
    for chapter_position, chapter in enumerate(document.chapters):
        if chapter.index != chapter_position:
            raise ValueError("chapter index must equal its list position")
        segments: list[dict[str, Any]] = []
        for segment_position, segment in enumerate(chapter.segments):
            if segment.index != segment_position:
                raise ValueError("segment index must equal its list position")
            if segment.target is not None:
                raise ValueError("translated segments cannot become source artifacts")
            segments.append(
                {
                    "index": segment.index,
                    "source": segment.source,
                    "kind": segment.kind,
                    "anchor": segment.anchor,
                    "resource_href": segment.resource_href,
                    "cont": segment.cont,
                    "meta": _copy_metadata(
                        segment.meta,
                        allowed=_SEGMENT_META_KEYS[canonical_format],
                        field="segment metadata",
                    ),
                }
            )
        chapters.append(
            {
                "index": chapter.index,
                "title": chapter.title,
                "segments": segments,
                "href": chapter.href,
                "template": chapter.template,
                "meta": _copy_metadata(
                    chapter.meta,
                    allowed=_CHAPTER_META_KEYS[canonical_format],
                    field="chapter metadata",
                ),
            }
        )

    document_meta = _copy_metadata(
        document.meta,
        allowed=_DOCUMENT_META_KEYS[canonical_format],
        field="document metadata",
    )
    if canonical_format in {"html", "pdf"}:
        document_meta["chapter_tags"] = _canonical_chapter_tags(document_meta.get("chapter_tags"))

    candidate = {
        "schema_version": NORMALIZED_DOCUMENT_SCHEMA_VERSION,
        "source_sha256": source_hash,
        "source_format": canonical_format,
        "source_lang": canonical_language,
        "title": document.title,
        "chapters": chapters,
        "meta": document_meta,
    }
    return validate_normalized_document_v1(candidate)


def encode_ingest_document_v1(
    document: Document,
    *,
    source_sha256: str,
    source_format: str,
) -> bytes:
    """Encode the one canonical V1 byte representation for an ingest document."""
    return encode_normalized_document_v1(
        normalized_document_v1_from_ingest(
            document,
            source_sha256=source_sha256,
            source_format=source_format,
        )
    )


def _copy_metadata(value: object, *, allowed: frozenset[str], field: str) -> dict[str, Any]:
    """Copy one provenance boundary and reject unowned extension fields."""
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise ValueError(f"{field} must be a JSON object")
    if not set(value) <= allowed:
        raise ValueError(f"{field} contains unsupported fields")
    try:
        copied = cast(dict[str, Any], copy_json_value(dict(value), field=field))
    except (TypeError, ValueError):
        raise ValueError(f"{field} must contain stable JSON values") from None
    _validate_nested_source_metadata(copied, field=field)
    return copied


def _validate_nested_source_metadata(value: Mapping[str, Any], *, field: str) -> None:
    """Validate metadata containers that may otherwise hide target-side fields."""
    if "epub_inline" in value:
        inline = _object_with_keys(value["epub_inline"], _INLINE_KEYS, field=field)
        _object_list(inline["nodes"], _INLINE_NODE_KEYS, field=field)
    if "epub_annotations" in value:
        annotations = _object_with_keys(value["epub_annotations"], _ANNOTATION_KEYS, field=field)
        _object_list(annotations["items"], _ANNOTATION_ITEM_KEYS, field=field)
    if "toc_entries" in value:
        _object_list(value["toc_entries"], _TOC_KEYS, field=field, exact=False)
    if "epub_annotation_contexts" in value:
        contexts = _object_with_keys(
            value["epub_annotation_contexts"],
            frozenset({"version", "contexts"}),
            field=field,
        )["contexts"]
        if not isinstance(contexts, Mapping):
            raise ValueError(f"{field} has invalid annotation contexts")
        for context in contexts.values():
            _object_with_keys(context, _ANNOTATION_CONTEXT_KEYS, field=field)
    # Resource indexes are producer-owned schemas too.  Validating only their
    # outer list name would leave a tunnel for plugin/runtime metadata.
    if "fb2_resources" in value:
        _object_list(value["fb2_resources"], _FB2_RESOURCE_KEYS, field=field)
    if "fb2_images" in value:
        _object_list(value["fb2_images"], _FB2_IMAGE_KEYS, field=field)
    if "epub_resources" in value:
        _object_list(value["epub_resources"], _EPUB_RESOURCE_KEYS, field=field)


def _object_with_keys(value: object, keys: frozenset[str], *, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError(f"{field} has an unsupported metadata shape")
    return cast(Mapping[str, Any], value)


def _object_list(
    value: object,
    keys: frozenset[str],
    *,
    field: str,
    exact: bool = True,
) -> None:
    if not isinstance(value, list):
        raise ValueError(f"{field} has an unsupported metadata shape")
    for item in value:
        if not isinstance(item, Mapping) or (set(item) != keys if exact else not set(item) <= keys):
            raise ValueError(f"{field} has an unsupported metadata shape")


def _canonical_chapter_tags(value: object) -> list[str] | None:
    """Turn the HTML reader policy set into deterministic durable ordering."""
    if value is None:
        return None
    if not isinstance(value, list) or any(type(tag) is not str or not tag for tag in value):
        raise ValueError("document metadata chapter_tags must be a string list or null")
    return sorted(set(value))


__all__ = ["encode_ingest_document_v1", "normalized_document_v1_from_ingest"]
