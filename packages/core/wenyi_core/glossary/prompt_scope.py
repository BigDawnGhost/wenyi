"""Bound the glossary one model request carries while preserving cache-friendly prefix order."""

from __future__ import annotations

from functools import lru_cache

from ..ingest.tokens import count_tokens
from ..models.glossary import TYPE_NAVIGATION, GlossaryTerm
from .store import GlossaryStore


def scope_navigation_terms(terms: list[GlossaryTerm], text: str) -> list[GlossaryTerm]:
    """Keep all non-navigation terms plus only navigation titles occurring in ``text``.

    EPUB navigation entries (hundreds of chapter titles in long web novels) are stored so
    Review and headings stay consistent when a title actually appears in body text,
    without bloating unrelated prompts.
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


def split_glossary_tiers(
    terms: list[GlossaryTerm],
    text: str,
    *,
    prefix_budget: int = 0,
) -> tuple[list[GlossaryTerm], list[GlossaryTerm]]:
    """Split ``terms`` into ``(frozen_system_prefix, per_batch_user_overflow)``.

    Budgeting the shared prefix independently from per-batch matches keeps the system
    prompt byte-identical across batches for DeepSeek KV prefix caching:
    - ``frozen_system_prefix`` holds the append-stable leading run of non-navigation
      entries within ``prefix_budget`` tokens (all non-navigation entries when ``0``).
    - ``per_batch_user_overflow`` holds only later non-navigation entries and
      ``TYPE_NAVIGATION`` entries that actually occur in ``text``.
    """
    if not terms:
        return [], []
    non_nav = [term for term in terms if term.type != TYPE_NAVIGATION]
    nav = [term for term in terms if term.type == TYPE_NAVIGATION]
    matched_nav = GlossaryStore.terms_in(nav, text) if (nav and text) else []

    if prefix_budget <= 0:
        return non_nav, matched_nav

    prefix: list[GlossaryTerm] = []
    overflow_pool: list[GlossaryTerm] = []
    spent = 0
    capped = False
    for term in non_nav:
        if not capped:
            cost = estimate_term_tokens(term)
            if spent + cost <= prefix_budget:
                prefix.append(term)
                spent += cost
                continue
            capped = True
        overflow_pool.append(term)

    matched_overflow = (
        GlossaryStore.terms_in(overflow_pool, text) if (overflow_pool and text) else []
    )
    return prefix, [*matched_overflow, *matched_nav]


def split_trimmed_glossary_tiers(
    full_terms: list[GlossaryTerm],
    selected_terms: list[GlossaryTerm],
    text: str,
    *,
    prefix_budget: int = 0,
) -> tuple[list[GlossaryTerm], list[GlossaryTerm]]:
    """Split ``selected_terms`` into ``(system_prefix, user_overflow)`` without promoting
    per-batch overflow terms into the shared system prefix when preflight trims ``full_terms``.
    """
    if len(selected_terms) >= len(full_terms):
        return split_glossary_tiers(selected_terms, text, prefix_budget=prefix_budget)
    orig_prefix, _ = split_glossary_tiers(full_terms, text, prefix_budget=prefix_budget)
    selected_ids = {id(term) for term in selected_terms}
    stable_prefix: list[GlossaryTerm] = []
    prefix_ids: set[int] = set()
    for term in orig_prefix:
        if id(term) not in selected_ids:
            break
        stable_prefix.append(term)
        prefix_ids.add(id(term))
    overflow_pool = [term for term in selected_terms if id(term) not in prefix_ids]
    matched_overflow = (
        GlossaryStore.terms_in(overflow_pool, text) if (overflow_pool and text) else []
    )
    return stable_prefix, matched_overflow


def prompt_terms(
    terms: list[GlossaryTerm],
    text: str,
    *,
    budget: int = 0,
) -> list[GlossaryTerm]:
    """Return the cache-stable entity prefix plus per-batch matched overflow and nav terms."""
    prefix, overflow = split_glossary_tiers(terms, text, prefix_budget=budget)
    return [*prefix, *overflow]


def fit_terms_to_budget(
    terms: list[GlossaryTerm],
    text: str,
    *,
    max_tokens: int,
) -> list[GlossaryTerm]:
    """Hard-cap ``terms`` to fit within ``max_tokens``, preserving the append-stable
    leading non-navigation prefix first for KV cache stability and filling remaining room
    with matched overflow/navigation entries.
    """
    if not terms or max_tokens <= 0:
        return []
    chosen_ids: set[int] = set()
    spent = 0
    # Pass 1: preserve the append-stable leading non-navigation prefix so batches under
    # preflight trimming still share a byte-identical system prefix.
    for term in terms:
        if term.type == TYPE_NAVIGATION:
            continue
        cost = estimate_term_tokens(term)
        if spent + cost > max_tokens:
            break
        chosen_ids.add(id(term))
        spent += cost
    # Pass 2: fill any remaining budget with entries occurring in the request text
    # (matched overflow entities and matching navigation titles).
    if spent < max_tokens and text:
        remaining = [term for term in terms if id(term) not in chosen_ids]
        for term in GlossaryStore.terms_in(remaining, text):
            cost = estimate_term_tokens(term)
            if spent + cost > max_tokens:
                continue
            chosen_ids.add(id(term))
            spent += cost
    return [term for term in terms if id(term) in chosen_ids]
