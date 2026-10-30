"""Global analysis agent using the strong tier.
Read sample chapters to produce a style guide and character references with gender and
voice. Name mappings and terminology extraction belong to the translation glossary workflow.
"""

from __future__ import annotations

from typing import Any

from ..glossary.store import TYPE_PERSON, GlossaryTerm
from ..i18n.metadata import normalize_gender
from .base import Agent


def _text(value: Any, default: str = "") -> str:
    """Normalize model fields to text; fall back for non-scalar values such as nested objects."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return default


class Analyzer(Agent):
    policy_phase = "analysis"

    def analyze(self, sample_text: str, chapter_digests: list[str] | None = None) -> dict[str, Any]:
        """Analyze samples and return type-checked style and character guidance."""
        system = self.render("analyzer_system", src=self.src, tgt=self.tgt)
        user = self.render("analyzer_user", src=self.src, tgt=self.tgt, sample=sample_text)
        if chapter_digests and self.language_policy.enabled("terminology.context"):
            user += "\n\n[Draft chapter digests: factual context, not naming authority]\n"
            user += "\n".join(f"[{i}] {digest}" for i, digest in enumerate(chapter_digests))
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

    def style_brief(self, analysis: dict[str, Any], terms: list[GlossaryTerm] | None = None) -> str:
        """Condense style and character guidance; name mappings belong to the glossary."""
        if not self.language_policy.enabled("terminology.context"):
            terms = None
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
