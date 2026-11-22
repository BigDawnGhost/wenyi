"""Sparse source-grounded semantic and craft editor.

Replaces full-batch source-blind polishing (which rewrites ~84% of paragraphs without
seeing the source and fixes zero meaning errors) with a source-aware audit that returns
JSON patches *only* for paragraphs with genuine fidelity or craft defects:
- Negation scope, conditional, causal, or agency inversions
- Wrong referents, pronoun/gender contradictions, or omitted clauses
- Literalized idioms, missed classical allusions, or stiff translationese calques
- Broken verse lineation or inconsistent dialogue punctuation
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass

from ..ingest.segmenter import is_verse_segment
from ..llm.json_parser import JsonParseError
from ..llm.preflight import fit_prompt_to_context_window
from ..llm.retrying import TruncatedResponseError, is_resumable_provider_interrupt
from ..models.glossary import GlossaryTerm
from . import prompts
from .base import Agent

_LOGGER = logging.getLogger(__name__)
_MARKER_RE = re.compile(r"⟦[^⟧]+⟧")


@dataclass(frozen=True)
class SparseEditResult:
    targets: tuple[str, ...]
    patched_indices: tuple[int, ...]
    reasons: dict[int, str]
    failed: bool = False


class SparseSemanticEditor(Agent):
    """Source-grounded sparse editor using the ``polish.body`` route."""

    def edit_batch(
        self,
        sources: Sequence[str],
        drafts: Sequence[str],
        *,
        glossary_terms: list[GlossaryTerm] | None = None,
        style: str = "",
        next_source: str = "",
    ) -> SparseEditResult:
        """Audit ``(sources, drafts)`` and return updated targets applying only valid patches."""
        n = len(drafts)
        if n == 0 or len(sources) != n:
            return SparseEditResult(tuple(drafts), (), {})

        src_list = list(sources)
        draft_list = list(drafts)
        prompt_vals = dict(self.language_policy.prompt_values)
        lang_rules = prompt_vals.get("configured_lang_guidance", "")
        punct_rule = prompt_vals.get("punct_rule", "")
        protected_guidance = prompts.protected_object_guidance(draft_list)

        system = (
            f"You are a senior bilingual literary editor auditing a {self.src} -> {self.tgt} draft translation "
            "against the original source text. Unlike a full rewriter, you ONLY emit patches for paragraphs "
            "that contain a concrete defect. Leave accurate, natural paragraphs UNCHANGED.\n"
            "Check every paragraph against its source for:\n"
            "1. Classical logic, numbers & direction: flipped negation scope across multi-clause imperatives "
            "(e.g. 'do not X and keep doing Y' wrongly turned into an affirmative 'just keep doing Y'), reversed "
            "conditionals ('if not A, then B'), reversed causality or timing, wrong arithmetic/ratios or classical "
            "time units (e.g. a two-hour shichen mistranslated as 'an hour'), swapped subject/object, plaintiff/defendant, "
            "or generational seniority, and anachronistic plurals (e.g. 'disciples' when only one disciple is present).\n"
            "2. Kinship, pronouns & register: in-law/kinship errors (confusing a spouse's siblings with aunts/uncles or "
            "misreading generational rank), wrong pronoun antecedents, lost court/humble register before monarchs or "
            "elders, literalized sovereign honorifics, and stiff calques (e.g. demonstrative 'That [Name]', awkward "
            "'this disciple took on a disciple', raw untranslated source characters, or parenthetical inline glosses).\n"
            "3. Idioms, allusions & terminology: vernacular false friends, missed classical/religious/astronomical "
            "allusions or stranded opaque transliterations where idiomatic target English fits the book's policy, "
            "dropped clauses, invented rhyme-padding words, glossary mismatches, and inconsistent capitalization.\n"
            "4. Verse vs prose craft & paragraph boundaries: multi-clause poems and parallel couplets MUST use explicit "
            "line breaks ('\\n', never inline prose or ' / '); conversely, NEVER split a continuous prose dialogue "
            "paragraph with '\\n' line breaks; and NEVER copy or anticipate text belonging to [Following source paragraph].\n"
            f"Language & formatting rules:\n{lang_rules}\n{punct_rule}\n"
            "Preserve every protected marker (e.g. ⟦...⟧) verbatim if present.\n"
            'Return ONLY JSON: {"patches": [{"index": 0, "reason": "short_tag", "text": "corrected paragraph"}]}. '
            'If every paragraph is faithful and natural, return {"patches": []}.'
            + protected_guidance
        )

        pairs_lines = []
        for idx, (src, tgt) in enumerate(zip(src_list, draft_list)):
            pairs_lines.append(f"[{idx}] SRC: {src}\n[{idx}] DRF: {tgt}")
        paired_block = "\n\n".join(pairs_lines)
        next_ref = prompts.render_source_reference(next_source)

        unlineated_verse = [
            idx
            for idx, (src, tgt) in enumerate(zip(src_list, draft_list))
            if is_verse_segment(src) and "\n" not in tgt
        ]
        verse_alert = ""
        if len(unlineated_verse) >= 2:
            verse_alert = (
                f"\n[Verse formatting alert] Paragraphs {unlineated_verse} are rhymed verse couplets "
                "drafted without line breaks. Emit patches splitting each couplet onto two lines with '\\n'.\n"
            )

        def _render(terms_for_prompt: list[GlossaryTerm]) -> tuple[str, str]:
            glossary_block = prompts.render_glossary(
                terms_for_prompt,
                source_lang=self.src,
                genre=self.language_policy.context.genre,
            )
            user = (
                f"[Style guide]\n{style or '(none)'}\n\n"
                f"[Glossary]\n{glossary_block}\n\n"
                f"[Paragraphs to audit] ({n} paragraphs, 0..{n - 1})\n{paired_block}\n"
                f"{verse_alert}\n"
                f"[Following source paragraph] (reference only)\n{next_ref}\n\n"
                'Return ONLY JSON: {"patches": [{"index": <int>, "reason": "<tag>", "text": "<full revised paragraph>"}]}'
            )
            return system, user

        match_text = "\n".join(part for part in (*src_list, next_source) if part)
        sys_msg, user_msg, _eff, _shrunk = fit_prompt_to_context_window(
            config=self.config,
            operation="polish.body",
            terms=list(glossary_terms or ()),
            match_text=match_text,
            render_messages=_render,
        )

        try:
            data, _raw = self._complete_json_turn(
                [
                    {"role": "system", "content": sys_msg},
                    {"role": "user", "content": user_msg},
                ],
                operation="polish.body",
            )
        except (JsonParseError, TruncatedResponseError, ValueError) as exc:
            _LOGGER.warning("Sparse editor returned invalid output (%s); keeping drafts", exc)
            return SparseEditResult(tuple(draft_list), (), {}, failed=True)
        except Exception as exc:
            if is_resumable_provider_interrupt(exc):
                raise
            _LOGGER.warning("Sparse editor call failed (%s); keeping drafts", exc)
            return SparseEditResult(tuple(draft_list), (), {}, failed=True)

        raw_patches = data.get("patches") if isinstance(data, dict) else None
        if not isinstance(raw_patches, list):
            return SparseEditResult(tuple(draft_list), (), {}, failed=True)

        updated = list(draft_list)
        patched_indices: list[int] = []
        reasons: dict[int, str] = {}
        for patch in raw_patches:
            if not isinstance(patch, dict):
                continue
            idx = patch.get("index")
            text = patch.get("text")
            if not isinstance(idx, int) or isinstance(idx, bool) or not (0 <= idx < n):
                continue
            if not isinstance(text, str):
                continue
            # Never let a patch blank a non-empty draft.
            if draft_list[idx].strip() and not text.strip():
                continue
            # Preserve any protected-object markers present in the draft.
            draft_markers = _MARKER_RE.findall(draft_list[idx])
            if draft_markers and _MARKER_RE.findall(text) != draft_markers:
                continue
            if text == updated[idx]:
                continue
            updated[idx] = text
            patched_indices.append(idx)
            reasons[idx] = str(patch.get("reason") or "edit").strip()[:48]

        return SparseEditResult(
            targets=tuple(updated),
            patched_indices=tuple(sorted(set(patched_indices))),
            reasons=reasons,
            failed=False,
        )
