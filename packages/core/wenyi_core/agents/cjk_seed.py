"""Lean CJK upfront glossary seeder for parallel ZH->EN (and future JA/KO->EN) web novels.

Mines recurring CJK proper-noun candidates from chapter digests (or bounded source samples
when digests are absent) and compiles a compact canonical entity glossary in parallel
chunks over the ``glossary.extract`` route so parallel chapter workers share consistent
names and a frozen KV-cacheable system prefix.

Pure agent module: zero pipeline or storage imports.
"""

from __future__ import annotations

import contextvars
import logging
import re
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from ..glossary.store import GlossaryStore, GlossaryTerm
from ..i18n.languages import (
    glossary_context_dependent_sources,
    glossary_generic_target_words,
)
from ..llm.json_parser import JsonParseError
from ..llm.preflight import fit_prompt_to_context_window
from ..llm.retrying import (
    ProviderRequestError,
    TruncatedResponseError,
    error_status_code,
    is_resumable_provider_interrupt,
)
from . import prompts
from .base import Agent

_LOGGER = logging.getLogger(__name__)

# Contiguous CJK ideograph runs and katakana runs (for future JA support).
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_PURE_CJK_RE = re.compile(r"^[\u4e00-\u9fff]+$")
_KATAKANA_RE = re.compile(r"[\u30a1-\u30fa\u30fc]{2,12}")
# Titles/techniques/factions explicitly marked in CJK brackets.
_BRACKETED_RE = re.compile(r"[《「『【]([^》」』】\n]{2,12})[》」』】]")
_NGRAM_MIN = 2
_NGRAM_MAX = 5
_VALID_TYPES = frozenset({"person", "place", "organization", "term"})
_GENERIC_SINGLE_WORD_RE = re.compile(r"[A-Za-z]+")
_SNIPPET_RADIUS = 36
_CHUNK_SIZE = 60
_DIRECT_COUNT_THRESHOLD = 250


@dataclass(frozen=True)
class SeedCandidate:
    """One recurring CJK candidate term and a short raw context snippet."""

    source: str
    count: int
    snippet: str


@dataclass(frozen=True)
class SeedResult:
    """Result of upfront CJK glossary seeding, including whether all chunks succeeded cleanly."""

    terms: list[GlossaryTerm]
    complete: bool = True


def _first_snippet(corpus: str, term: str) -> str:
    pos = corpus.find(term)
    if pos < 0:
        return ""
    start = max(0, pos - _SNIPPET_RADIUS)
    end = min(len(corpus), pos + len(term) + _SNIPPET_RADIUS)
    return corpus[start:end].replace("\n", " ").strip()


