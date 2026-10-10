"""Extract glossary terms with an economical model and persist actual translations.
Extract proper names from source/target pairs after translation. GlossaryStore.upsert_term
records alternate translations as conflicts for human resolution.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Any

from ..agents import prompts
from ..agents.base import Agent
from ..config import Config
from ..i18n.policy.models import content_hash
from ..llm.base import LLMClient
from ..llm.json_parser import JsonParseError, parse_json_result
from ..storage.protocol import Storage
from .store import (
    TYPE_TERM,
    GlossaryOccurrenceMatcher,
    GlossaryStore,
    GlossaryTerm,
    source_matches_text,
)

_LOGGER = logging.getLogger(__name__)
_HISTORY_VERSION = 1


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
        system = self.render("glossary_extractor_system", src=self.src, tgt=self.tgt)
        user = self.render(
            "glossary_extractor_user",
            src=self.src,
            tgt=self.tgt,
            glossary=prompts.render_glossary(existing),
            source=source_text,
            target=target_text,
        )
        raw = self._ask_json(system, user, operation="glossary.extract", key="terms", default=[])
        terms: list[GlossaryTerm] = []
        for d in self.dict_items(raw, operation="glossary.extract", field="terms"):
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
        store: Storage | GlossaryStore,
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

    def _history_mappings(
        self, candidates: list[dict[str, Any]], *, store: Storage | None
    ) -> dict[str, str]:
        """Cache only supported mapping decisions, including explicit deferrals."""
        if not candidates:
            return {}
        system = self.render("glossary_history_system", src=self.src, tgt=self.tgt)
        user = self.render(
            "glossary_history_user",
            src=self.src,
            tgt=self.tgt,
            candidates_json=json.dumps(candidates, ensure_ascii=False, indent=2),
        )
        historical_targets = {
            candidate["source"]: candidate["first_occurrence"]["target"] for candidate in candidates
        }
        key = None
        if store is not None:
            identity = {
                "version": _HISTORY_VERSION,
                "source_sha256": store.load_manifest().get("source_sha256"),
                "policy": self.language_policy.fingerprint,
                "routing": self.config.llm.model_dump(mode="json"),
                "prompts": [system, user],
            }
            key = f"glossary/history/requests/{content_hash(identity)}.json"
            saved = store.read_artifact(key)
            if saved is not None:
                resolved = saved.get("resolved") if isinstance(saved, dict) else None
                if (
                    not isinstance(saved, dict)
                    or saved.get("schema_version") != _HISTORY_VERSION
                    or not isinstance(resolved, dict)
                    or any(
                        source not in historical_targets
                        or not isinstance(target, str)
                        or not target.strip()
                        or target != target.strip()
                        or not source_matches_text(target, historical_targets[source])
                        for source, target in resolved.items()
                    )
                ):
                    raise ValueError("Invalid glossary history alignment cache")
                return dict(resolved)
        text = self.client.complete(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            operation="glossary.align_history",
            json_mode=True,
        )
        try:
            parsed = parse_json_result(text)
        except JsonParseError:
            raise ValueError("Invalid glossary history alignment JSON response") from None
        if not parsed.safe_for_complete_payload:
            raise ValueError("Unsafe glossary history alignment JSON repair")
        raw = parsed.value.get("terms") if isinstance(parsed.value, dict) else parsed.value
        resolved: dict[str, str] = {}
        ambiguous: set[str] = set()
        for item in self.dict_items(raw, operation="glossary.align_history", field="terms"):
            source = _text(item.get("source"))
            target = _text(item.get("target"))
            if (
                source not in historical_targets
                or not target
                or not source_matches_text(target, historical_targets[source])
            ):
                continue
            if source in resolved and resolved[source] != target:
                ambiguous.add(source)
                del resolved[source]
            elif source not in ambiguous:
                resolved[source] = target
        if store is not None and key is not None:
            deferred = [
                index
                for index, candidate in enumerate(candidates)
                if candidate["source"] not in resolved
            ]
            if deferred:
                diagnostic_key = key.replace(
                    "glossary/history/requests/", "glossary/history/diagnostics/", 1
                )
                diagnostic = {
                    "schema_version": _HISTORY_VERSION,
                    "request_key": key,
                    "operation": "glossary.align_history",
                    "reason": "unresolved",
                    "candidate_count": len(candidates),
                    "aligned_count": len(candidates) - len(deferred),
                    "deferred_count": len(deferred),
                    "deferred_indices": deferred,
                }
                store.write_artifact(diagnostic_key, diagnostic)
                store.log_event(
                    "glossary_history_alignment_deferred",
                    diagnostic_key=diagnostic_key,
                    **diagnostic,
                )
                _LOGGER.warning(
                    "Deferred %d historical glossary mappings (aligned=%d); see %s",
                    len(deferred),
                    len(candidates) - len(deferred),
                    diagnostic_key,
                )
            store.write_artifact(key, {"schema_version": _HISTORY_VERSION, "resolved": resolved})
        return resolved

    def _align_with_first_occurrences(
        self,
        terms: list[GlossaryTerm],
        occurrences: dict[str, TranslatedSegmentEvidence],
        *,
        store: Storage | None = None,
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

        resolved = self._history_mappings(candidates, store=store)
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
        store: Storage | GlossaryStore,
        source_text: str,
        target_text: str,
        chapter: int,
        *,
        history: Iterable[TranslatedSegmentEvidence] = (),
        before: tuple[int, int] | None = None,
        source_corpus: str | None = None,
        admit: Callable[[GlossaryTerm, int | None], str] | None = None,
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
        terms, aligned, unresolved = self._align_with_first_occurrences(
            terms, occurrences, store=store if isinstance(store, Storage) else None
        )
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
            result = admit(t, chapter) if admit else store.upsert_term(t, chapter=chapter)
            summary[result] = summary.get(result, 0) + 1
        return summary
