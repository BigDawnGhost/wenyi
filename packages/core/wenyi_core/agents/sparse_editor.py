"""Sparse source-grounded semantic and craft editor with risk-routed reasoning.

Replaces full-batch source-blind polishing (which rewrites ~84% of paragraphs without
seeing the source and fixes zero meaning errors) with a source-aware audit that returns
JSON patches *only* for paragraphs with genuine fidelity or craft defects:
- Negation scope, conditional, causal, temporal, or agency inversions
- Wrong referents, pronoun/gender contradictions, or omitted clauses
- Literalized idioms, missed literary allusions, or stiff translationese calques
- Broken verse lineation, glossary drift, or inconsistent dialogue punctuation

A deterministic pre-screener (``batch_risk``) flags concrete anomalies and escalates
thinking effort along the provider's ladder only for high-risk batches.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..ingest.segmenter import is_verse_segment
from ..llm.json_parser import JsonParseError
from ..llm.preflight import fit_prompt_to_context_window
from ..llm.retrying import TruncatedResponseError, is_resumable_provider_interrupt
from ..models.glossary import GlossaryTerm
from . import prompts
from .base import Agent
from .batch_risk import assess_batch_risk

_LOGGER = logging.getLogger(__name__)
_MARKER_RE = re.compile(r"〘object:[A-Za-z0-9_.:-]+〙|⟦[^⟧]+⟧")
_WORD_RE = re.compile(r"[^\W\d_]+")

# Reasons that legitimately require heavy structural rewrites (never blocked by voice guard).
_HEAVY_REWRITE_REASON_TOKENS = (
    "omission",
    "verse",
    "poem",
    "lyric",
    "mistranslat",
    "logic",
    "negation",
    "conditional",
    "allusion",
    "idiom",
    "glossary",
    "pronoun",
    "gender",
    "referent",
    "agency",
    "fidelity",
    "meaning",
    "script",
    "residue",
    "gloss",
    "leak",
    "calque",
    "register",
    "honorific",
    "addressee",
    "temporal",
    "phantom",
    "entity",
    "toponym",
    "title",
    "consistency",
    "drift",
)


@dataclass(frozen=True)
class SparseEditResult:
    targets: tuple[str, ...]
    patched_indices: tuple[int, ...]
    reasons: dict[int, str]
    failed: bool = False
    thinking_boost: int = 0
    risk_reasons: tuple[str, ...] = ()


def _token_overlap_ratio(a: str, b: str) -> float:
    """Return Jaccard overlap of content tokens (used to guard against gratuitous style churn)."""
    ta = {w.casefold() for w in _WORD_RE.findall(a)}
    tb = {w.casefold() for w in _WORD_RE.findall(b)}
    if not ta or not tb:
        return 1.0
    return len(ta & tb) / max(len(ta), len(tb))


def _is_gratuitous_style_churn(draft: str, candidate: str, reason: str) -> bool:
    """Reject large non-semantic rewrites that flatten an already natural paragraph's voice."""
    low_reason = reason.casefold()
    if any(tok in low_reason for tok in _HEAVY_REWRITE_REASON_TOKENS):
        return False
    draft_words = _WORD_RE.findall(draft)
    if len(draft_words) < 20:
        return False
    # If the model changed >65% of words for a purely stylistic/unspecified tweak, keep the draft.
    return _token_overlap_ratio(draft, candidate) < 0.35


def _pair_specific_audit_note(src: str, tgt: str) -> str:
    src_base = (src or "").lower().split("-", 1)[0]
    tgt_base = (tgt or "").lower().split("-", 1)[0]
    if src_base == "zh" and tgt_base == "en":
        return (
            "Pair-specific checks (zh -> en): verify classical negation scope (莫…只管…), "
            "correlative conditionals (不A就B), delayed realization (良久…才), traditional time "
            "and numeric ranges (時辰 = two hours; 三五 = three to five), classical chronicle "
            "verb-vs-name parsing, royal/humble register (孤/臣/陛下/大人), kinship seniority, "
            "literary allusions (do not translate surface words literally), and embedded poems/lyrics."
        )
    if src_base == "ja" and tgt_base == "en":
        return (
            "Pair-specific checks (ja -> en): verify omitted subjects and speaker shifts, "
            "consistent honorific policy (-さん/-様/-殿/-君), natural rendering of katakana "
            "names/loanwords, zero leaked furigana markers (〘...〙), and lineated tanka/haiku."
        )
    if src_base == "en" and tgt_base == "zh":
        return (
            "Pair-specific checks (en -> zh): verify nested English clauses are rendered in crisp, "
            "idiomatic Chinese without stacked '的' translationese, check negation/irony and "
            "pronoun antecedents, enforce consistent Chinese transliteration and full-width "
            "punctuation, and preserve quoted epigraph/verse line breaks."
        )
    return ""