def _bounded_sample_texts(
    source_corpus: str, *, max_slices: int = 16, slice_chars: int = 1500
) -> list[str]:
    """Sample evenly across ``source_corpus`` when chapter digests are unavailable."""
    if not source_corpus:
        return []
    total = len(source_corpus)
    if total <= max_slices * slice_chars:
        return [source_corpus]
    step = max(1, (total - slice_chars) // max(1, max_slices - 1))
    return [source_corpus[i * step : i * step + slice_chars] for i in range(max_slices)]


def _count_candidates_in_corpus(source_corpus: str, candidates: set[str]) -> dict[str, int]:
    """Count corpus occurrences in O(N) for large candidate sets without repeated full scans."""
    cjk_by_len: dict[int, set[str]] = {}
    other_cands: list[str] = []
    for tok in candidates:
        tlen = len(tok)
        if _NGRAM_MIN <= tlen <= _NGRAM_MAX and _PURE_CJK_RE.match(tok):
            cjk_by_len.setdefault(tlen, set()).add(tok)
        else:
            other_cands.append(tok)

    counts: dict[str, int] = {}
    cjk_total = sum(len(bucket) for bucket in cjk_by_len.values())
    if cjk_total <= _DIRECT_COUNT_THRESHOLD:
        for bucket in cjk_by_len.values():
            for tok in bucket:
                freq = source_corpus.count(tok)
                if freq >= 2:
                    counts[tok] = freq
    elif cjk_by_len:
        # Single linear scan over contiguous CJK runs checks only active candidate lengths,
        # tracking each token's next eligible offset to match non-overlapping str.count().
        active_buckets = tuple(sorted(cjk_by_len.items()))
        hit_counts: Counter[str] = Counter()
        next_valid: dict[str, int] = {}
        for run_match in _CJK_RUN_RE.finditer(source_corpus):
            run = run_match.group(0)
            run_start = run_match.start()
            run_len = len(run)
            for size, bucket in active_buckets:
                if run_len < size:
                    break
                for start in range(0, run_len - size + 1):
                    sub = run[start : start + size]
                    if sub in bucket:
                        pos = run_start + start
                        if pos >= next_valid.get(sub, 0):
                            hit_counts[sub] += 1
                            next_valid[sub] = pos + size
        for tok, freq in hit_counts.items():
            if freq >= 2:
                counts[tok] = freq

    for tok in other_cands:
        freq = source_corpus.count(tok)
        if freq >= 2:
            counts[tok] = freq
    return counts


def mine_cjk_candidates(
    digests: Sequence[str],
    source_corpus: str,
    *,
    existing_sources: set[str] | frozenset[str] = frozenset(),
    context_dependent_sources: set[str] | frozenset[str] = frozenset(),
    max_candidates: int = 480,
) -> list[SeedCandidate]:
    """Deterministically mine recurring CJK proper-noun candidates for upfront seeding.

    Reads chapter digests first (high proper-noun density); if digests are absent, falls
    back to evenly spaced slices of ``source_corpus``. Requires >= 2 corpus occurrences,
    drops context-dependent forms and existing entries, and suppresses substrings shadowed
    >= 80% by a longer compound in O(M) time.
    """
    if not source_corpus:
        return []
    mining_texts = [d for d in digests if d and d.strip()]
    used_fallback_samples = not mining_texts
    if used_fallback_samples:
        mining_texts = _bounded_sample_texts(source_corpus)

    raw_hits: Counter[str] = Counter()
    for text in mining_texts:
        for m in _BRACKETED_RE.finditer(text):
            token = m.group(1).strip()
            if _CJK_RUN_RE.search(token) or _KATAKANA_RE.search(token):
                raw_hits[token] += 2
        for m in _KATAKANA_RE.finditer(text):
            raw_hits[m.group(0)] += 1
        # Chinese has no spaces between words: mine 2..5 char sliding CJK n-grams inside
        # each hanzi run, then validate recurrence against the full corpus.
        for run_match in _CJK_RUN_RE.finditer(text):
            run = run_match.group(0)
            run_len = len(run)
            for size in range(_NGRAM_MIN, min(_NGRAM_MAX, run_len) + 1):
                for start in range(0, run_len - size + 1):
                    raw_hits[run[start : start + size]] += 1

    excluded = existing_sources | context_dependent_sources
    for tok in list(raw_hits):
        if tok in excluded:
            del raw_hits[tok]

    raw_cap = max(max_candidates * 10, 4800)
    if len(raw_hits) > raw_cap:
        # In large books/fallback samples, keep the highest-recurrence raw candidates.
        ranked_raw = sorted(raw_hits, key=lambda s: (-raw_hits[s], -len(s), s))[:raw_cap]
        candidate_set = set(ranked_raw)
    else:
        candidate_set = set(raw_hits)

    if not candidate_set:
        return []

    counts = _count_candidates_in_corpus(source_corpus, candidate_set)
    if not counts:
        return []

    # Drop shorter substrings shadowed >= 80% by a longer mined compound in O(M) using
    # a parent-count table over proper substrings (tokens are bounded at <= 12 chars).
    by_length_desc = sorted(counts, key=lambda s: (-len(s), -counts[s], s))
    kept: list[str] = []
    max_parent_count: dict[str, int] = {}
    for token in by_length_desc:
        freq = counts[token]
        if max_parent_count.get(token, 0) * 5 >= freq * 4:
            continue
        kept.append(token)
        tlen = len(token)
        for sub_len in range(_NGRAM_MIN, tlen):
            for i in range(tlen - sub_len + 1):
                sub = token[i : i + sub_len]
                if sub in counts and freq > max_parent_count.get(sub, 0):
                    max_parent_count[sub] = freq

    # Also drop substrings shadowed >= 80% by an already-existing glossary source in O(M).
    kept_set = set(kept)
    if kept_set and existing_sources:
        max_kept_len = max(len(tok) for tok in kept_set)
        existing_parent: dict[str, int] = {}
        for ex in existing_sources:
            ex_len = len(ex)
            if ex_len <= _NGRAM_MIN:
                continue
            matched_subs: list[str] = []
            upper = min(ex_len, max_kept_len + 1)
            for sub_len in range(_NGRAM_MIN, upper):
                for i in range(ex_len - sub_len + 1):
                    sub = ex[i : i + sub_len]
                    if sub in kept_set:
                        matched_subs.append(sub)
            if not matched_subs:
                continue
            ex_cnt = source_corpus.count(ex)
            for sub in matched_subs:
                if ex_cnt > existing_parent.get(sub, 0):
                    existing_parent[sub] = ex_cnt
        if existing_parent:
            kept = [tok for tok in kept if existing_parent.get(tok, 0) * 5 < counts[tok] * 4]

    # Rank by corpus frequency descending (tie-break deterministically by token).
    kept.sort(key=lambda s: (-counts[s], -len(s), s))
    top = kept[: max(1, max_candidates)]
    return [
        SeedCandidate(source=tok, count=counts[tok], snippet=_first_snippet(source_corpus, tok))
        for tok in top
    ]


class CjkGlossarySeeder(Agent):
    """Compile recurring CJK web-novel candidates into canonical glossary terms."""

    def seed(
        self,
        candidates: Sequence[SeedCandidate],
        *,
        book_synopsis: str = "",
        style: str = "",
        existing_terms: Sequence[GlossaryTerm] = (),
        max_workers: int = 4,
    ) -> list[GlossaryTerm]:
        """Adjudicate mined CJK candidates into canonical terms in parallel chunks."""
        return self.seed_result(
            candidates,
            book_synopsis=book_synopsis,
            style=style,
            existing_terms=existing_terms,
            max_workers=max_workers,
        ).terms

    def seed_result(
        self,
        candidates: Sequence[SeedCandidate],
        *,
        book_synopsis: str = "",
        style: str = "",
        existing_terms: Sequence[GlossaryTerm] = (),
        max_workers: int = 4,
    ) -> SeedResult:
        """Adjudicate mined CJK candidates and report whether all chunks succeeded cleanly."""
        if not candidates:
            return SeedResult([], complete=True)

        chunks = [
            list(candidates[i : i + _CHUNK_SIZE]) for i in range(0, len(candidates), _CHUNK_SIZE)
        ]

        workers = min(max(1, max_workers), len(chunks))
        if workers <= 1:
            chunk_outcomes = [
                self._adjudicate_chunk(book_synopsis, style, existing_terms, chunk)
                for chunk in chunks
            ]
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wenyi-cjk-seed") as ex:
                futures = [
                    ex.submit(
                        contextvars.copy_context().run,
                        self._adjudicate_chunk,
                        book_synopsis,
                        style,
                        existing_terms,
                        ch,
                    )
                    for ch in chunks
                ]
                chunk_outcomes = [fut.result() for fut in futures]

        context_sources = glossary_context_dependent_sources(self.src)
        generic_words = glossary_generic_target_words(self.tgt)
        existing_keys = {t.source for t in existing_terms}

        merged: list[GlossaryTerm] = []
        seen: set[str] = set()
        complete = True
        for batch, ok in chunk_outcomes:
            if not ok:
                complete = False
            for term in batch:
                if (
                    term.source in seen
                    or term.source in existing_keys
                    or term.source in context_sources
                ):
                    continue
                if self._is_generic_non_person(term, generic_words):
                    continue
                seen.add(term.source)
                merged.append(term)
        return SeedResult(merged, complete=complete)

    def _build_system_prompt(
        self,
        book_synopsis: str,
        style: str,
        existing_terms: Sequence[GlossaryTerm],
    ) -> str:
        """Build a byte-stable KV-cacheable system prompt shared across all seed chunks."""
        parts = [
            f"You compile a consistent glossary for translating a {self.src} web novel into "
            f"{self.tgt}.",
            "Given candidate recurring source terms with corpus frequency and a short snippet, "
            "KEEP ONLY true recurring proper nouns and world-building terminology: character "
            "names, places, sects/factions, cultivation realms, techniques, artifacts, and "
            "named institutions or titles tied to one specific entity.",
            "DROP ordinary words, verbs, adjectives, generic pronouns, and context-dependent "
            "address forms (e.g. 大人, 公子, 师兄, 前辈, 孤, 朕, 臣) unless attached to a specific "
            "proper name.",
            "For every kept person, include gender ('male', 'female', or 'unknown') using only "
            "evidence visible in the synopsis/snippet; never spoil later plot twists.",
            'Return strict JSON: {"terms":[{"source":"...","target":"...","type":"person|place|'
            'organization|term","gender":"male|female|unknown|","aliases":["..."]}]}',
        ]
        if style.strip():
            parts.append(prompts.wrap_untrusted_style("Style guide", style.strip()))
        if book_synopsis.strip():
            parts.append(
                prompts.wrap_untrusted_reference(
                    "Whole-book synopsis", "synopsis_data", book_synopsis.strip()
                )
            )
        if existing_terms:
            rendered = prompts.render_glossary(
                list(existing_terms),
                source_lang=self.src,
                genre=self.language_policy.context.genre,
            )
            parts.append(prompts.wrap_untrusted_glossary(rendered))
        return "\n\n".join(parts)

    def _adjudicate_chunk(
        self,
        book_synopsis: str,
        style: str,
        existing_terms: Sequence[GlossaryTerm],
        chunk: Sequence[SeedCandidate],
    ) -> tuple[list[GlossaryTerm], bool]:
        lines = [
            f"- {c.source} (x{c.count}): {c.snippet}" if c.snippet else f"- {c.source} (x{c.count})"
            for c in chunk
        ]
        user_prompt = (
            "[Candidate recurring source terms]\n"
            + "\n".join(lines)
            + '\n\nReturn JSON {"terms":[...]} keeping only true proper nouns and key world terms.'
        )
        match_text = "\n".join(c.snippet or c.source for c in chunk)
        system_prompt, user_msg, _effective, _shrunk = fit_prompt_to_context_window(
            config=self.config,
            operation="glossary.extract",
            terms=list(existing_terms),
            match_text=match_text,
            render_messages=lambda terms_for_prompt: (
                self._build_system_prompt(book_synopsis, style, terms_for_prompt),
                user_prompt,
            ),
        )
        try:
            data, _raw = self._complete_json_turn(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                operation="glossary.extract",
                require_complete_payload=True,
            )
        except (JsonParseError, TruncatedResponseError, ValueError) as exc:
            _LOGGER.warning("CJK glossary seed chunk failed (%s); skipping chunk", exc)
            return [], False
        except ProviderRequestError as exc:
            # Propagate resumable outages and non-content provider errors (e.g. 401/403/404/405);
            # absorb only HTTP 400/422 content-filter rejections so one noisy chunk never bricks a book.
            if exc.resumable or exc.status_code not in {400, 422}:
                raise
            _LOGGER.warning("CJK glossary seed chunk provider error (%s); skipping chunk", exc)
            return [], False
        except Exception as exc:
            status = error_status_code(exc)
            if is_resumable_provider_interrupt(exc) or (
                status is not None and status not in {400, 422}
            ):
                raise
            _LOGGER.warning("CJK glossary seed chunk error (%s); skipping chunk", exc)
            return [], False

        raw_terms = data.get("terms") if isinstance(data, dict) else None
        if not isinstance(raw_terms, list):
            return [], False

        allowed_sources = {c.source for c in chunk}
        out: list[GlossaryTerm] = []
        for item in raw_terms:
            if not isinstance(item, dict):
                return [], False
            raw_source = item.get("source")
            raw_target = item.get("target")
            raw_type = item.get("type", "term")
            raw_gender = item.get("gender", "")
            raw_aliases = item.get("aliases", [])
            if (
                not isinstance(raw_source, str)
                or not isinstance(raw_target, str)
                or (raw_type is not None and not isinstance(raw_type, str))
                or (raw_gender is not None and not isinstance(raw_gender, str))
                or (
                    raw_aliases is not None
                    and (
                        not isinstance(raw_aliases, list)
                        or not all(isinstance(a, str) for a in raw_aliases)
                    )
                )
            ):
                return [], False
            source = raw_source.strip()
            target = raw_target.strip()
            if not source or not target or source not in allowed_sources:
                continue
            term_type = str(raw_type or "term").strip().lower()
            if term_type not in _VALID_TYPES:
                term_type = "term"
            gender = str(raw_gender or "").strip() if term_type == "person" else ""
            aliases = [a.strip() for a in (raw_aliases or []) if a.strip()]
            out.append(
                GlossaryTerm(
                    source=source,
                    target=target,
                    type=term_type,
                    gender=gender,
                    aliases=aliases,
                    first_chapter=0,
                )
            )
        return out, True

    @staticmethod
    def _is_generic_non_person(term: GlossaryTerm, generic_words: frozenset[str]) -> bool:
        if not generic_words:
            return False
        words = _GENERIC_SINGLE_WORD_RE.findall(term.target)
        if not words:
            return False
        # Preserve capitalized single-word personal names like "Grace" or "Duke".
        if term.type == "person" and len(words) == 1 and words[0][0].isupper():
            return False
        return all(w.casefold() in generic_words for w in words)


__all__ = [
    "CjkGlossarySeeder",
    "GlossaryStore",
    "SeedCandidate",
    "SeedResult",
    "mine_cjk_candidates",
]
