"""Bound the glossary one model request carries while preserving cache-friendly insertion order."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from ..ingest.tokens import count_tokens
from ..models.glossary import TYPE_NAVIGATION, GlossaryTerm
from .store import GlossaryStore


def scope_navigation_terms(terms: list[GlossaryTerm], text: str) -> list[GlossaryTerm]:
    """Keep all non-navigation terms plus only navigation titles occurring in ``text``.

    EPUB navigation entries (thousands of chapter titles in long books) are stored so
    Review and headings can stay consistent when a title actually appears in body text,
    but dumping unrelated chapter titles into every prompt bloats context windows.
    """
    if not terms:
        return terms
    nav_terms = [term for term in terms if term.type == TYPE_NAVIGATION]
    if not nav_terms:
        return terms
    matched_nav_ids = (
        {id(term) for term in GlossaryStore.terms_in(nav_terms, text)} if text else set()
    )
    return [term for term in terms if term.type != TYPE_NAVIGATION or id(term) in matched_nav_ids]


@lru_cache(maxsize=1 << 16)
def _mapping_tokens(source: str, target: str, term_type: str, aliases: tuple[str, ...]) -> int:
    """Estimate one rendered mapping line; the budget is a size target, not an exact cap."""
    return count_tokens(f"- {source} → {target}({term_type}) {' '.join(aliases)}")


def estimate_term_tokens(term: GlossaryTerm) -> int:
    """Return the estimated token cost of rendering one glossary entry."""
    return _mapping_tokens(term.source, term.target, term.type, tuple(term.aliases))


def prompt_terms(
    terms: list[GlossaryTerm],
    text: str,
    *,
    budget: int,
    mode: Literal["all", "relevant"] = "all",
) -> list[GlossaryTerm]:
    """Return the glossary entries one request carries, in stable insertion order.

    ``TYPE_NAVIGATION`` entries are always scoped to matches in ``text`` (even in ``all``
    mode) so stored chapter titles never bloat unrelated batches. ``budget == 0`` means no
    cap: ``all`` sends all non-navigation entries plus matching navigation entries, and
    ``relevant`` sends every entry matching ``text``. A positive ``budget`` is an opt-in cap:

    - In ``relevant`` mode, keeps entries matching ``text`` up to ``budget`` tokens.
    - In ``all`` mode, sends the scoped glossary while it fits ``budget`` tokens. When larger,
      keeps the append-stable leading run that fits ``budget`` tokens, plus any later entries
      matching ``text``.
    """
    if not terms:
        return []
    if mode == "relevant":
        matched = GlossaryStore.terms_in(terms, text)
        if budget <= 0:
            return matched
        selected: list[GlossaryTerm] = []
        spent = 0
        for term in matched:
            cost = estimate_term_tokens(term)
            if spent + cost > budget:
                break
            selected.append(term)
            spent += cost
        return selected

    scoped = scope_navigation_terms(terms, text)
    if budget <= 0:
        return scoped
    spent = 0
    for position, term in enumerate(scoped):
        spent += estimate_term_tokens(term)
        if spent > budget:
            return [*scoped[:position], *GlossaryStore.terms_in(scoped[position:], text)]
    return scoped


def fit_terms_to_budget(
    terms: list[GlossaryTerm],
    text: str,
    *,
    max_tokens: int,
) -> list[GlossaryTerm]:
    """Hard-cap ``terms`` to fit within ``max_tokens``, prioritizing entries matching ``text``
    while retaining original insertion order.
    """
    if not terms or max_tokens <= 0:
        return []
    matched_ids = {id(term) for term in GlossaryStore.terms_in(terms, text)}
    chosen_ids: set[int] = set()
    spent = 0
    # Pass 1: prioritize entries occurring in the request text (including matching nav titles).
    for term in terms:
        if id(term) not in matched_ids:
            continue
        cost = estimate_term_tokens(term)
        if spent + cost > max_tokens:
            continue
        chosen_ids.add(id(term))
        spent += cost
    # Pass 2: fill any remaining budget from the append-stable leading non-navigation prefix.
    for term in terms:
        if id(term) in chosen_ids or term.type == TYPE_NAVIGATION:
            continue
        cost = estimate_term_tokens(term)
        if spent + cost > max_tokens:
            break
        chosen_ids.add(id(term))
        spent += cost
    return [term for term in terms if id(term) in chosen_ids]
