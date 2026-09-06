"""Compatibility entry point for pure language helpers supplied by top-level i18n."""

from ..i18n.languages import normalize_language as normalize_lang
from ..i18n.languages import require_language, validate_run_languages

__all__ = ["normalize_lang", "require_language", "validate_run_languages"]
