"""Extract glossary terms with an economical model and persist actual translations.
Extract proper names from source/target pairs after translation. GlossaryStore.upsert_term
records alternate translations as conflicts for human resolution.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Any

from ..config import Config
from ..glossary.prompt_scope import prompt_terms
from ..glossary.store import (
    TYPE_NAVIGATION,
    TYPE_TERM,
    GlossaryOccurrenceMatcher,
    GlossaryStore,
    GlossaryTerm,
    equivalent_targets,
    normalize_match_text,
    source_matches_text,
    source_occurrence_spans,
    term_match_sources,
)
from ..i18n.languages import (
    glossary_context_dependent_sources,
    glossary_generic_target_words,
    glossary_single_character_names_only,
)
from ..i18n.policy.models import content_hash
from ..llm.base import LLMClient
from ..llm.json_parser import JsonParseError, parse_json_result
from ..llm.preflight import fit_prompt_to_context_window
from ..models.glossary import SOURCE_ONLY_TYPES
from ..storage.protocol import Storage
from . import prompts
from .base import Agent

_LOGGER = logging.getLogger(__name__)
_HISTORY_VERSION = 1
_POSSESSIVE_KIN = re.compile(
    r"(?:my|your|his|her|its|our|their)\s+(?:(?:elder|older|younger|little|big)\s+)?"
    r"(?:brothers?|sisters?|siblings?|uncles?|aunts?|cousins?|parents?|mothers?|fathers?|sons?|daughters?)",
    re.IGNORECASE,
)
_EN_CONTRADICTION_PREFIX = (
    r"(?:distinct\s+from|different\s+from|not\s+to\s+be\s+confused\s+with|"
    r"do\s+not\s+confuse\s+with|don['’]t\s+confuse\s+with|unlike|"
    r"not\s+the\s+same\s+as|rather\s+than|instead\s+of|"
    r"do\s+not\s+(?:use|translate\s+as|render\s+as)|"
    r"don['’]t\s+(?:use|translate\s+as|render\s+as)|"
    r"never\s+(?:use|translate\s+as|render\s+as)|"
    r"avoid|not)"
)
_EN_CONTRAST_PREDICATES = re.compile(r"^(?i:distinct\s+from|different\s+from|unlike)\b")
_EN_NEGATED_CONTRAST = re.compile(r"(?i:(?:\b(?:not|no|never)|n['’]t)\s+)$")
_EN_TARGET_BOUNDARY = (
    r"(?:$|\s*[,.;:!?)\]}—–/]|"
    r"\s+(?:when|while|because|since|although|though|but|however|instead)\b)"
)
_ZH_CONTRADICTION_PREFIX = (
    r"(?:区别于|不同于|勿译为|不要译为|不可译为|勿翻为|不要翻为|并非|(?<!是)不是|而非)"
)
_ZH_CONTRAST_PREDICATES = re.compile(r"^(?:区别于|不同于)")
_ZH_NEGATED_CONTRAST = re.compile(r"(?:并非|不是|毫不|绝不|并无|不)$")
_ZH_TARGET_BOUNDARY = r"(?:$|\s*[，。、；：！？,.!?;:)）\]】—–/]|(?:而|但|却))"


def _quoted_target_pattern(escaped: str) -> str:
    body = f"(?i:{escaped})"
    return rf'(?:"{body}"|\'{body}\'|“{body}”|‘{body}’|`{body}`|「{body}」|『{body}』|《{body}》)'


def _note_contradicts_target(target: str, note: str) -> bool:
    """Return True when the note explicitly distinguishes the entry from its own target."""
    cleaned_target = target.strip()
    cleaned_note = note.strip()
    if not cleaned_target or not cleaned_note:
        return False
    escaped = re.escape(cleaned_target)
    quoted = _quoted_target_pattern(escaped)
    en_pattern = re.compile(
        rf"\b(?i:{_EN_CONTRADICTION_PREFIX})\s+(?:(?i:the|a|an)\s+)?"
        rf"(?:{quoted}(?!['’]s\b)|(?i:{escaped})(?={_EN_TARGET_BOUNDARY}))"
    )
    for match in en_pattern.finditer(cleaned_note):
        if _EN_CONTRAST_PREDICATES.match(match.group(0)) and _EN_NEGATED_CONTRAST.search(
            cleaned_note[: match.start()]
        ):
            continue
        return True
    zh_pattern = re.compile(
        rf"{_ZH_CONTRADICTION_PREFIX}\s*(?:{quoted}|(?i:{escaped})(?={_ZH_TARGET_BOUNDARY}))"
    )
    for match in zh_pattern.finditer(cleaned_note):
        if _ZH_CONTRAST_PREDICATES.match(match.group(0)) and _ZH_NEGATED_CONTRAST.search(
            cleaned_note[: match.start()]
        ):
            continue
        return True
    return False


def _is_shadowed_or_unbounded_source(
    term: GlossaryTerm,
    normalized_text: str,
    reference_keys_by_source: Iterable[tuple[str, tuple[str, ...]]],
) -> bool:
    """Reject a source key that only appears inside a longer word or another longer term."""
    normalized_source = normalize_match_text(term.source).strip()
    if not normalized_source or not normalized_text:
        return False
    if normalized_source not in normalized_text:
        return False

    source_spans = source_occurrence_spans(normalized_source, normalized_text)
    if not source_spans:
        # For space-delimited scripts, require a token boundary rather than matching inside a
        # longer word (e.g. "Ann" inside "Anna"), unless one of this term's own matching aliases
        # appears on a token boundary.
        alias_keys = [
            normalized
            for alias in term_match_sources(term)
            if (normalized := normalize_match_text(alias).strip())
            and normalized != normalized_source
        ]
        alias_spans = [
            span for alias in alias_keys for span in source_occurrence_spans(alias, normalized_text)
        ]
        if not alias_spans:
            return True
        source_spans = alias_spans
        candidate_keys = set(alias_keys)
    else:
        candidate_keys = {normalized_source}

    longer_keys = {
        key
        for other_source, other_keys in reference_keys_by_source
        if other_source != normalized_source
        for key in other_keys
        if any(len(key) > len(candidate) and candidate in key for candidate in candidate_keys)
    }
    if not longer_keys:
        return False

    longer_spans = [
        span for key in longer_keys for span in source_occurrence_spans(key, normalized_text)
    ]
    if not longer_spans:
        return False

    return all(
        any(l_start <= s_start and s_end <= l_end for l_start, l_end in longer_spans)
        for s_start, s_end in source_spans
    )


def _filter_shadowed_sources(
    terms: list[GlossaryTerm],
    source_text: str,
    reference_terms: Iterable[GlossaryTerm],
) -> list[GlossaryTerm]:
    """Drop terms whose source only appears inside a longer word or another longer term."""
    if not terms:
        return terms
    reference_keys_by_source = [
        (
            normalize_match_text(item.source).strip(),
            tuple(
                normalized
                for raw_key in term_match_sources(item)
                if (normalized := normalize_match_text(raw_key).strip())
            ),
        )
        for item in reference_terms
    ]
    normalized_source_text = normalize_match_text(source_text)
    filtered: list[GlossaryTerm] = []
    shadowed = 0
    for term in terms:
        if _is_shadowed_or_unbounded_source(term, normalized_source_text, reference_keys_by_source):
            shadowed += 1
            continue
        filtered.append(term)
    if shadowed:
        _LOGGER.warning(
            "Rejected %d glossary extraction(s) shadowed by a longer term or word boundary "
            "(operation=glossary.extract)",
            shadowed,
        )
    return filtered


_NAME_TYPES = frozenset({"person", "place", "organization"})


def _has_uppercase(text: str) -> bool:
    return any(char.isupper() for char in text)


def _is_lowercase_only(text: str) -> bool:
    """Return True for cased-script wording without capitals, such as "lay across the river"."""
    return any(char.islower() for char in text) and not _has_uppercase(text)


def _is_single_character_source(source: str) -> bool:
    """Return True for a one-character source such as Chinese 孤 or 胜.

    One character is too ambiguous to locate by substring: 孤 also occurs in 孤舟 and
    胜 in 胜利, so such sources are neither aligned with history nor kept unless named.
    """
    return len(normalize_match_text(source).strip()) == 1


def _filter_context_dependent_sources(
    terms: list[GlossaryTerm],
    context_sources: frozenset[str],
    *,
    single_character_names_only: bool,
) -> list[GlossaryTerm]:
    """Drop self-references, forms of address and, where the source language profile asks,
    one-character common words.

    Their rendering depends on speaker, listener or the surrounding word, so a single
    book-wide mapping is wrong more often than it helps. Such forms are also removed from
    aliases, where they would match every later form of address. One-character names are
    kept: a person, place or organization, or a target with a capital letter (晋 "Jin").
    """
    filtered: list[GlossaryTerm] = []
    dropped = 0
    for term in terms:
        if term.source.strip() in context_sources or (
            single_character_names_only
            and _is_single_character_source(term.source)
            and term.type not in _NAME_TYPES
            and not _has_uppercase(term.target)
        ):
            dropped += 1
            continue
        aliases = [alias for alias in term.aliases if alias.strip() not in context_sources]
        filtered.append(term if aliases == term.aliases else replace(term, aliases=aliases))
    if dropped:
        _LOGGER.info(
            "Skipped %d context-dependent glossary extraction(s) (operation=glossary.extract)",
            dropped,
        )
    return filtered


_WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*")


def _is_generic_target(target: str, generic_words: frozenset[str]) -> bool:
    """Return True when every word of a target is a pronoun or a title word.

    "I", "your son", "this commoner" and "Your Excellency" name nobody in particular, while
    "Lord Jiang" or "Prince of Yong" keep a name and stay. Cased and spaced scripts only:
    a target without such words, such as Chinese, is never generic.
    """
    words = _WORD.findall(normalize_match_text(target))
    return bool(words) and all(word.replace("’", "'") in generic_words for word in words)


def _filter_generic_targets(
    terms: list[GlossaryTerm], generic_words: frozenset[str]
) -> list[GlossaryTerm]:
    """Drop extractions whose target is only pronouns and generic titles (target profile)."""
    if not generic_words:
        return terms

    def generic(term: GlossaryTerm) -> bool:
        words = _WORD.findall(term.target)
        # A one-word capitalized name such as "Grace" or "King" is a person, not a title.
        if term.type in _NAME_TYPES and len(words) == 1 and len(words[0]) > 1:
            if words[0][0].isupper():
                return False
        return _is_generic_target(term.target, generic_words)

    kept = [term for term in terms if not generic(term)]
    if len(kept) < len(terms):
        _LOGGER.info(
            "Skipped %d glossary extraction(s) with a pronoun or bare-title target "
            "(operation=glossary.extract)",
            len(terms) - len(kept),
        )
    return kept


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


@dataclass(frozen=True)
class ExtractionRequest:
    """One extraction request, read from the glossary before its model call."""

    source_text: str
    target_text: str
    # Entries shown to the model, and the whole glossary they were chosen from.
    existing: tuple[GlossaryTerm, ...]
    all_existing: tuple[GlossaryTerm, ...]


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

        assert self._recurrence_matcher is not None, "recurrence matcher was not initialised"
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

    def _drop_single_occurrence_new_terms(
        self,
        terms: list[GlossaryTerm],
        store: Storage | GlossaryStore,
        source_corpus: str,
    ) -> list[GlossaryTerm]:
        """Drop new terms whose source and aliases occur only once in the whole book.

        A term that never recurs cannot be translated inconsistently, so storing it only
        enlarges every later prompt. Repeats of stored terms still pass so the store can
        merge aliases or record conflicts.
        """
        new_indices = [
            index for index, term in enumerate(terms) if store.get_term(term.source) is None
        ]
        if not new_indices:
            return terms
        recurring = self._recurring_existing_terms(
            [terms[index] for index in new_indices], source_corpus
        )
        recurring_keys = {(term.source, term.type, tuple(term.aliases)) for term in recurring}
        single = {
            index
            for index in new_indices
            if (terms[index].source, terms[index].type, tuple(terms[index].aliases))
            not in recurring_keys
        }
        if not single:
            return terms
        _LOGGER.info(
            "Skipped %d glossary extraction(s) that occur once in the book "
            "(operation=glossary.extract)",
            len(single),
        )
        return [term for index, term in enumerate(terms) if index not in single]

    def extract(
        self, source_text: str, target_text: str, existing: list[GlossaryTerm]
    ) -> list[GlossaryTerm]:
        """Extract valid terms from source/target pairs and normalize model field types."""
        system_template = self.render("glossary_extractor_system", src=self.src, tgt=self.tgt)

        def _render(terms_for_prompt: list[GlossaryTerm]) -> tuple[str, str]:
            return (
                system_template,
                self.render(
                    "glossary_extractor_user",
                    src=self.src,
                    tgt=self.tgt,
                    glossary=prompts.render_glossary(terms_for_prompt, source_lang=self.src),
                    source=source_text,
                    target=target_text,
                ),
            )

        system, user, _effective_existing, _shrunk = fit_prompt_to_context_window(
            config=self.config,
            operation="glossary.extract",
            terms=existing,
            match_text=source_text,
            render_messages=_render,
        )

        def local_reference(spelling: str) -> bool:
            return self.src.split("-")[0] == "en" and bool(_POSSESSIVE_KIN.fullmatch(spelling))

        raw = self._ask_json(system, user, operation="glossary.extract", key="terms", default=[])
        terms: list[GlossaryTerm] = []
        for d in self.dict_items(raw, operation="glossary.extract", field="terms"):
            source = _text(d.get("source"))
            target = _text(d.get("target"))
            if not source or not target:
                continue
            term_type = _text(d.get("type"), TYPE_TERM)
            if d.get("scope", "stable") != "stable" or (
                term_type in SOURCE_ONLY_TYPES and d.get("scope") != "stable"
            ):
                continue
            if local_reference(source):
                continue
            raw_aliases = d.get("aliases")
            aliases = raw_aliases if isinstance(raw_aliases, list) else []
            gender = _text(d.get("gender"))
            terms.append(
                GlossaryTerm(
                    source=source,
                    target=target,
                    reading=_text(d.get("reading")) if self.src == "ja" else "",
                    type=term_type,
                    gender=gender,
                    aliases=[
                        alias for a in aliases if (alias := _text(a)) and not local_reference(alias)
                    ],
                    note=_text(d.get("note")),
                )
            )
        non_contradictory: list[GlossaryTerm] = []
        contradicted = 0
        for term in terms:
            if _note_contradicts_target(term.target, term.note):
                contradicted += 1
                continue
            non_contradictory.append(term)
        if contradicted:
            _LOGGER.warning(
                "Rejected %d glossary extraction(s) with self-contradictory notes "
                "(operation=glossary.extract)",
                contradicted,
            )
        bounded = _filter_shadowed_sources(non_contradictory, source_text, (*terms, *existing))
        addressed = _filter_context_dependent_sources(
            bounded,
            glossary_context_dependent_sources(self.src),
            single_character_names_only=glossary_single_character_names_only(self.src),
        )
        return _filter_generic_targets(addressed, glossary_generic_target_words(self.tgt))

    @staticmethod
    def _first_occurrences(
        terms: list[GlossaryTerm],
        store: Storage | GlossaryStore,
        history: Iterable[TranslatedSegmentEvidence],
        before: tuple[int, int],
    ) -> dict[str, TranslatedSegmentEvidence]:
        """Find the first translated paragraph before the given position for terms not yet
        stored. One-character sources are skipped: a substring hit such as 孤 in 孤舟 is
        usually a different word, so the batch's own translation is kept instead.
        """
        pending = {
            term.source
            for term in terms
            if not _is_single_character_source(term.source) and store.get_term(term.source) is None
        }
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
                cached_mappings = saved.get("resolved") if isinstance(saved, dict) else None
                if (
                    not isinstance(saved, dict)
                    or saved.get("schema_version") != _HISTORY_VERSION
                    or not isinstance(cached_mappings, dict)
                    or any(
                        source not in historical_targets
                        or not isinstance(target, str)
                        or not target.strip()
                        or target != target.strip()
                        or not source_matches_text(target, historical_targets[source])
                        for source, target in cached_mappings.items()
                    )
                ):
                    raise ValueError("Invalid glossary history alignment cache")
                return dict(cached_mappings)
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
        A name aligned to cased-script wording without any capital letter, while the batch
        rendered it with one, matched a different word (横江 in 白露横江 "white dew lay
        across the river"). Such a match keeps the batch's own target and is removed from
        occurrences. Caseless targets such as Chinese are never rejected this way.
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
        rejected = 0
        for term in terms:
            if term.source not in occurrences:
                aligned.append(term)
                continue
            target = resolved.get(term.source)
            if not target:
                unresolved += 1
                continue
            if (
                term.type in _NAME_TYPES
                and _has_uppercase(term.target)
                and _is_lowercase_only(target)
            ):
                rejected += 1
                del occurrences[term.source]
                aligned.append(term)
                continue
            aligned.append(replace(term, target=target))
        if rejected:
            _LOGGER.warning(
                "Kept %d batch name translation(s) whose historical match was not a name "
                "(operation=glossary.align_history)",
                rejected,
            )
        return aligned, len(resolved) - rejected, unresolved

    def prepare_extraction(
        self,
        store: Storage | GlossaryStore,
        source_text: str,
        target_text: str,
        *,
        source_corpus: str | None = None,
    ) -> ExtractionRequest:
        """Read the glossary entries one extraction request shows the model.

        With source_corpus, show only existing terms occurring at least twice in the source.
        Low-frequency terms remain stored but do not repeatedly consume extraction context.
        """
        all_existing = [t for t in store.all_terms() if t.type != TYPE_NAVIGATION]
        existing = (
            self._recurring_existing_terms(all_existing, source_corpus)
            if source_corpus is not None
            else all_existing
        )
        existing = prompt_terms(
            existing,
            source_text,
            budget=self.config.pipeline.glossary_prompt_tokens,
        )
        return ExtractionRequest(source_text, target_text, tuple(existing), tuple(all_existing))

    def extract_candidates(self, request: ExtractionRequest) -> list[GlossaryTerm]:
        """Call the model for one prepared request; reads and writes no run state."""
        return self.extract(request.source_text, request.target_text, list(request.existing))

    def store_extracted(
        self,
        store: Storage | GlossaryStore,
        request: ExtractionRequest,
        terms: list[GlossaryTerm],
        chapter: int,
        *,
        history: Iterable[TranslatedSegmentEvidence] = (),
        before: tuple[int, int] | None = None,
        source_corpus: str | None = None,
        admit: Callable[[GlossaryTerm, int | None], str] | None = None,
    ) -> dict[str, int]:
        """Filter, align and store the candidates of one request; see ``extract_and_store``."""
        source_text = request.source_text
        if len(request.existing) < len(request.all_existing):
            terms = _filter_shadowed_sources(terms, source_text, (*terms, *request.all_existing))
        if source_corpus is not None:
            terms = self._drop_single_occurrence_new_terms(terms, store, source_corpus)
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
            stored = store.get_term(t.source)
            if (
                stored is not None
                and stored.target != t.target
                and equivalent_targets(stored.target, t.target)
            ):
                # Case, spacing, a leading "the" or a final stop is not a new translation.
                t.target = stored.target
            evidence = occurrences.get(t.source)
            t.first_chapter = evidence.chapter if evidence is not None else chapter
            result = admit(t, chapter) if admit else store.upsert_term(t, chapter=chapter)
            summary[result] = summary.get(result, 0) + 1
        return summary

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
        source, and do not store new terms occurring only once in the book.
        """
        request = self.prepare_extraction(
            store, source_text, target_text, source_corpus=source_corpus
        )
        return self.store_extracted(
            store,
            request,
            self.extract_candidates(request),
            chapter,
            history=history,
            before=before,
            source_corpus=source_corpus,
            admit=admit,
        )
