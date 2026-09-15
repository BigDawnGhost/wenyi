"""Pure language rules and prompt resources without agent, pipeline or state dependencies."""

from .languages import normalize_language, require_language

__all__ = ["normalize_language", "require_language"]
