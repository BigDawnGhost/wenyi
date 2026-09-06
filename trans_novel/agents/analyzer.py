"""Global analysis agent using the strong tier.
Read sample chapters to produce a style guide, character reference with gender and voice,
and initial term candidates. Seed characters and terms into the glossary as a consistent
reference for the book.
"""

from __future__ import annotations

from typing import Any

from ..glossary.store import TYPE_PERSON, GlossaryStore, GlossaryTerm
from ..i18n.metadata import normalize_gender, normalize_term_type
from . import prompts
from .base import Agent


def _text(value: Any, default: str = "") -> str:
    """Normalize model fields to text; fall back for non-scalar values such as nested objects."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return default


class Analyzer(Agent):
    def analyze(self, sample_text: str) -> dict[str, Any]:
        """Analyze samples and return type-checked style, character and terminology data."""
        system = prompts.render("analyzer_system", src=self.src, tgt=self.tgt)
        user = prompts.render("analyzer_user", src=self.src, tgt=self.tgt, sample=sample_text)
        # No default: propagate analysis failures for the caller to handle, including preparation failures.
        data = self._ask_json(system, user, tier="strong")
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
        data["characters"] = self.dict_items(data.get("characters"))
        data["terms"] = self.dict_items(data.get("terms"))
        for character in data["characters"]:
            character["gender"] = normalize_gender(_text(character.get("gender")))
        for term in data["terms"]:
            term["type"] = normalize_term_type(_text(term.get("type")))
        return data

    def seed_glossary(self, store: GlossaryStore, analysis: dict[str, Any]) -> int:
        """Seed analyzed characters and terms into the glossary; return the entry count."""
        count = 0
        for ch in self.dict_items(analysis.get("characters")):
            source = _text(ch.get("source"))
            target = _text(ch.get("target"))
            if not source or not target:
                continue
            store.upsert_term(
                GlossaryTerm(
                    source=source,
                    target=target,
                    reading=_text(ch.get("reading")),
                    type=TYPE_PERSON,
                    gender=_text(ch.get("gender")),
                    note=_text(ch.get("note")),
                    first_chapter=0,
                ),
                chapter=0,
            )
            count += 1
        for tm in self.dict_items(analysis.get("terms")):
            source = _text(tm.get("source"))
            target = _text(tm.get("target"))
            if not source or not target:
                continue
            store.upsert_term(
                GlossaryTerm(
                    source=source,
                    target=target,
                    reading=_text(tm.get("reading")),
                    type=normalize_term_type(_text(tm.get("type"))),
                    note=_text(tm.get("note")),
                    first_chapter=0,
                ),
                chapter=0,
            )
            count += 1
        return count

    def style_brief(self, analysis: dict[str, Any]) -> str:
        """Condense analysis into a style and character brief for the translator."""
        lines = []
        if analysis.get("genre"):
            lines.append(f"Genre: {analysis['genre']}")
        if analysis.get("tone"):
            lines.append(f"Tone: {analysis['tone']}")
        if analysis.get("style_guide"):
            lines.append(f"Style guide: {analysis['style_guide']}")
        # Skip missing detailed style fields for compatibility with older analysis.json files.
        for key, tag in (
            ("narration", "Narration"),
            ("pacing", "Pacing"),
            ("register", "Register"),
            ("dialogue_style", "Dialogue style"),
            ("rhetoric", "Rhetoric"),
        ):
            if analysis.get(key):
                lines.append(f"{tag}: {analysis[key]}")
        chars = self.dict_items(analysis.get("characters"))
        if chars:
            lines.append("Characters: ")
            for c in chars:
                gender = normalize_gender(_text(c.get("gender")))
                g = f", {gender}" if gender else ""
                note = f", {c.get('note')}" if c.get("note") else ""
                lines.append(
                    f"  - {c.get('target') or c.get('source', '')} ({c.get('source', '')}{g}{note})"
                )
        return "\n".join(lines)
