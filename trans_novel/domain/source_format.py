"""Stable source-reader family names used by durable domain identities.

User-facing file suffixes are normalized at admission time.  Persisted
artifacts and workflow identities use only the five canonical reader families,
so aliases such as ``md``/``txt`` or ``htm``/``xhtml`` cannot fork recovery.
"""

from __future__ import annotations

CANONICAL_SOURCE_FORMATS = frozenset({"epub", "fb2", "html", "pdf", "text"})

# The alias table is deliberately small and mirrors the reader families the
# application can materialize.  Adding an alias is safe; adding a family is a
# durable-contract change and requires corresponding reader support.
_SOURCE_FORMAT_ALIASES = {
    "epub": "epub",
    "fb2": "fb2",
    "htm": "html",
    "html": "html",
    "xhtml": "html",
    "md": "text",
    "markdown": "text",
    "text": "text",
    "txt": "text",
    "pdf": "pdf",
}


def normalize_source_format(value: object, *, field: str = "source_format") -> str:
    """Normalize a user-facing suffix to one supported source-reader family."""
    raw = _require_native_utf8_text(value, field=field)
    candidate = raw.strip().lower().lstrip(".")
    canonical = _SOURCE_FORMAT_ALIASES.get(candidate)
    if canonical is None:
        allowed = ", ".join(sorted(CANONICAL_SOURCE_FORMATS))
        raise ValueError(f"{field} must map to a supported source format: {allowed}")
    return canonical


def validate_canonical_source_format(
    value: object,
    *,
    field: str = "source_format",
) -> str:
    """Require an already canonical source-reader family without rewriting it."""
    source_format = _require_native_utf8_text(value, field=field)
    if source_format not in CANONICAL_SOURCE_FORMATS:
        allowed = ", ".join(sorted(CANONICAL_SOURCE_FORMATS))
        raise ValueError(f"{field} must be one canonical source format: {allowed}")
    return source_format


def _require_native_utf8_text(value: object, *, field: str) -> str:
    """Reject subclasses, blank values, and strings that cannot cross JSON/UTF-8."""
    if type(value) is not str or not value.strip():
        raise ValueError(f"{field} must be a non-empty native string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{field} must be UTF-8 encodable") from error
    return value


__all__ = [
    "CANONICAL_SOURCE_FORMATS",
    "normalize_source_format",
    "validate_canonical_source_format",
]
