"""Global analysis agent using the style analysis route.
Read sample chapters to produce a style guide and character references with gender and
voice. Name mappings and terminology extraction belong to the translation glossary workflow.
"""

from __future__ import annotations

from typing import Any

from ..i18n.metadata import normalize_gender
from ..ingest.tokens import count_tokens, split_balanced_tokens
from ..llm.preflight import output_reserve
from ..llm.routing import model_route, resolve_routes
from ..models.glossary import TYPE_PERSON, GlossaryTerm
from .base import Agent

_DIGESTS_HEADER = "\n\n[Draft chapter digests: factual context, not naming authority]\n"


def _text(value: Any, default: str = "") -> str:
    """Normalize model fields to text; fall back for non-scalar values such as nested objects."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return default


def _cap_digest(digest: str, cap: int) -> str:
    """Truncate one oversized digest to ``cap`` tokens, keeping its opening words."""
    if cap <= 0:
        return ""
    if count_tokens(digest) <= cap:
        return digest
    try:
        return split_balanced_tokens(digest, cap)[0]
    except ValueError:
        # A budget below a single character's tokens; halve down to "" so the loop always ends.
        trimmed = digest
        while trimmed and count_tokens(trimmed) > cap:
            trimmed = trimmed[: len(trimmed) // 2]
        return trimmed


def _spread_line_indices(costs: list[int], budget: int) -> list[int]:
    """Pick strictly ascending line indices, anchored at both ends, within ``budget``."""
    total = len(costs)
    if total == 0:
        return []
    if total == 1:
        return [0] if costs[0] <= budget else []
    # The "+ 1" covers the newline between the opening and closing anchor lines.
    anchor_cost = costs[0] + costs[-1] + 1
    if anchor_cost > budget:
        return [0] if costs[0] <= budget else []
    for stride in range(1, total - 1):
        picks = list(range(stride, total - 1, stride))
        if anchor_cost + sum(costs[i] for i in picks) + len(picks) <= budget:
            return [0, *picks, total - 1]
    return [0, total - 1]


def _representative_digest_block(digests: list[str], budget: int) -> str:
    """Sample oversized digest lists evenly across the book arc within ``budget`` tokens.

    Selection is deterministic: original ``[i]`` labels and strictly ascending order are
    preserved, the opening and closing non-empty chapters are always kept when they fit,
    and a single digest that alone exceeds the budget is truncated instead of dropped.
    """
    if budget <= 0:
        return ""
    # One chapter must never consume the whole block, so cap each digest to a share of it.
    per_digest = max(1, budget // 4)
    lines: list[tuple[int, str]] = []
    for index, digest in enumerate(digests):
        stripped = digest.strip()
        if not stripped:
            continue
        capped = _cap_digest(stripped, per_digest)
        if capped:
            lines.append((index, f"[{index}] {capped}"))
    if not lines:
        return ""
    costs = [count_tokens(line) for _, line in lines]
    chosen = _spread_line_indices(costs, budget)
    while chosen:
        block = "\n".join(lines[i][1] for i in chosen)
        if count_tokens(block) <= budget:
            return block
        # Joined tokenization can merge across newlines; drop the latest middle sample first.
        chosen = chosen[:-2] + chosen[-1:] if len(chosen) > 2 else chosen[:-1]
    return ""


class Analyzer(Agent):
    policy_phase = "analysis"

    def analyze(self, sample_text: str, chapter_digests: list[str] | None = None) -> dict[str, Any]:
        """Analyze samples and return type-checked style and character guidance."""
        system = self.render("analyzer_system", src=self.src, tgt=self.tgt)
        user = self.render("analyzer_user", src=self.src, tgt=self.tgt, sample=sample_text)
        if chapter_digests:
            block = self._bounded_digest_block(system, user, chapter_digests)
            if block:
                user += _DIGESTS_HEADER + block
        # No default: propagate analysis failures for the caller to handle, including preparation failures.
        data = self._ask_json(system, user, operation="analysis.style")
        if not isinstance(data, dict):
            data = {}
        # Accept a list of prose bullets as well as the requested string. Never stringify objects.
        if isinstance(data.get("style_guide"), list):
            data["style_guide"] = "\n".join(
                item.strip()
                for item in data["style_guide"]
                if isinstance(item, str) and item.strip()
            )
        for key in (
            "genre",
            "tone",
            "style_guide",
            "narration",
            "pacing",
            "register",
            "dialogue_style",
            "rhetoric",
        ):
            data[key] = _text(data.get(key))
        data["characters"] = self.dict_items(
            data.get("characters"), operation="analysis.style", field="characters"
        )
        # Ignore unsolicited glossary candidates, including fields from the former schema.
        data.pop("terms", None)
        for character in data["characters"]:
            character["gender"] = normalize_gender(_text(character.get("gender")))
            character.pop("target", None)
            character.pop("reading", None)
        return data

    def _bounded_digest_block(self, system: str, user: str, digests: list[str]) -> str:
        """Render chapter digests within the token budget, byte-identical when they fit."""
        exact = "\n".join(f"[{i}] {digest}" for i, digest in enumerate(digests))
        budget = self._digests_token_budget(system, user)
        if count_tokens(exact) <= budget:
            return exact
        return _representative_digest_block(digests, budget)

    def _digests_token_budget(self, system: str, user: str) -> int:
        """Bound the digests block by the synopsis cap and the analysis context window."""
        budget = self.config.pipeline.synopsis_reduce_tokens
        route = resolve_routes(self.config.llm).get("analysis.style")
        if route is None:
            return budget
        # Enforce the tightest input budget across primary and configured fallback models,
        # matching the preflight checks the translation routes already run.
        candidates = [route]
        for profile in route.fallbacks:
            candidates.append(
                model_route(self.config.llm, "analysis.style", profile, origin="fallback")
            )
        bounded = [r for r in candidates if r.context_window is not None]
        if not bounded:
            return budget
        available = min(
            r.context_window - output_reserve(r.max_output_tokens, r.context_window)
            for r in bounded
        )
        # The rendered prompt already carries the system message, sample, template, and header.
        return min(
            budget,
            available - count_tokens(system) - count_tokens(user) - count_tokens(_DIGESTS_HEADER),
        )

    def style_brief(self, analysis: dict[str, Any], terms: list[GlossaryTerm] | None = None) -> str:
        """Condense style and character guidance; name mappings belong to the glossary."""
        lines = []
        if analysis.get("genre"):
            lines.append(f"Genre: {analysis['genre']}")
        if analysis.get("tone"):
            lines.append(f"Tone: {analysis['tone']}")
        if analysis.get("style_guide"):
            lines.append(f"Style guide: {analysis['style_guide']}")
        # Include only style dimensions supported by the model's analysis.
        for key, tag in (
            ("narration", "Narration"),
            ("pacing", "Pacing"),
            ("register", "Register"),
            ("dialogue_style", "Dialogue style"),
            ("rhetoric", "Rhetoric"),
        ):
            if analysis.get(key):
                lines.append(f"{tag}: {analysis[key]}")
        chars = (
            [
                {
                    "source": term.source,
                    "gender": term.gender,
                    # Notes belong in the bounded, source-scoped glossary evidence,
                    # never in the global style prefix.
                }
                for term in terms
                if term.type == TYPE_PERSON
            ]
            if terms is not None
            else self.dict_items(
                analysis.get("characters"), operation="analysis.style", field="characters"
            )
        )
        if chars:
            lines.append("Characters: ")
            for c in chars:
                gender = normalize_gender(_text(c.get("gender")))
                g = f", {gender}" if gender else ""
                note = f", {c.get('note')}" if c.get("note") else ""
                lines.append(f"  - {c.get('source', '')}{g}{note}")
        return "\n".join(lines)
