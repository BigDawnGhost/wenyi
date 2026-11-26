"""Continuation-aware, source-grounded sparse semantic and craft editor.

Replaces source-blind full-batch rewriting with a source-grounded audit that returns JSON
patches *only* for paragraphs with genuine fidelity or readability defects:
- Omitted clauses, mistranslations, negation/conditional/agency inversions
- Wrong referents, pronoun/gender contradictions, glossary mismatches
- Literalized idioms (成语), stiff Chinese calques, or awkward dialogue flow

Two execution paths:
1. ``edit_continue`` appends a short audit turn directly onto the translation conversation
   (``draft.turn``) so DeepSeek gets ~100% KV prefix cache hits on ``[system + style +
   synopsis + glossary + source + draft]`` while outputting only defective paragraphs.
2. ``edit_batch`` provides a standalone fallback (when ``polish.body`` uses a different
   route or continuation is unavailable) that still caches ``[system + style + glossary]``.

Pure agent module: zero heuristics, zero silent thinking escalation, zero pipeline imports.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

from ..glossary.prompt_scope import split_trimmed_glossary_tiers
from ..llm.json_parser import JsonParseError
from ..llm.preflight import fit_prompt_to_context_window
from ..llm.retrying import TruncatedResponseError, is_resumable_provider_interrupt
from ..models.glossary import GlossaryTerm
from . import prompts
from .base import Agent, Messages

_LOGGER = logging.getLogger(__name__)
_MARKER_RE = re.compile(r"〘object:[^〙]+〙|⟦[^⟧]+⟧")

_EDIT_RULES = (
    "Audit each translated paragraph against its source paragraph. Return JSON patches ONLY "
    "for paragraphs with a clear fidelity or readability defect:\n"
    "- Omitted clauses, wrong numbers/negation/conditionals, or reversed speaker/agent\n"
    "- Wrong pronoun/gender, glossary inconsistency, or untranslated source residue\n"
    "- Literalized idioms, stiff translationese calques, or broken embedded verse line breaks\n"
    "Leave accurate, natural paragraphs untouched; never rewrite for cosmetic synonym swaps.\n"
    "Preserve every protected marker (〘object:...〙, ⟦...⟧) verbatim and never blank a "
    "non-empty paragraph.\n"
    "Never enumerate or comment on clean paragraphs in internal reasoning; if no paragraph "
    'has a clear defect, immediately return {"patches":[]}.\n'
    'Return strict JSON: {"patches":[{"index":<int>,"text":"<replacement>","reason":"<short>"}]}'
)


@dataclass(frozen=True)
class SparseEditResult:
    """Result of one sparse audit pass over an aligned batch."""

    targets: tuple[str, ...]
    patched_indices: tuple[int, ...]
    reasons: dict[int, str]
    failed: bool = False


def _apply_validated_patches(
    drafts: Sequence[str],
    raw_patches: object,
) -> SparseEditResult:
    """Validate and apply JSON patches strictly; never blank drafts or drop markers."""
    draft_list = list(drafts)
    if not isinstance(raw_patches, list):
        return SparseEditResult(tuple(draft_list), (), {}, failed=True)

    n = len(draft_list)
    updated = list(draft_list)
    seen_indices: set[int] = set()
    patched_indices: list[int] = []
    reasons: dict[int, str] = {}

    for patch in raw_patches:
        if not isinstance(patch, dict):
            continue
        idx = patch.get("index")
        text = patch.get("text")
        if not isinstance(idx, int) or isinstance(idx, bool) or not (0 <= idx < n):
            continue
        if idx in seen_indices:
            continue
        if not isinstance(text, str):
            continue
        if draft_list[idx].strip() and not text.strip():
            continue
        if _MARKER_RE.findall(text) != _MARKER_RE.findall(draft_list[idx]):
            continue
        seen_indices.add(idx)
        if text == draft_list[idx]:
            continue
        reason = str(patch.get("reason") or "defect").strip()[:64]
        updated[idx] = text
        patched_indices.append(idx)
        reasons[idx] = reason

    return SparseEditResult(
        targets=tuple(updated),
        patched_indices=tuple(sorted(patched_indices)),
        reasons=reasons,
        failed=False,
    )


class SparseSemanticEditor(Agent):
    """Source-grounded sparse editor using the ``polish.body`` route."""

    def edit_continue(
        self,
        turn: Messages,
        drafts: Sequence[str],
        *,
        next_source: str = "",
    ) -> SparseEditResult | None:
        """Append a sparse audit turn to the translation conversation for KV prefix reuse.

        Returns ``None`` on invalid output so the caller can fall back cleanly to
        ``edit_batch`` or retain the saved draft.
        """
        n = len(drafts)
        if n <= 0 or len(turn) < 3:
            return None

        next_ref = prompts.render_source_reference(next_source)
        continue_user = (
            f"You just translated {n} paragraphs (indexed 0 through {n - 1}) above.\n"
            f"{_EDIT_RULES}\n"
            f"[Following source paragraph] (reference only; do not translate)\n{next_ref}"
        ) + prompts.protected_object_guidance(list(drafts))

        messages: Messages = [*turn, {"role": "user", "content": continue_user}]
        try:
            data, _raw = self._complete_json_turn(
                messages,
                operation="polish.body",
                require_complete_payload=True,
            )
        except (JsonParseError, TruncatedResponseError, ValueError) as exc:
            _LOGGER.warning("Sparse continuation returned invalid output (%s); falling back", exc)
            return None
        except Exception as exc:
            if is_resumable_provider_interrupt(exc):
                raise
            _LOGGER.warning("Sparse continuation failed (%s); falling back", exc)
            return None

        raw_patches = data.get("patches") if isinstance(data, dict) else None
        result = _apply_validated_patches(drafts, raw_patches)
        return None if result.failed else result

    def edit_batch(
        self,
        sources: Sequence[str],
        drafts: Sequence[str],
        *,
        glossary_terms: Sequence[GlossaryTerm] = (),
        style: str = "",
        next_source: str = "",
    ) -> SparseEditResult:
        """Audit an aligned ``(sources, drafts)`` batch in a standalone cached conversation."""
        src_list = list(sources)
        draft_list = list(drafts)
        n = len(draft_list)
        if n == 0 or len(src_list) != n:
            return SparseEditResult(tuple(draft_list), (), {}, failed=False)

        base_system = (
            f"You are a precise bilingual editor auditing {self.src}->{self.tgt} web-novel "
            f"translations.\n{_EDIT_RULES}"
        )
        style_block = (
            prompts.wrap_untrusted_style("Style guide", style.strip()) if style.strip() else ""
        )
        protected_guidance = prompts.protected_object_guidance([*src_list, *draft_list])
        pairs = "\n\n".join(
            f"[{i}]\nSRC: {src}\nDRF: {drf}"
            for i, (src, drf) in enumerate(zip(src_list, draft_list))
        )
        next_ref = prompts.render_source_reference(next_source)
        match_text = "\n".join(part for part in (*src_list, next_source) if part)
        prefix_budget = self.config.pipeline.glossary_prompt_tokens

        full_glossary = list(glossary_terms)

        def _render(terms_for_prompt: list[GlossaryTerm]) -> tuple[str, str]:
            entity_terms, extra_terms = split_trimmed_glossary_tiers(
                full_glossary, terms_for_prompt, match_text, prefix_budget=prefix_budget
            )
            sys_parts = [base_system]
            if style_block:
                sys_parts.append(style_block)
            if entity_terms:
                rendered = prompts.render_glossary(
                    entity_terms,
                    source_lang=self.src,
                    genre=self.language_policy.context.genre,
                )
                sys_parts.append(prompts.wrap_untrusted_glossary(rendered))
            system_text = "\n\n".join(sys_parts) + protected_guidance

            user_parts: list[str] = []
            if extra_terms:
                extra_rendered = prompts.render_glossary(
                    extra_terms,
                    source_lang=self.src,
                    genre=self.language_policy.context.genre,
                )
                user_parts.append(f"[Additional glossary]\n{extra_rendered}")
            user_parts.append(
                f"[Paragraph pairs] ({n} paragraphs, indexed 0 through {n - 1})\n{pairs}"
            )
            user_parts.append(f"[Following source paragraph] (reference only)\n{next_ref}")
            return system_text, "\n\n".join(user_parts)

        try:
            system_msg, user_msg, _effective, _shrunk = fit_prompt_to_context_window(
                config=self.config,
                operation="polish.body",
                terms=full_glossary,
                match_text=match_text,
                render_messages=_render,
            )
        except ValueError as exc:
            _LOGGER.warning(
                "Sparse editor prompt does not fit context window (%s); keeping drafts", exc
            )
            return SparseEditResult(tuple(draft_list), (), {}, failed=True)
        try:
            data, _raw = self._complete_json_turn(
                [
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                ],
                operation="polish.body",
                require_complete_payload=True,
            )
        except (JsonParseError, TruncatedResponseError, ValueError) as exc:
            _LOGGER.warning("Sparse editor returned invalid output (%s); keeping drafts", exc)
            return SparseEditResult(tuple(draft_list), (), {}, failed=True)
        except Exception as exc:
            if is_resumable_provider_interrupt(exc):
                raise
            _LOGGER.warning("Sparse editor failed (%s); keeping drafts", exc)
            return SparseEditResult(tuple(draft_list), (), {}, failed=True)

        raw_patches = data.get("patches") if isinstance(data, dict) else None
        return _apply_validated_patches(draft_list, raw_patches)