class SparseSemanticEditor(Agent):
    """Source-grounded sparse editor using the ``polish.body`` route with risk escalation."""

    def edit_batch(
        self,
        sources: Sequence[str],
        drafts: Sequence[str],
        *,
        glossary_terms: list[GlossaryTerm] | None = None,
        style: str = "",
        next_source: str = "",
        variants_by_source: Mapping[str, Sequence[str]] | None = None,
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
        pair_note = _pair_specific_audit_note(self.src, self.tgt)

        risk = assess_batch_risk(
            src_list,
            draft_list,
            source_lang=self.src,
            target_lang=self.tgt,
            glossary_terms=glossary_terms or (),
            variants_by_source=variants_by_source,
        )
        thinking_boost = 1 if risk.should_escalate else 0

        system = (
            f"You are a senior bilingual literary editor auditing a {self.src} -> {self.tgt} draft translation "
            "against the original source text. Unlike a full rewriter, you ONLY emit patches for paragraphs "
            "that contain a concrete defect. Leave accurate, vivid, natural paragraphs UNCHANGED — never flatten "
            "lively literary voice into bland prose.\n"
            "Check every paragraph against its source for:\n"
            "1. Logic, numbers & direction: flipped negation scope, reversed conditionals, reversed causality "
            "or timing, wrong arithmetic/ratios/time units, swapped subject/object/agent/addressee, dropped "
            "clauses or names, and singular/plural mismatches.\n"
            "2. Pronouns, register & naturalness: wrong pronoun antecedents, unmotivated gender contradictions "
            "or spoilers, broken speaker/addressee shifts, dropped court/humble/formal register, stiff literal "
            "calques, untranslated source-script residue, and unrequested parenthetical inline glosses.\n"
            "3. Idioms, allusions & terminology: literalized idioms or literary allusions, glossary mismatches "
            "against [Glossary] or [Style guide] conventions, inconsistent proper-noun spelling, and never "
            "substituting a familiar glossary place/person name for a different proper noun in the source.\n"
            "4. Verse vs prose craft & paragraph boundaries: standalone and embedded poems/lyrics MUST use "
            "explicit line breaks ('\\n', never inline prose or ' / '); conversely, NEVER split a continuous "
            "prose dialogue paragraph with '\\n' line breaks; and NEVER copy or anticipate text belonging to "
            "[Following source paragraph].\n"
            + (f"{pair_note}\n" if pair_note else "")
            + f"Language & formatting rules:\n{lang_rules}\n{punct_rule}\n"
            "Preserve every protected marker (e.g. 〘object:...〙 or ⟦...⟧) verbatim if present.\n"
            'Return ONLY JSON: {"patches": [{"index": 0, "reason": "short_tag", "text": "corrected paragraph"}]}. '
            'If every paragraph is faithful and natural, return {"patches": []}.'
            + protected_guidance
        )

        pairs_lines = []
        for idx, (src, tgt) in enumerate(zip(src_list, draft_list)):
            pairs_lines.append(f"[{idx}] SRC: {src}\n[{idx}] DRF: {tgt}")
        paired_block = "\n\n".join(pairs_lines)
        next_ref = prompts.render_source_reference(next_source)

        alert_blocks: list[str] = []
        unlineated_verse = [
            idx
            for idx, (src, tgt) in enumerate(zip(src_list, draft_list))
            if is_verse_segment(src) and "\n" not in tgt
        ]
        if len(unlineated_verse) >= 2:
            alert_blocks.append(
                f"[Verse formatting alert] Paragraphs {unlineated_verse} are rhymed verse couplets "
                "drafted without line breaks. Emit patches splitting each couplet onto two lines with '\\n'."
            )
        risk_alert_text = risk.format_prompt_alerts(max_items=6)
        if risk_alert_text:
            alert_blocks.append(risk_alert_text)
        extra_alerts = ("\n" + "\n".join(alert_blocks) + "\n") if alert_blocks else ""

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
                f"{extra_alerts}\n"
                f"[Following source paragraph] (reference only)\n{next_ref}\n\n"
                'Return ONLY JSON: {"patches": [{"index": <int>, "reason": "<tag>", "text": "<full revised paragraph>"}]}'
            )
            return system, user

        match_text = "\n".join(part for part in (*src_list, next_source) if part)
        try:
            sys_msg, user_msg, _eff, _shrunk = fit_prompt_to_context_window(
                config=self.config,
                operation="polish.body",
                terms=list(glossary_terms or ()),
                match_text=match_text,
                render_messages=_render,
            )
        except ValueError as exc:
            _LOGGER.warning(
                "Sparse editor prompt does not fit context window (%s); keeping drafts", exc
            )
            return SparseEditResult(
                tuple(draft_list),
                (),
                {},
                failed=True,
                thinking_boost=thinking_boost,
                risk_reasons=risk.reasons,
            )

        try:
            data, _raw = self._complete_json_turn(
                [
                    {"role": "system", "content": sys_msg},
                    {"role": "user", "content": user_msg},
                ],
                operation="polish.body",
                thinking_boost=thinking_boost,
            )
        except (JsonParseError, TruncatedResponseError, ValueError) as exc:
            _LOGGER.warning("Sparse editor returned invalid output (%s); keeping drafts", exc)
            return SparseEditResult(
                tuple(draft_list),
                (),
                {},
                failed=True,
                thinking_boost=thinking_boost,
                risk_reasons=risk.reasons,
            )
        except Exception as exc:
            if is_resumable_provider_interrupt(exc):
                raise
            _LOGGER.warning("Sparse editor call failed (%s); keeping drafts", exc)
            return SparseEditResult(
                tuple(draft_list),
                (),
                {},
                failed=True,
                thinking_boost=thinking_boost,
                risk_reasons=risk.reasons,
            )

        raw_patches = data.get("patches") if isinstance(data, dict) else None
        if not isinstance(raw_patches, list):
            return SparseEditResult(
                tuple(draft_list),
                (),
                {},
                failed=True,
                thinking_boost=thinking_boost,
                risk_reasons=risk.reasons,
            )

        updated = list(draft_list)
        patched_indices: list[int] = []
        reasons: dict[int, str] = {}
        flagged_indices = {a.index for a in risk.alerts}
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
            # Preserve any protected-object markers present in the draft and reject invented ones.
            if _MARKER_RE.findall(text) != _MARKER_RE.findall(draft_list[idx]):
                continue
            if text == updated[idx]:
                continue
            reason = str(patch.get("reason") or "edit").strip()[:48]
            if idx not in flagged_indices and _is_gratuitous_style_churn(
                draft_list[idx], text, reason
            ):
                continue
            updated[idx] = text
            patched_indices.append(idx)
            reasons[idx] = reason

        return SparseEditResult(
            targets=tuple(updated),
            patched_indices=tuple(sorted(set(patched_indices))),
            reasons=reasons,
            failed=False,
            thinking_boost=thinking_boost,
            risk_reasons=risk.reasons,
        )

    def adjudicate_drift(
        self,
        clusters: Sequence[dict[str, object]],
        *,
        style: str = "",
        conventions: Sequence[str] = (),
    ) -> dict[str, str]:
        """Pick one canonical target for genuine cross-chapter title/term splits; skip benign synonyms."""
        if not clusters:
            return {}
        rows: list[str] = []
        allowed: set[str] = set()
        for item in clusters[:8]:
            src = str(item.get("source") or "").strip()
            targets = item.get("targets") or ()
            if not src or not targets:
                continue
            allowed.add(src)
            tgt_desc = ", ".join(f"{t!r} ({c}x)" for t, c in targets)
            rows.append(f"- {src}: {tgt_desc}")
        if not rows:
            return {}
        conv_block = "\n".join(f"- {c}" for c in conventions)
        system = (
            f"You are a book consistency editor reviewing recurring {self.src} -> {self.tgt} terms "
            "whose translations split across parallel chapters. Decide ONLY for genuine recurring "
            "titles, ranks, offices, institutions, or signature recurring objects where the book "
            "should use ONE uniform rendering (including capitalization). "
            'For ordinary verbs, adverbs, or context-dependent words where variation is natural, return "".\n'
            'Return ONLY JSON: {"decisions": {"<source>": "<canonical_target_or_empty>", ...}}.'
        )
        user = (
            f"[Style guide]\n{style or '(none)'}\n\n"
            + (f"[Book conventions]\n{conv_block}\n\n" if conv_block else "")
            + "[Cross-chapter split candidates]\n"
            + "\n".join(rows)
            + '\n\nReturn ONLY JSON: {"decisions": {...}}'
        )
        try:
            data, _raw = self._complete_json_turn(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                operation="glossary.extract",
            )
        except Exception as exc:  # noqa: BLE001 - optional post-pass; never abort completed book.
            if is_resumable_provider_interrupt(exc):
                raise
            _LOGGER.warning("Drift adjudication call failed (%s); skipping drift patches", exc)
            return {}
        raw_decisions = data.get("decisions") if isinstance(data, dict) else None
        if not isinstance(raw_decisions, dict):
            return {}
        decisions: dict[str, str] = {}
        for src, val in raw_decisions.items():
            if src in allowed and isinstance(val, str) and val.strip():
                decisions[src] = val.strip()
        return decisions
