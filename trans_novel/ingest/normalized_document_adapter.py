"""Map legacy ingest models into the source-only normalized document contract.

This adapter is intentionally one-way and must receive a freshly parsed
``Document`` from a verified source artifact.  Its strict schemas reject
cross-format and target-side fields, but cannot prove the provenance of
arbitrary caller-constructed strings.  Selecting source-derived fields instead
of serializing the whole legacy model keeps runtime paths, target-language
configuration, and translated text out of a durable artifact.
"""

from __future__ import annotations

import posixpath
from collections.abc import Mapping
from typing import Any, cast
from urllib.parse import urlsplit

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
_REQUIRED_DOCUMENT_META_KEYS = {
    "text": frozenset(),
    "html": _DOCUMENT_META_KEYS["html"],
    "pdf": _DOCUMENT_META_KEYS["pdf"],
    "fb2": frozenset(),
    "epub": _DOCUMENT_META_KEYS["epub"],
}
_CHAPTER_META_KEYS = {
    "text": frozenset({"heading_level"}),
    "html": frozenset(),
    "pdf": frozenset(),
    "fb2": frozenset({"fb2_images"}),
    "epub": frozenset({"epub_split_strategy", "toc_entry_id"}),
}
_REQUIRED_CHAPTER_META_KEYS = {
    "text": frozenset({"heading_level"}),
    "html": frozenset(),
    "pdf": frozenset(),
    "fb2": frozenset(),
    "epub": frozenset({"epub_split_strategy"}),
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
_TOC_REQUIRED_KEYS = frozenset(
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
    }
)


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
        _validate_chapter_structure(chapter, source_format=canonical_format)
        segments: list[dict[str, Any]] = []
        for segment_position, segment in enumerate(chapter.segments):
            if segment.index != segment_position:
                raise ValueError("segment index must equal its list position")
            if segment.target is not None:
                raise ValueError("translated segments cannot become source artifacts")
            _validate_segment_structure(segment, source_format=canonical_format)
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
                        required=frozenset(),
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
                    required=_REQUIRED_CHAPTER_META_KEYS[canonical_format],
                    field="chapter metadata",
                ),
            }
        )

    document_meta = _copy_metadata(
        document.meta,
        allowed=_DOCUMENT_META_KEYS[canonical_format],
        required=_REQUIRED_DOCUMENT_META_KEYS[canonical_format],
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


def _copy_metadata(
    value: object,
    *,
    allowed: frozenset[str],
    required: frozenset[str],
    field: str,
) -> dict[str, Any]:
    """Copy one provenance boundary and reject unowned extension fields."""
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise ValueError(f"{field} must be a JSON object")
    actual_keys = set(value)
    if not required <= actual_keys or not actual_keys <= allowed:
        raise ValueError(f"{field} contains unsupported fields")
    try:
        copied = cast(dict[str, Any], copy_json_value(dict(value), field=field))
    except (TypeError, ValueError):
        raise ValueError(f"{field} must contain stable JSON values") from None
    _validate_source_metadata_values(copied, field=field)
    return copied


def _validate_source_metadata_values(value: Mapping[str, Any], *, field: str) -> None:
    """Validate metadata containers that may otherwise hide target-side fields."""
    if "heading_level" in value:
        _native_int(value["heading_level"], field=field, minimum=1, maximum=3)
    if "head_html" in value:
        _native_string(value["head_html"], field=field)
    if "epub_schema" in value and (
        type(value["epub_schema"]) is not int or value["epub_schema"] != 5
    ):
        raise ValueError(f"{field} has an unsupported EPUB schema")
    if "opf_path" in value:
        _package_path(value["opf_path"], field=field)
    if "fb2_cover_image" in value:
        _native_string(value["fb2_cover_image"], field=field, nonblank=True)
    if "epub_split_toc_path" in value:
        split_toc_path = _native_string(value["epub_split_toc_path"], field=field)
        if split_toc_path:
            _package_path(split_toc_path, field=field)
    for name in ("epub_split_strategy", "toc_entry_id"):
        if name in value:
            _native_string(value[name], field=field, nonblank=True)
    if "epub_split_strategy" in value and value["epub_split_strategy"] not in {
        "spine-fallback",
        "top-level-toc",
    }:
        raise ValueError(f"{field} has an unsupported EPUB split strategy")
    if "toc_paths" in value:
        for path in _string_list(value["toc_paths"], field=field, nonblank=True):
            _package_path(path, field=field)
    if "epub_inline" in value:
        inline = _object_with_keys(value["epub_inline"], _INLINE_KEYS, field=field)
        _validate_inline_metadata(inline, field=field)
    if "epub_annotations" in value:
        annotations = _object_with_keys(value["epub_annotations"], _ANNOTATION_KEYS, field=field)
        _validate_annotation_metadata(annotations, field=field)
    if "toc_entries" in value:
        _validate_toc_entries(value["toc_entries"], field=field)
    if "epub_annotation_contexts" in value:
        _validate_annotation_contexts(value["epub_annotation_contexts"], field=field)
    # Resource indexes are producer-owned schemas too.  Validating only their
    # outer list name would leave a tunnel for plugin/runtime metadata.
    if "fb2_resources" in value:
        for resource in _object_list(value["fb2_resources"], _FB2_RESOURCE_KEYS, field=field):
            _native_string(resource["id"], field=field, nonblank=True)
            _native_string(resource["content_type"], field=field)
    if "fb2_images" in value:
        for image in _object_list(value["fb2_images"], _FB2_IMAGE_KEYS, field=field):
            _native_string(image["id"], field=field, nonblank=True)
            _native_int(image["position"], field=field)
    if "epub_resources" in value:
        for resource in _object_list(value["epub_resources"], _EPUB_RESOURCE_KEYS, field=field):
            _native_int(resource["index"], field=field)
            _package_path(resource["href"], field=field)


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
) -> list[Mapping[str, Any]]:
    if type(value) is not list:
        raise ValueError(f"{field} has an unsupported metadata shape")
    result: list[Mapping[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping) or (set(item) != keys if exact else not set(item) <= keys):
            raise ValueError(f"{field} has an unsupported metadata shape")
        result.append(cast(Mapping[str, Any], item))
    return result


def _validate_chapter_structure(chapter: object, *, source_format: str) -> None:
    """Bind durable reconstruction fields to the reader family that owns them."""
    href = getattr(chapter, "href")
    template = getattr(chapter, "template")
    if source_format in {"text", "fb2"}:
        if href is not None or template is not None:
            raise ValueError(f"{source_format} chapters cannot contain href or template")
        return
    if source_format in {"html", "pdf"}:
        if href is not None or type(template) is not str:
            raise ValueError(f"{source_format} chapters require a source template and no href")
        return
    if template is not None:
        raise ValueError("epub chapters require a nonblank href and no template")
    _package_path(href, field="epub chapter href")


def _validate_segment_structure(segment: object, *, source_format: str) -> None:
    """Prevent resource locators from crossing reader-family boundaries."""
    resource_href = getattr(segment, "resource_href")
    anchor = getattr(segment, "anchor")
    continuation = getattr(segment, "cont")
    if source_format == "epub":
        _package_path(resource_href, field="epub segment resource_href")
    elif resource_href is not None:
        raise ValueError(f"{source_format} segments cannot contain resource_href")
    if source_format == "text":
        if anchor is not None:
            raise ValueError("text segments cannot contain reconstruction anchors")
    elif continuation:
        if anchor is not None:
            raise ValueError("continuation segments cannot contain reconstruction anchors")
    else:
        _native_string(anchor, field=f"{source_format} segment anchor", nonblank=True)


def _validate_inline_metadata(value: Mapping[str, Any], *, field: str) -> None:
    """Validate the reader-owned placement schema and its offset bounds."""
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError(f"{field} has an unsupported inline metadata version")
    source_length = _native_int(value["source_length"], field=field, minimum=1)
    nodes = _object_list(value["nodes"], _INLINE_NODE_KEYS, field=field)
    if not nodes:
        raise ValueError(f"{field} inline metadata must contain at least one node")
    for node in nodes:
        _native_string(node["id"], field=field, nonblank=True)
        _native_string(node["tag"], field=field, nonblank=True)
        _native_choice(
            node["placement"],
            choices=frozenset({"before", "inline", "after"}),
            field=field,
        )
        _native_int(node["offset"], field=field, maximum=source_length)


def _validate_annotation_metadata(value: Mapping[str, Any], *, field: str) -> None:
    """Validate source annotation spans without binding them to split segment text."""
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError(f"{field} has an unsupported annotation metadata version")
    source_length = _native_int(value["source_length"], field=field, minimum=1)
    items = _object_list(value["items"], _ANNOTATION_ITEM_KEYS, field=field)
    if not items:
        raise ValueError(f"{field} annotation metadata must contain at least one item")
    for item in items:
        _native_string(item["id"], field=field, nonblank=True)
        _native_choice(
            item["mode"],
            choices=frozenset({"point", "range"}),
            field=field,
        )
        start = _native_int(item["source_start"], field=field, maximum=source_length)
        end = _native_int(item["source_end"], field=field, maximum=source_length)
        if start > end:
            raise ValueError(f"{field} has a reversed annotation range")
        for name in ("source_text", "marker_text", "raw_href", "target_key"):
            _native_string(item[name], field=field)
        if len(item["source_text"]) != end - start:
            raise ValueError(f"{field} annotation source_text disagrees with its range")
        _native_choice(
            item["relation"],
            choices=frozenset({"noteref", "backlink", "internal_link"}),
            field=field,
        )


def _validate_toc_entries(value: object, *, field: str) -> None:
    """Validate parser-required TOC fields while allowing three reader-derived fields."""
    entries = _object_list(value, _TOC_KEYS, field=field, exact=False)
    for entry in entries:
        if not _TOC_REQUIRED_KEYS <= set(entry):
            raise ValueError(f"{field} has an incomplete toc entry")
        _native_string(entry["entry_id"], field=field, nonblank=True)
        _package_path(entry["toc_path"], field=field)
        _native_int(entry["node_index"], field=field)
        _native_int(entry["depth"], field=field)
        parent_index = entry["parent_index"]
        if parent_index is not None:
            _native_int(parent_index, field=field)
        for name in (
            "node_id",
            "title",
            "raw_href",
            "fragment",
            "target_key",
        ):
            _native_string(entry[name], field=field)
        resource_href = entry["resource_href"]
        if resource_href != "":
            _package_path(resource_href, field=field)
        _native_choice(entry["kind"], choices=frozenset({"nav", "ncx"}), field=field)
        if type(entry["external"]) is not bool:
            raise ValueError(f"{field} has invalid toc routing fields")
        # Internal TOC routing is parser-derived and therefore must agree with
        # its normalized resource/fragment pair.  External links intentionally
        # retain an empty target key and are outside this package-local check.
        if not entry["external"] and entry["target_key"] != _source_target_key(
            resource_href,
            entry["fragment"],
        ):
            raise ValueError(f"{field} toc target_key disagrees with its source locator")
        for name in ("segment_anchor", "inherited_boundary_from"):
            if name in entry:
                _native_string(entry[name], field=field, nonblank=True)
        if "boundary_position" in entry:
            _native_int(entry["boundary_position"], field=field)


def _validate_annotation_contexts(value: object, *, field: str) -> None:
    """Validate context-map ownership and bind every mapping key to its payload."""
    container = _object_with_keys(
        value,
        frozenset({"version", "contexts"}),
        field=field,
    )
    if type(container["version"]) is not int or container["version"] != 1:
        raise ValueError(f"{field} has an unsupported annotation-context version")
    contexts = container["contexts"]
    if not isinstance(contexts, Mapping) or any(type(key) is not str for key in contexts):
        raise ValueError(f"{field} has invalid annotation contexts")
    for context_key, raw_context in contexts.items():
        _native_string(context_key, field=field, nonblank=True)
        context = _object_with_keys(raw_context, _ANNOTATION_CONTEXT_KEYS, field=field)
        if context["target_key"] != context_key:
            raise ValueError(f"{field} annotation context key does not match target_key")
        for name in ("target_key", "resource_href", "fragment"):
            _native_string(context[name], field=field, nonblank=True)
        _package_path(context["resource_href"], field=field)
        if context["target_key"] != _source_target_key(
            context["resource_href"],
            context["fragment"],
        ):
            raise ValueError(f"{field} annotation target_key disagrees with its source locator")
        _string_list(context["source_blocks"], field=field, nonblank=True, require_items=True)
        _string_list(context["segment_anchors"], field=field, nonblank=True, require_items=True)


def _native_int(
    value: object,
    *,
    field: str,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    """Require an exact bounded integer so booleans cannot impersonate offsets."""
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"{field} has an invalid integer value")
    return value


def _native_string(value: object, *, field: str, nonblank: bool = False) -> str:
    """Require native UTF-8 text and optionally a nonblank producer identity."""
    if type(value) is not str or (nonblank and not value.strip()):
        raise ValueError(f"{field} has an invalid string value")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError(f"{field} has an invalid string value") from None
    return value


def _native_choice(value: object, *, choices: frozenset[str], field: str) -> str:
    """Require one native string enum without leaking malformed input values."""
    choice = _native_string(value, field=field)
    if choice not in choices:
        raise ValueError(f"{field} has an unsupported string value")
    return choice


def _package_path(value: object, *, field: str) -> str:
    """Require one normalized, package-relative EPUB member path."""
    path = _native_string(value, field=field, nonblank=True)
    parsed = urlsplit(path)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or path.startswith(("/", "\\"))
        or "\\" in path
    ):
        raise ValueError(f"{field} must be a package-relative EPUB path")
    normalized = posixpath.normpath(path)
    if normalized in {".", ".."} or normalized.startswith("../") or normalized != path:
        raise ValueError(f"{field} must be a normalized package-relative EPUB path")
    # POSIX normalization does not recognize a Windows drive prefix.
    first_component = normalized.partition("/")[0]
    if len(first_component) >= 2 and first_component[1] == ":":
        raise ValueError(f"{field} must be a package-relative EPUB path")
    return normalized


def _source_target_key(resource_href: object, fragment: object) -> str:
    """Rebuild the reader's stable key from its already-validated source fields."""
    if not resource_href:
        return ""
    return f"{resource_href}#{fragment}" if fragment else str(resource_href)


def _string_list(
    value: object,
    *,
    field: str,
    nonblank: bool = False,
    require_items: bool = False,
) -> list[str]:
    """Validate one ordered source-derived string collection."""
    if type(value) is not list or (require_items and not value):
        raise ValueError(f"{field} has an invalid string list")
    result: list[str] = []
    for item in value:
        result.append(_native_string(item, field=field, nonblank=nonblank))
    return result


def _canonical_chapter_tags(value: object) -> list[str] | None:
    """Turn the HTML reader policy set into deterministic durable ordering."""
    if value is None:
        return None
    if type(value) is not list:
        raise ValueError("document metadata chapter_tags must be a string list or null")
    tags = [_native_string(tag, field="document metadata", nonblank=True) for tag in value]
    return sorted(set(tags))


__all__ = ["encode_ingest_document_v1", "normalized_document_v1_from_ingest"]
