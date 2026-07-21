"""Target-language prompt bundle registry."""

from __future__ import annotations

from dataclasses import dataclass
from string import Template
from typing import Any, Callable, Mapping

from .tags import is_simplified_chinese


@dataclass(frozen=True)
class TargetProfile:
    """Target-language prose, title, typography, and summary policies."""

    prose_guidance: str
    title_guidance: str
    punctuation_guidance: str
    chapter_digest_limit: str
    book_synopsis_limit: str


@dataclass(frozen=True)
class LanguageBundle:
    """Complete model-facing language policy for one instruction language."""

    templates: Mapping[str, Template]
    target_profile: Callable[[str | None], TargetProfile]
    language_label: Callable[[str | None], str]
    source_guidance: Callable[[str | None], str]
    pair_guidance: Callable[[str | None, str | None, str], str]
    term_guidance: Callable[[str | None, str | None], str]
    default_term_type: Callable[[str | None], str]
    default_person_type: Callable[[str | None], str]
    formatters: Any
    punctuation: Any


def get_bundle(target: str | None) -> LanguageBundle:
    """Resolve the complete bundle for ``target``.

    Simplified Chinese uses the Chinese bundle. English is the default
    instruction bundle for every other target until that target gains its own
    complete set of prompts.
    """
    if is_simplified_chinese(target):
        from ..zh import BUNDLE

        return BUNDLE
    from ..en import BUNDLE

    return BUNDLE
