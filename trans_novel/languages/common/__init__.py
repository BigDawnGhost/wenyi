"""Stable contracts, language tags, and prompt-bundle registration."""

from .registry import LanguageBundle, TargetProfile, get_bundle
from .tags import (
    base_language,
    canonical_tag,
    epub_language_tag,
    filename_language_tag,
    is_simplified_chinese,
    normalize_detected_language,
    target_identity,
)

__all__ = [
    "LanguageBundle",
    "TargetProfile",
    "base_language",
    "canonical_tag",
    "epub_language_tag",
    "filename_language_tag",
    "get_bundle",
    "is_simplified_chinese",
    "normalize_detected_language",
    "target_identity",
]
