"""Resolve and render complete target-language prompt bundles.

This module intentionally contains no natural-language task instructions.
Chinese and English system/user templates, labels, formatters, and punctuation
policies live under :mod:`trans_novel.languages`.
"""

from __future__ import annotations

from typing import Any

from .. import languages
from ..glossary.store import GlossaryTerm


def render(
    name: str,
    *,
    src: str = "ja",
    tgt: str = "zh",
    honorific_strategy: str = "keep_style",
    **kwargs: Any,
) -> str:
    """Render ``name`` from the complete bundle selected by ``tgt``."""
    bundle = languages.get_bundle(tgt)
    template = bundle.templates[name]
    profile = bundle.target_profile(tgt)
    kwargs.setdefault("src_label", bundle.language_label(src))
    kwargs.setdefault("tgt_label", bundle.language_label(tgt))
    kwargs.setdefault("source_guidance", bundle.source_guidance(src))
    kwargs.setdefault("target_guidance", profile.prose_guidance)
    kwargs.setdefault(
        "pair_guidance",
        bundle.pair_guidance(src, tgt, honorific_strategy),
    )
    kwargs.setdefault("term_guidance", bundle.term_guidance(src, tgt))
    kwargs.setdefault("punct_rule", profile.punctuation_guidance)
    kwargs.setdefault("title_guidance", profile.title_guidance)
    kwargs.setdefault("digest_limit", profile.chapter_digest_limit)
    kwargs.setdefault("synopsis_limit", profile.book_synopsis_limit)
    return template.safe_substitute(**kwargs)


def render_glossary(terms: list[GlossaryTerm], *, tgt: str) -> str:
    """Render stored glossary values verbatim with target-bundle labels."""
    return languages.render_glossary(terms, target=tgt)


def empty(*, tgt: str) -> str:
    """Return the target bundle's explicit empty-section marker."""
    return languages.empty_value(tgt)


def numbered(texts: list[str]) -> str:
    """Render texts as a zero-based bracketed sequence."""
    return "\n".join(f"[{index}] {text}" for index, text in enumerate(texts))


def numbered_pairs(
    sources: list[str], targets: list[str], *, tgt: str
) -> str:
    """Render indexed source/translation pairs in the target bundle."""
    return languages.numbered_pairs(sources, targets, target=tgt)


def backtranslation_pairs(
    sources: list[str], backtranslations: list[str], *, tgt: str
) -> str:
    """Render indexed source/backtranslation pairs in the target bundle."""
    return languages.backtranslation_pairs(
        sources,
        backtranslations,
        target=tgt,
    )
