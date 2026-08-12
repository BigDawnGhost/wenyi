"""Canonical source-reader family normalization tests."""

from __future__ import annotations

import pytest

from trans_novel.domain.source_format import (
    normalize_source_format,
    validate_canonical_source_format,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (".EPUB", "epub"),
        ("fb2", "fb2"),
        (".HTM", "html"),
        ("xhtml", "html"),
        (".TXT", "text"),
        ("md", "text"),
        ("markdown", "text"),
        ("pdf", "pdf"),
    ],
)
def test_source_suffix_aliases_map_to_reader_families(raw: str, expected: str) -> None:
    """Admission aliases collapse before they enter workflow identity."""
    assert normalize_source_format(raw) == expected


@pytest.mark.parametrize("raw", ["", ".", "docx", "epub.zip", 1, True, chr(0xD800)])
def test_source_format_normalizer_rejects_unsupported_or_unstable_values(raw: object) -> None:
    with pytest.raises(ValueError):
        normalize_source_format(raw)


@pytest.mark.parametrize("raw", ["txt", "markdown", "xhtml", "EPUB", ".pdf"])
def test_persisted_source_format_validator_does_not_rewrite_aliases(raw: object) -> None:
    """Persisted contracts fail closed instead of silently changing identity."""
    with pytest.raises(ValueError):
        validate_canonical_source_format(raw)
