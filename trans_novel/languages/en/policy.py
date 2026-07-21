"""English instruction and target-language policies."""

from __future__ import annotations

from ..common.registry import TargetProfile
from ..common.tags import base_language, canonical_tag


_LABELS = {
    "ja": "Japanese",
    "en": "English",
    "zh": "Chinese",
    "ru": "Russian",
    "ko": "Korean",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "it": "Italian",
    "pt": "Portuguese",
}


def language_label(tag: str | None) -> str:
    """Return a language name suitable for English instructions."""
    canonical = canonical_tag(tag)
    base = base_language(canonical)
    if base in _LABELS:
        name = _LABELS[base]
        return name if canonical == base else f"{name} ({canonical})"
    return f"language {canonical}" if canonical else "the source language"


def target_profile(target: str | None) -> TargetProfile:
    """Return English prose rules or a conservative generic target profile."""
    if base_language(target) == "en":
        return TargetProfile(
            prose_guidance=(
                "Write natural, idiomatic literary English. Keep tense, narrative "
                "person, character register, and dialogue voice consistent. Handle "
                "articles, pronouns, collocations, and syntax as English requires "
                "instead of copying the source language word for word."
            ),
            title_guidance=(
                "Keep titles concise and suitable for English-language publishing. "
                "Use one consistent English title-capitalization style, translate "
                "generic volume and chapter markers naturally, and add no quotation "
                "marks, explanations, or information absent from the source."
            ),
            punctuation_guidance=(
                "Preserve the structural role, hierarchy, count, repetition, and "
                "pairing of punctuation and special symbols in the source. Ordinary "
                "sentence punctuation may move as English syntax requires. Use "
                "standard English typography: half-width commas, periods, question "
                "marks, exclamation marks, colons, and semicolons; paired ASCII "
                "half-width straight double quotation marks (\") and single "
                "quotation marks or apostrophes ('); a single em dash (—); and an "
                "ellipsis (…). Never use curly smart quotation marks. Do not "
                "emit Chinese full-width sentence punctuation, book-title marks, or "
                "Japanese corner brackets unless a symbol must remain verbatim as "
                "part of the work."
            ),
            chapter_digest_limit="160 English words",
            book_synopsis_limit="350 English words",
        )
    target_name = language_label(target)
    return TargetProfile(
        prose_guidance=(
            f"Write natural, accurate literary {target_name}. Preserve all information, "
            "narrative person, register, and character voice while adapting syntax to "
            "the conventions of the target language."
        ),
        title_guidance=(
            f"Keep titles concise and consistent with {target_name} publishing "
            "conventions. Translate generic volume and chapter markers naturally and "
            "add no explanation or information absent from the source."
        ),
        punctuation_guidance=(
            "Preserve the structural role, hierarchy, count, repetition, and pairing "
            f"of punctuation and special symbols, and use standard {target_name} "
            "typography. Do not remove or add unusual symbols when their function is "
            "uncertain."
        ),
        chapter_digest_limit="about 160 words or an equivalent short passage",
        book_synopsis_limit="about 350 words or an equivalent short passage",
    )


def source_guidance(source: str | None) -> str:
    """Return source-language interpretation rules in English."""
    base = base_language(source)
    if base == "ja":
        return (
            "- Recover subjects, objects, and logical links commonly omitted in "
            "Japanese from context, but invent no information.\n"
            "- Use first-person forms such as 私, 僕, 俺, and あたし, levels of "
            "politeness, and sentence endings to identify register and relationships.\n"
            "- Reproduce the meaning, rhythm, and emotional function of mimetic words "
            "instead of transliterating them mechanically.\n"
            "- Interpret kanji compounds in context rather than assuming a familiar "
            "character sequence has the same meaning in another language."
        )
    if base == "en":
        return (
            "- Resolve tense, modality, reference, negation scope, and clause logic "
            "accurately.\n"
            "- Preserve the identity, distance, and tone conveyed by forms of address "
            "such as Mr., Ms., and Sir.\n"
            "- Passive constructions, long sentences, and relative clauses may be "
            "restructured, but none of their logical relationships may be lost."
        )
    return (
        "- Resolve meaning, reference, tone, and syntactic relationships accurately; "
        "do not lose information while restructuring the source."
    )


def pair_guidance(
    source: str | None,
    target: str | None,
    honorific_strategy: str = "keep_style",
) -> str:
    """Return source-target rules written entirely in English."""
    src = base_language(source)
    tgt = base_language(target)
    if src == "ja" and tgt == "en":
        if honorific_strategy == "drop":
            honorific = (
                "Normally omit suffixes such as -san, -kun, and -chan, but preserve "
                "the status, intimacy, and tone they convey through wording."
            )
        elif honorific_strategy == "normalize":
            honorific = (
                "Choose a consistent English treatment for each major honorific and "
                "retain romanized suffixes only when the narrative needs them."
            )
        else:
            honorific = (
                "Retain reader-comprehensible, narratively meaningful forms such as "
                "-san, -kun, -chan, -sama, and senpai when appropriate, or express the "
                "relationship naturally in English; treat each relationship consistently."
            )
        return (
            f"- Japanese honorifics: {honorific}\n"
            "- Use consistent romanization or an established English form for names, "
            "and never change the chosen name order mid-book."
        )
    if src == "ja":
        return (
            "- Preserve relationship and register information carried by Japanese "
            "honorifics, using one consistent treatment for each relationship.\n"
            "- Use consistent romanization or an established target-language form for "
            "Japanese names."
        )
    if src == "en":
        return (
            "- Treat forms of address such as Mr., Ms., and Sir consistently according "
            "to character relationships and target-language conventions.\n"
            "- Prefer established target-language forms of proper names; otherwise use "
            "one consistent transliteration or translation."
        )
    return "(no additional language-pair rules)"


def term_guidance(source: str | None, target: str | None) -> str:
    """Return glossary-field instructions in English."""
    src = base_language(source)
    if src == "ja":
        reading = (
            "Use the reading field for kana readings that disambiguate names and "
            "proper nouns"
        )
    elif src == "zh":
        reading = (
            "The reading field may contain pinyin or another pronunciation useful for "
            "target-language rendering"
        )
    else:
        reading = "Leave reading empty when pronunciation does not help determine the name"
    target_name = language_label(target)
    if base_language(target) == "en":
        categories = (
            "person, place, organization, term, skill, appellation, honorific, "
            "speech pattern, or fixed expression"
        )
        genders = "male, female, or unknown"
    else:
        categories = f"short category names written in {target_name}"
        genders = f"the standard {target_name} equivalents of male, female, or unknown"
    return (
        f"{reading}. Write target, type, gender, and note in {target_name}. Use "
        f"{categories} for type. Fill gender only for people, using {genders}; leave it "
        "empty for other entries. Infer gender and pronoun information only from "
        "character facts and context."
    )


def default_term_type(_target: str | None) -> str:
    """Return the fallback term category used by the English bundle."""
    return "term"


def default_person_type(_target: str | None) -> str:
    """Return the fallback person category used by the English bundle."""
    return "person"
