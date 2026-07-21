"""Target-language prompt bundles and language-tag utilities.

Every model-facing natural-language string is selected by the translation
target. Simplified Chinese uses the Chinese bundle; English (and targets that
do not yet have a dedicated bundle) use the English instruction bundle.
"""

from __future__ import annotations

from typing import Any

from ..locales import message as ui_message
from .common.contracts import LANGUAGE_DETECTION_EXAMPLE
from .common.registry import LanguageBundle, TargetProfile, get_bundle
from .common.tags import (
    base_language,
    canonical_tag,
    epub_language_tag,
    filename_language_tag,
    is_simplified_chinese,
    normalize_detected_language,
    target_identity,
)


def target_profile(target: str | None) -> TargetProfile:
    """Return prose, title, punctuation, and summary rules for ``target``."""
    return get_bundle(target).target_profile(target)


def pair_guidance(
    source: str | None,
    target: str | None,
    honorific_strategy: str = "keep_style",
) -> str:
    """Return source-target guidance in the selected target bundle."""
    return get_bundle(target).pair_guidance(source, target, honorific_strategy)


def default_term_type(target: str | None) -> str:
    """Return the target bundle's fallback category for non-person terms."""
    return get_bundle(target).default_term_type(target)


def default_person_type(target: str | None) -> str:
    """Return the target bundle's fallback category for people."""
    return get_bundle(target).default_person_type(target)


def empty_value(target: str | None) -> str:
    """Return a localized explicit placeholder for an empty prompt section."""
    return str(get_bundle(target).formatters.EMPTY)


def render_glossary(terms: list[Any], *, target: str | None) -> str:
    """Render stored glossary fields verbatim using target-bundle labels."""
    return str(get_bundle(target).formatters.render_glossary(terms))


def style_brief(
    analysis: dict[str, Any],
    characters: list[dict[str, Any]],
    *,
    target: str | None,
) -> str:
    """Render a localized style and character brief for later model calls."""
    return str(get_bundle(target).formatters.style_brief(analysis, characters))


def numbered_pairs(
    sources: list[str], targets: list[str], *, target: str | None
) -> str:
    """Render source/translation pairs using target-bundle labels."""
    return str(get_bundle(target).formatters.numbered_pairs(sources, targets))


def backtranslation_pairs(
    sources: list[str], backtranslations: list[str], *, target: str | None
) -> str:
    """Render source/backtranslation pairs using target-bundle labels."""
    return str(
        get_bundle(target).formatters.backtranslation_pairs(
            sources, backtranslations
        )
    )


def chapter_label(title: str, index: int, *, target: str | None) -> str:
    """Return a target-bundle chapter label for model-facing excerpts."""
    return str(get_bundle(target).formatters.chapter_label(title, index))


def chapter_digest(label_text: str, snippet: str, *, target: str | None) -> str:
    """Render one model-facing chapter excerpt."""
    return str(get_bundle(target).formatters.chapter_digest(label_text, snippet))


def join_digest_segments(segments: list[str], *, target: str | None) -> str:
    """Join chapter excerpt segments using target-appropriate typography."""
    return str(get_bundle(target).formatters.join_digest_segments(segments))


def autofix_feedback(issues: list[dict[str, Any]], *, target: str | None) -> str:
    """Render reviewer feedback for targeted retranslation."""
    return str(get_bundle(target).formatters.autofix_feedback(issues))


def sample_labels(target: str | None) -> tuple[str, str, str]:
    """Return opening, middle, and ending sample labels for style analysis."""
    labels = get_bundle(target).formatters.SAMPLE_LABELS
    return str(labels[0]), str(labels[1]), str(labels[2])


def sample_block(label_text: str, text: str, *, target: str | None) -> str:
    """Render a location-labeled style-analysis sample."""
    return str(get_bundle(target).formatters.sample_block(label_text, text))


def punctuation_enabled(target: str | None) -> bool:
    """Return whether deterministic punctuation rewriting exists for target."""
    if not is_simplified_chinese(target) and base_language(target) != "en":
        return False
    return bool(get_bundle(target).punctuation.enabled)


def normalize_punctuation(text: str, *, target: str | None) -> str:
    """Apply the target bundle's deterministic single-segment normalization."""
    if not punctuation_enabled(target):
        return text
    return str(get_bundle(target).punctuation.normalize(text))


def normalize_punctuation_segments(
    texts: list[str],
    continuations: list[bool] | None = None,
    *,
    target: str | None,
) -> list[str]:
    """Apply the target bundle's deterministic multi-segment normalization."""
    if not punctuation_enabled(target):
        if continuations is not None and len(continuations) != len(texts):
            raise ValueError(ui_message("error.punctuation_segment_count_mismatch"))
        return list(texts)
    return list(
        get_bundle(target).punctuation.normalize_segments(texts, continuations)
    )


def language_detection_system() -> str:
    """Return the language-neutral detection contract in English."""
    return (
        "Identify the main natural language of the supplied text. Return only JSON: "
        f"{LANGUAGE_DETECTION_EXAMPLE}. If uncertain, set language to an empty string."
    )


__all__ = [
    "LanguageBundle",
    "TargetProfile",
    "autofix_feedback",
    "backtranslation_pairs",
    "base_language",
    "canonical_tag",
    "chapter_digest",
    "chapter_label",
    "default_person_type",
    "default_term_type",
    "empty_value",
    "epub_language_tag",
    "filename_language_tag",
    "get_bundle",
    "is_simplified_chinese",
    "join_digest_segments",
    "language_detection_system",
    "normalize_detected_language",
    "normalize_punctuation",
    "normalize_punctuation_segments",
    "numbered_pairs",
    "pair_guidance",
    "punctuation_enabled",
    "render_glossary",
    "sample_block",
    "sample_labels",
    "style_brief",
    "target_identity",
    "target_profile",
]
