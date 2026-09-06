"""Extract glossary terms with an economical model and persist actual translations.
Extract proper names from source/target pairs after translation. GlossaryStore.upsert_term
records alternate translations as conflicts for human resolution.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, replace

from ..agents import prompts
from ..agents.base import Agent
from ..config import Config
from ..llm.base import LLMClient
from .store import (
    TYPE_TERM,
    GlossaryOccurrenceMatcher,
    GlossaryStore,
    GlossaryTerm,
    source_matches_text,
)


def _text(value: object, default: str = "") -> str:
    """Normalize scalar model fields to strings."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return default


@dataclass(frozen=True)
class TranslatedSegmentEvidence:
    """A translated paragraph and its book position for tracing a new term's first translation."""

    chapter: int
    segment: int
    source: str
    target: str


class GlossaryExtractor(Agent):
    def __init__(self, client: LLMClient, config: Config):
        super().__init__(client, config)
        self._recurrence_corpus: str | None = None
        self._recurrence_matcher: GlossaryOccurrenceMatcher | None = None
        self._recurrence_cache: dict[tuple[str, str, tuple[str, ...]], bool] = {}

    def _recurring_existing_terms(
        self,
        terms: list[GlossaryTerm],
        source_corpus: str,
    ) -> list[GlossaryTerm]:
        """Return existing terms occurring at least twice in the book and cache per-term
        matches.
        """
        if source_corpus is not self._recurrence_corpus:
            self._recurrence_corpus = source_corpus
            self._recurrence_matcher = GlossaryOccurrenceMatcher(source_corpus)
            self._recurrence_cache.clear()

        assert self._recurrence_matcher is not None
        missing: list[GlossaryTerm] = []
        for term in terms:
            signature = (term.source, term.type, tuple(term.aliases))
            if signature not in self._recurrence_cache:
                missing.append(term)

        if missing:
            matched = {
                (term.source, term.type, tuple(term.aliases))
                for term in self._recurrence_matcher.recurring_terms(missing)
            }
            for term in missing:
                signature = (term.source, term.type, tuple(term.aliases))
                self._recurrence_cache[signature] = signature in matched

        return [
            term
            for term in terms
            if self._recurrence_cache[(term.source, term.type, tuple(term.aliases))]
        ]

    def extract(
        self, source_text: str, target_text: str, existing: list[GlossaryTerm]
    ) -> list[GlossaryTerm]:
        """Extract valid terms from source/target pairs and normalize model field types."""
        system = prompts.render("glossary_extractor_system", src=self.src, tgt=self.tgt)
        user = prompts.render(
            "glossary_extractor_user",
            src=self.src,
            tgt=self.tgt,
            glossary=prompts.render_glossary(existing),
            source=source_text,
            target=target_text,
        )
        raw = self._ask_json(system, user, tier="fast", key="terms", default=[])
        terms: list[GlossaryTerm] = []
        for d in self.dict_items(raw):
            source = _text(d.get("source"))
            target = _text(d.get("target"))
            if not source or not target:
                continue
            raw_aliases = d.get("aliases")
            aliases = raw_aliases if isinstance(raw_aliases, list) else []
            gender = _text(d.get("gender"))
            terms.append(
                GlossaryTerm(
                    source=source,
                    target=target,
                    reading=_text(d.get("reading")),
                    type=_text(d.get("type"), TYPE_TERM),
                    gender=gender,
                    aliases=[alias for a in aliases if (alias := _text(a))],
                    note=_text(d.get("note")),
                )
            )
        return terms

    @staticmethod
    def _first_occurrences(
        terms: list[GlossaryTerm],
        store: GlossaryStore,
        history: Iterable[TranslatedSegmentEvidence],
        before: tuple[int, int],
    ) -> dict[str, TranslatedSegmentEvidence]:
        """Find the first translated paragraph before the given position for terms not yet
        stored.
        """
        pending = {term.source for term in terms if store.get_term(term.source) is None}
        if not pending:
            return {}

        first: dict[str, TranslatedSegmentEvidence] = {}
        ordered_history = sorted(history, key=lambda item: (item.chapter, item.segment))
        for evidence in ordered_history:
            if (evidence.chapter, evidence.segment) >= before:
                continue
            for source in pending:
                if source in first:
                    continue
                if source_matches_text(source, evidence.source):
                    first[source] = evidence
            if len(first) == len(pending):
                break
        return first

    def _align_with_first_occurrences(
        self,
        terms: list[GlossaryTerm],
        occurrences: dict[str, TranslatedSegmentEvidence],
    ) -> tuple[list[GlossaryTerm], int, int]:
        """Align candidates with their first translations; defer terms whose historical mapping
        is uncertain.
        """
        if not occurrences:
            return terms, 0, 0

        candidates = []
        for term in terms:
            evidence = occurrences.get(term.source)
            if evidence is None:
                continue
            candidates.append(
                {
                    "source": term.source,
                    "proposed_target": term.target,
                    "first_occurrence": {
                        "chapter": evidence.chapter,
                        "segment": evidence.segment,
                        "source": evidence.source,
                        "target": evidence.target,
                    },
                }
            )

        system = prompts.render("glossary_history_system", src=self.src, tgt=self.tgt)
        user = prompts.render(
            "glossary_history_user",
            src=self.src,
            tgt=self.tgt,
            candidates_json=json.dumps(candidates, ensure_ascii=False, indent=2),
        )
        raw = self._ask_json(system, user, tier="fast", key="terms", default=[])
        resolved = {
            source: target
            for item in self.dict_items(raw)
            if (source := _text(item.get("source"))) in occurrences
            and (target := _text(item.get("target")))
        }

        aligned: list[GlossaryTerm] = []
        unresolved = 0
        for term in terms:
            if term.source not in occurrences:
                aligned.append(term)
                continue
            target = resolved.get(term.source)
            if not target:
                unresolved += 1
                continue
            aligned.append(replace(term, target=target))
        return aligned, len(resolved), unresolved

    def extract_and_store(
        self,
        store: GlossaryStore,
        source_text: str,
        target_text: str,
        chapter: int,
        *,
        history: Iterable[TranslatedSegmentEvidence] = (),
        before: tuple[int, int] | None = None,
        source_corpus: str | None = None,
    ) -> dict[str, int]:
        """Extract and store terms, preferring the translation at their first historical
        occurrence.
        history contains translated evidence only. If a new term appears before the supplied
        position, align target against its first source/target pair. Defer uncertain
        mappings instead of locking a later candidate into the glossary and contaminating
        subsequent text.
        With source_corpus, inject only existing terms occurring at least twice in the
        source. Low-frequency terms remain stored but do not repeatedly consume extraction
        context.
        """
        all_existing = store.all_terms()
        existing = (
            self._recurring_existing_terms(all_existing, source_corpus)
            if source_corpus is not None
            else all_existing
        )
        terms = self.extract(source_text, target_text, existing)
        occurrences = (
            self._first_occurrences(terms, store, history, before) if before is not None else {}
        )
        terms, aligned, unresolved = self._align_with_first_occurrences(terms, occurrences)
        summary = {
            "inserted": 0,
            "conflict": 0,
            "unchanged": 0,
            "history_matched": len(occurrences),
            "history_aligned": aligned,
            "history_unresolved": unresolved,
        }
        for t in terms:
            evidence = occurrences.get(t.source)
            t.first_chapter = evidence.chapter if evidence is not None else chapter
            result = store.upsert_term(t, chapter=chapter)
            summary[result] = summary.get(result, 0) + 1
        return summary
