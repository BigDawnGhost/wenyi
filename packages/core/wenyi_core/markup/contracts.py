"""Shared serialized markup metadata keys and structural tag definitions."""

from __future__ import annotations

_BLOCK_TAGS = {
    "p",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "blockquote",
    "td",
    "th",
    "dt",
    "dd",
    "figcaption",
}


_BLOCK_CANDIDATE_TAGS = _BLOCK_TAGS | {"div"}


_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


INLINE_META_KEY = "epub_inline"


INLINE_ID_ATTR = "data-tn-inline-id"


ANNOTATION_META_KEY = "epub_annotations"


ANNOTATION_ID_ATTR = "data-tn-annotation-id"


_ATOMIC_INLINE_TAGS = {
    "audio",
    "canvas",
    "embed",
    "hr",
    "iframe",
    "img",
    "math",
    "object",
    "source",
    "svg",
    "video",
}


LINE_WRAPPER_ATTR = "data-tn-line"
