"""Prompt compatibility entry point; task and language text lives in trans_novel/i18n/data."""

from __future__ import annotations

import json

from ..glossary.store import GlossaryTerm
from ..i18n.prompts import render, template
from . import langprofile

__all__ = ["render", "langprofile"]

TRANSLATOR_SYSTEM = template("translator_system")
TRANSLATOR_USER = template("translator_user")
REVIEWER_SYSTEM = template("reviewer_system")
REVIEWER_USER = template("reviewer_user")
REVIEW_AGENT_SYSTEM = template("review_agent_system")
REVIEW_AGENT_USER = template("review_agent_user")
REVIEW_ARBITER_SYSTEM = template("review_arbiter_system")
REVIEW_ARBITER_USER = template("review_arbiter_user")
REVIEW_FIXER_SYSTEM = template("review_fixer_system")
REVIEW_FIXER_USER = template("review_fixer_user")
POLISHER_SYSTEM = template("polisher_system")
POLISHER_USER = template("polisher_user")
TITLE_TRANSLATOR_SYSTEM = template("title_translator_system")
TITLE_TRANSLATOR_USER = template("title_translator_user")
ANALYZER_SYSTEM = template("analyzer_system")
ANALYZER_USER = template("analyzer_user")
GLOSSARY_EXTRACTOR_SYSTEM = template("glossary_extractor_system")
GLOSSARY_EXTRACTOR_USER = template("glossary_extractor_user")
GLOSSARY_HISTORY_SYSTEM = template("glossary_history_system")
GLOSSARY_HISTORY_USER = template("glossary_history_user")
CHAPTER_DIGEST_SYSTEM = template("chapter_digest_system")
CHAPTER_DIGEST_USER = template("chapter_digest_user")
BOOK_SYNOPSIS_SYSTEM = template("book_synopsis_system")
BOOK_SYNOPSIS_USER = template("book_synopsis_user")


def render_glossary(terms: list[GlossaryTerm]) -> str:
    """Render glossary objects as a line-by-line reference for prompts."""
    if not terms:
        return "(none)"
    lines = []
    for t in terms:
        extra = []
        if t.gender:
            extra.append(t.gender)
        if t.reading:
            extra.append(f"Pronunciation: {t.reading}")
        tag = f"({t.type}{(', ' + ', '.join(extra)) if extra else ''})"
        alias = f" [Aliases:  {', '.join(t.aliases)}]" if t.aliases else ""
        lines.append(f"- {t.source} → {t.target}{tag}{alias}")
    return "\n".join(lines)


def render_annotation_contexts(contexts: list[list[dict[str, str]]]) -> str:
    """Deduplicate annotation data into stable JSON while retaining applicable batch indices."""
    rendered_by_key: dict[str, dict[str, object]] = {}
    for segment_index, items in enumerate(contexts):
        for item in items:
            target_key = item["target_key"]
            source = item["source"]
            rendered = rendered_by_key.get(target_key)
            if rendered is None:
                rendered_by_key[target_key] = {
                    "target_key": target_key,
                    "source": source,
                    "applies_to": [segment_index],
                }
                continue
            if rendered["source"] != source:
                raise ValueError(f"Inconsistent text for annotation target: {target_key}")
            applies_to = rendered["applies_to"]
            if isinstance(applies_to, list) and segment_index not in applies_to:
                applies_to.append(segment_index)
    return json.dumps(list(rendered_by_key.values()), ensure_ascii=False, indent=2)


def numbered(texts: list[str]) -> str:
    """Render text with zero-based indices in square brackets."""
    return "\n".join(f"[{i}] {t}" for i, t in enumerate(texts))


def numbered_pairs(sources: list[str], targets: list[str]) -> str:
    """Render aligned source/target pairs for review prompts."""
    out = []
    for i, (s, t) in enumerate(zip(sources, targets)):
        out.append(f"[{i}] Source: {s}\n    Translation: {t}")
    return "\n".join(out)


def numbered_pairs_with_refs(
    sources: list[str],
    targets: list[str],
    refs: list[str],
) -> str:
    """Render source/target pairs with stable segment references for evidence review."""
    out = []
    for index, (source, target) in enumerate(zip(sources, targets)):
        ref = refs[index] if index < len(refs) else ""
        out.append(f"[{index}] ref={ref or '(none)'} Source: {source}\n    Translation: {target}")
    return "\n".join(out)
