"""Preflight context-window checks that shrink glossary prompts or fail with actionable errors."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from ..glossary.prompt_scope import estimate_term_tokens, fit_terms_to_budget
from ..ingest.tokens import count_tokens
from ..models.glossary import GlossaryTerm
from .routing import resolve_routes

if TYPE_CHECKING:
    from ..config import Config

_LOGGER = logging.getLogger(__name__)
_DEFAULT_OUTPUT_RESERVE = 4096


def _output_reserve(configured_max_output: int | None, context_window: int) -> int:
    """Reserve configured output tokens (capped sensibly) or a conservative default."""
    if configured_max_output is not None and configured_max_output > 0:
        return configured_max_output
    return min(_DEFAULT_OUTPUT_RESERVE, max(1, context_window // 4))


def fit_prompt_to_context_window(
    *,
    config: Config,
    operation: str,
    terms: list[GlossaryTerm],
    match_text: str,
    render_messages: Callable[[list[GlossaryTerm]], tuple[str, str]],
) -> tuple[str, str, list[GlossaryTerm], bool]:
    """Render ``(system, user)`` and verify prompt + output reserve fits the model context window.

    When the prompt exceeds the available budget, shrinks the glossary to fit and logs a
    warning. Raises ``ValueError`` with actionable configuration hints if the request still
    cannot fit even without glossary entries.
    """
    system, user = render_messages(terms)
    route = resolve_routes(config.llm).get(operation)
    window = route.context_window if route is not None else None
    if window is None:
        return system, user, terms, False

    reserve = _output_reserve(route.max_output_tokens if route is not None else None, window)
    available = window - reserve
    prompt_tokens = count_tokens(system) + count_tokens(user)
    if prompt_tokens <= available:
        return system, user, terms, False

    shrunk_terms = terms
    if terms and available > 0:
        # Measure overhead without glossary to compute the exact glossary budget that fits.
        base_system, base_user = render_messages([])
        base_tokens = count_tokens(base_system) + count_tokens(base_user)
        glossary_room = available - base_tokens
        if glossary_room <= 0:
            shrunk_terms = []
            system, user = base_system, base_user
            prompt_tokens = base_tokens
        else:
            # Start with the exact room and tighten if rendered formatting overhead exceeds estimate.
            budget = glossary_room
            for _ in range(4):
                candidate = fit_terms_to_budget(terms, match_text, max_tokens=budget)
                cand_sys, cand_user = render_messages(candidate)
                cand_tokens = count_tokens(cand_sys) + count_tokens(cand_user)
                if cand_tokens <= available or not candidate:
                    shrunk_terms = candidate
                    system, user = cand_sys, cand_user
                    prompt_tokens = cand_tokens
                    break
                overflow = cand_tokens - available
                estimated = sum(estimate_term_tokens(t) for t in candidate)
                budget = max(0, estimated - overflow - 64)
            else:
                shrunk_terms = []
                system, user = base_system, base_user
                prompt_tokens = base_tokens

        if prompt_tokens <= available:
            _LOGGER.warning(
                "Prompt for %s exceeded model context_window=%d (output_reserve=%d); "
                "shrunk glossary from %d to %d terms (%d prompt tokens).",
                operation,
                window,
                reserve,
                len(terms),
                len(shrunk_terms),
                prompt_tokens,
            )
            return system, user, shrunk_terms, True

    raise ValueError(
        f"Prompt for {operation} (~{prompt_tokens} tokens) plus output reserve ({reserve} tokens) "
        f"exceeds context_window ({window} tokens) even after shrinking the glossary. "
        "Lower pipeline.glossary_prompt_tokens, set pipeline.batch_glossary=relevant, "
        "reduce segment.max_tokens_per_batch, or increase the model's context_window."
    )
