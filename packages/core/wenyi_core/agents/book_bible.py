"""Pre-translation Book Bible compiler with dual-context entropy probing.

Build a compact, high-signal frozen glossary *before* chapter 1 translates:
1. Deterministically mine recurring entity candidates from chapter digests (which already
   quote source-spelling names/terms) and whole-book source frequency (>= 2 occurrences).
2. Extract two distant short source snippets per candidate and translate them in separate,
   independent calls using the economical ``glossary.extract`` route to measure whether the
   model disagrees with itself (high entropy) or produces generic pronouns/titles.
3. Adjudicate all divergent entities and core cast anchors once globally with synopsis +
   style context so chapter 1 starts anchored, per-batch extraction is eliminated from the
   critical path, and parallel chapters share one immutable glossary prefix.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from ..glossary.store import (
    equivalent_targets,
    normalize_match_text,
    source_occurrence_spans,
)
from ..i18n.languages import (
    glossary_context_dependent_sources,
    glossary_generic_target_words,
    glossary_single_character_names_only,
)
from ..llm.json_parser import JsonParseError
from ..llm.retrying import TruncatedResponseError, is_resumable_provider_interrupt
from ..models.glossary import GlossaryTerm, normalize_gender
from .base import Agent

_LOGGER = logging.getLogger(__name__)

# CJK runs inside target-language digests (digests quote original source names in source spelling).
_CJK_SPAN_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]{2,6}")
# Capitalized multi-word / proper-noun sequences for Latin-script sources.
_LATIN_ENTITY_RE = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3}\b")
_WORD_RE = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*")

# Pure CJK function/glue characters (excluding proper-noun/title/place morphemes like 天王山海門石金星).
_CJK_STOP_CHARS = frozenset(
    "的一是在不了有人这這中为為上个個我以要他她它时時来來用们們生到作地于於出就分对對成会會可发發动動同也能下过過说說面而方后後多定所民得进進着著等如自起加量都两兩当當使去把性好应應开開合还還因由其些然前外那事相全间間样樣与與关關各重新内內反你明看原又么麼比或但向此变變只没沒问問意无無很情者最立代想已并並提直很将將把被给給从從往却卻便才呢吗嗎吧啊呀嘛哪谁誰怎何莫非乃即若虽雖故则則且再甚颇頗笑叫听聽知觉覺见見"
)
# Tiny subset of structural particles that never belong at edges of a CJK name/title.
_CJK_EDGE_PARTICLES = frozenset(
    "的了呢吗嗎吧啊呀嘛着著过過在是有为為与與和及或而但就都也却卻便只才将將把被给給从從向往对對于於之其此那这這哪谁誰怎何不莫无無非乃即若虽雖因故则則且又再还還很太更最极極甚颇頗说說道笑问問叫听聽看知想觉覺见見来來去上下来到"
)


@dataclass(frozen=True)
class BibleCandidate:
    source: str
    count: int
    from_digest: bool
    snippet_a: str
    snippet_b: str


@dataclass(frozen=True)
class BibleResult:
    terms: tuple[GlossaryTerm, ...]
    variants_by_source: dict[str, tuple[str, ...]]
    candidate_count: int
    divergent_count: int


def _extract_distant_snippets(
    source: str,
    corpus: str,
    normalized_corpus: str,
    *,
    radius: int = 38,
) -> tuple[str, str]:
    """Return two distant context snippets (first and ~2/3rd occurrence) for ``source``."""
    spans = source_occurrence_spans(source, normalized_corpus)
    if not spans:
        return source, source
    first_pos = spans[0][0]
    second_idx = max(1, (len(spans) * 2) // 3) if len(spans) > 1 else 0
    second_pos = spans[min(second_idx, len(spans) - 1)][0]

    def _slice(pos: int) -> str:
        lo = max(0, pos - radius)
        hi = min(len(corpus), pos + len(source) + radius)
        return " ".join(corpus[lo:hi].split())

    return _slice(first_pos), _slice(second_pos)


def _is_generic_target(target: str, generic_words: frozenset[str]) -> bool:
    if not generic_words:
        return False
    words = _WORD_RE.findall(target.casefold())
    if not words:
        return True
    return all(w.replace("’", "'") in generic_words for w in words)


def mine_bible_candidates(
    digests: Sequence[str],
    source_corpus: str,
    *,
    source_lang: str,
    existing_sources: set[str] | None = None,
    max_candidates: int = 120,
) -> list[BibleCandidate]:
    """Deterministically collect recurring entity candidates from digests + source corpus.

    Priority goes to source-spelling entities explicitly quoted in chapter digests
    (which are already filtered by the digest LLM to key characters, places and titles),
    supplemented by high-frequency CJK/Latin spans from the source corpus.
    """
    if not source_corpus.strip():
        return []
    existing = {normalize_match_text(s).strip() for s in (existing_sources or ())}
    blocked = {
        normalize_match_text(s).strip() for s in glossary_context_dependent_sources(source_lang)
    }
    normalized_corpus = normalize_match_text(source_corpus)
    is_cjk_source = glossary_single_character_names_only(source_lang) or source_lang.startswith(
        ("zh", "ja")
    )

    digest_candidates: set[str] = set()
    joined_digests = "\n".join(digests)
    if is_cjk_source:
        for match in _CJK_SPAN_RE.finditer(joined_digests):
            token = match.group(0).strip()
            if (
                len(token) >= 2
                and token[0] not in _CJK_EDGE_PARTICLES
                and token[-1] not in _CJK_EDGE_PARTICLES
            ):
                digest_candidates.add(token)
    else:
        for match in _LATIN_ENTITY_RE.finditer(joined_digests):
            token = match.group(0).strip()
            if len(token) >= 3:
                digest_candidates.add(token)

    # Also mine high-frequency 2..5 char CJK sequences from source_corpus when digests are sparse.
    corpus_counts: Counter[str] = Counter()
    if is_cjk_source:
        # Split on punctuation to mine contiguous phrase runs.
        for chunk in re.findall(r"[\u3400-\u9fff\uf900-\ufaff]{2,}", source_corpus):
            clen = len(chunk)
            for n in (2, 3, 4, 5):
                if clen < n:
                    continue
                for start in range(clen - n + 1):
                    gram = chunk[start : start + n]
                    if gram[0] in _CJK_EDGE_PARTICLES or gram[-1] in _CJK_EDGE_PARTICLES:
                        continue
                    corpus_counts[gram] += 1

    # Score and filter candidates with exact corpus occurrence counts.
    raw_scored: dict[str, tuple[int, bool]] = {}
    for cand in digest_candidates:
        norm = normalize_match_text(cand).strip()
        if not norm or norm in existing or norm in blocked:
            continue
        count = len(source_occurrence_spans(cand, normalized_corpus))
        if count >= 2:
            raw_scored[cand] = (count, True)

    # Supplement with top recurring non-digest n-grams (>=3 for 3..5-char compounds, >=4 for 2-char).
    for gram, approx_count in corpus_counts.most_common(500):
        min_needed = 4 if len(gram) == 2 else 3
        if approx_count < 3:
            break
        if approx_count < min_needed or gram in raw_scored:
            continue
        norm = normalize_match_text(gram).strip()
        if not norm or norm in existing or norm in blocked:
            continue
        if all(ch in _CJK_STOP_CHARS for ch in gram):
            continue
        count = len(source_occurrence_spans(gram, normalized_corpus))
        if count >= min_needed:
            raw_scored[gram] = (count, False)

    # Prune substrings mostly shadowed by a longer candidate (e.g., drop "弼馬" when
    # "弼馬溫" covers nearly all its hits; keep standalone recurring short names like "行者").
    sorted_keys = sorted(
        raw_scored.keys(),
        key=lambda k: (raw_scored[k][1], raw_scored[k][0], len(k)),
        reverse=True,
    )
    kept_keys: list[str] = []
    for cand in sorted_keys:
        c_count, c_from_digest = raw_scored[cand]
        shadowed = False
        for other in raw_scored:
            if other == cand or len(other) <= len(cand) or cand not in other:
                continue
            o_count, _ = raw_scored[other]
            ratio = 0.80 if c_from_digest else 0.70
            if o_count >= c_count * ratio and (c_count - o_count) <= 2:
                shadowed = True
                break
            if not c_from_digest and o_count >= c_count * 0.70:
                shadowed = True
                break
        if not shadowed:
            kept_keys.append(cand)
        if len(kept_keys) >= max_candidates:
            break

    candidates: list[BibleCandidate] = []
    for cand in kept_keys:
        count, from_digest = raw_scored[cand]
        snip_a, snip_b = _extract_distant_snippets(cand, source_corpus, normalized_corpus)
        candidates.append(
            BibleCandidate(
                source=cand,
                count=count,
                from_digest=from_digest,
                snippet_a=snip_a,
                snippet_b=snip_b,
            )
        )
    return candidates


class BookBibleAgent(Agent):
    """Dual-context entropy probe + single global adjudicator for pre-translation bible seeding."""

    def _probe_snippets(
        self,
        items: Sequence[tuple[str, str]],
        *,
        batch_size: int = 35,
    ) -> dict[str, str]:
        """Render each ``(source, snippet)`` independently in batched ``glossary.extract`` calls."""
        results: dict[str, str] = {}
        system = (
            f"You are a fast terminology prober translating {self.src} entity spans into {self.tgt}. "
            "For each numbered item, give the natural English/target rendering of the named entity "
            "as used in its snippet. If the span is an ordinary verb, common noun, pronoun, or "
            "context-dependent form of address (like 'I', 'my lord', 'said'), return an empty string.\n"
            'Return ONLY JSON: {"renderings": {"<source>": "<target or empty>", ...}}.'
        )
        for offset in range(0, len(items), batch_size):
            chunk = items[offset : offset + batch_size]
            lines = [f"{i}. {src} | {snip}" for i, (src, snip) in enumerate(chunk)]
            user = (
                f"Source language: {self.src}, Target language: {self.tgt}\n"
                "Render each term in context:\n"
                + "\n".join(lines)
                + '\nReturn JSON: {"renderings": {"source": "target"}}'
            )
            try:
                data = self._complete_json_dict(
                    [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    operation="glossary.extract",
                )
            except (JsonParseError, TruncatedResponseError, ValueError) as exc:
                _LOGGER.warning("Bible probe batch failed (%s); skipping batch", exc)
                continue
            except Exception as exc:
                if is_resumable_provider_interrupt(exc):
                    raise
                _LOGGER.warning("Bible probe batch error (%s); skipping batch", exc)
                continue
            raw_map = data.get("renderings") if isinstance(data, dict) else None
            if isinstance(raw_map, dict):
                for src, _ in chunk:
                    val = raw_map.get(src)
                    if isinstance(val, str):
                        results[src] = val.strip()
        return results

    def _adjudicate(
        self,
        candidates: Sequence[tuple[BibleCandidate, str, str, bool]],
        *,
        book_synopsis: str,
        style: str,
        existing_terms: Sequence[GlossaryTerm],
    ) -> list[GlossaryTerm]:
        """Settle all kept candidates once globally with synopsis + genre + probe context."""
        if not candidates:
            return []
        existing_block = "\n".join(
            f"- {t.source} => {t.target} ({t.type})" for t in existing_terms[:40]
        )
        rows = []
        for cand, rend_a, rend_b, divergent in candidates:
            tag = "DIVERGENT" if divergent else "ANCHOR"
            rows.append(
                f"- {cand.source} (count={cand.count}, {tag}) | probeA={rend_a!r} | probeB={rend_b!r}"
            )
        system = (
            f"You are a senior literary translation editor compiling a frozen Book Bible ({self.src} -> {self.tgt}). "
            "Given recurring book entities and their two independent draft probe renderings, decide ONE canonical "
            "translation for each genuine proper noun, title, epithet, place, organization, or setting-specific term.\n"
            "Rules:\n"
            "1. EXCLUDE ordinary words, verb phrases, idioms, pronouns, generic standalone common nouns/titles, "
            "bare forms of address (like 孤, 貧僧, 師父, 大人, 老孫, 小人), and truncated stems/fragments of a longer "
            "compound term already present in the candidate list.\n"
            "2. Resolve every DIVERGENT entry decisively into ONE consistent canonical target.\n"
            "3. Maintain ONE coherent naming policy across the whole book: translate descriptive epithets, titles, "
            "places, palaces, caves, artifacts, spells, and astronomical/religious terms into idiomatic target English "
            "rather than opaque transliteration, and keep parallel place/deity pairs in the same convention.\n"
            "4. Keep standard unsplit romanization for Chinese personal and dharma names (e.g. Sanzang, not San Zang; "
            "Wukong, not Wu Kong) unless a recurring byname is a transparent epithet.\n"
            "5. Keep linked name families consistent (e.g. a person, their estate, and their title should share one root).\n"
            "6. Respect any existing user-imported glossary mappings listed below.\n"
            'Return ONLY JSON: {"terms": [{"source": "...", "target": "...", "type": "person|place|organization|term", '
            '"gender": "male|female|unknown", "aliases": [], "note": "..."}]}.'
        )
        user = (
            f"[Style guide]\n{style or '(none)'}\n\n"
            f"[Book synopsis]\n{book_synopsis or '(none)'}\n\n"
            f"[Existing locked glossary]\n{existing_block or '(none)'}\n\n"
            f"[Candidates to adjudicate ({len(candidates)})]\n"
            + "\n".join(rows)
            + '\n\nReturn JSON: {"terms": [...]}'
        )
        try:
            data = self._complete_json_dict(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                operation="glossary.extract",
            )
        except (JsonParseError, TruncatedResponseError, ValueError) as exc:
            _LOGGER.warning("Bible adjudication failed (%s); falling back to probe results", exc)
            return []
        except Exception as exc:
            if is_resumable_provider_interrupt(exc):
                raise
            _LOGGER.warning("Bible adjudication error (%s)", exc)
            return []

        raw_terms = data.get("terms") if isinstance(data, dict) else None
        if not isinstance(raw_terms, list):
            return []

        generic_words = glossary_generic_target_words(self.tgt)
        blocked_sources = {
            normalize_match_text(s).strip() for s in glossary_context_dependent_sources(self.src)
        }
        allowed_sources = {c.source for c, _, _, _ in candidates}
        out: list[GlossaryTerm] = []
        seen: set[str] = set()
        for item in raw_terms:
            if not isinstance(item, dict):
                continue
            src = str(item.get("source") or "").strip()
            tgt = str(item.get("target") or "").strip()
            if not src or not tgt or src not in allowed_sources:
                continue
            norm = normalize_match_text(src).strip()
            if norm in seen or norm in blocked_sources:
                continue
            if _is_generic_target(tgt, generic_words):
                continue
            term_type = str(item.get("type") or "term").strip().lower()
            if term_type not in {"person", "place", "organization", "term"}:
                term_type = "term"
            gender = normalize_gender(str(item.get("gender") or ""))
            aliases = [
                str(a).strip()
                for a in (item.get("aliases") or [])
                if isinstance(a, str) and a.strip() and a.strip() != src
            ]
            note = str(item.get("note") or "").strip()
            seen.add(norm)
            out.append(
                GlossaryTerm(
                    source=src,
                    target=tgt,
                    type=term_type,
                    gender=gender,
                    aliases=aliases,
                    first_chapter=0,
                    note=note,
                )
            )
        return out

    def compile_bible(
        self,
        digests: Sequence[str],
        source_corpus: str,
        *,
        book_synopsis: str = "",
        style: str = "",
        existing_terms: Sequence[GlossaryTerm] = (),
        max_candidates: int = 120,
    ) -> BibleResult:
        """Mine candidates, run dual-context entropy probe, and adjudicate a frozen Book Bible."""
        existing_sources = {t.source for t in existing_terms}
        candidates = mine_bible_candidates(
            digests,
            source_corpus,
            source_lang=self.src,
            existing_sources=existing_sources,
            max_candidates=max_candidates,
        )
        if not candidates:
            return BibleResult(
                terms=(), variants_by_source={}, candidate_count=0, divergent_count=0
            )

        # Dual-context entropy probe: snippet A and snippet B in two separate calls so neither
        # biases the other.
        probe_a = self._probe_snippets([(c.source, c.snippet_a) for c in candidates])
        probe_b = self._probe_snippets([(c.source, c.snippet_b) for c in candidates])

        generic_words = glossary_generic_target_words(self.tgt)
        to_adjudicate: list[tuple[BibleCandidate, str, str, bool]] = []
        variants_by_source: dict[str, tuple[str, ...]] = {}
        divergent_count = 0
        for cand in candidates:
            ra = probe_a.get(cand.source, "").strip()
            rb = probe_b.get(cand.source, "").strip()
            if not ra and not rb:
                continue
            if (not ra or _is_generic_target(ra, generic_words)) and (
                not rb or _is_generic_target(rb, generic_words)
            ):
                continue
            divergent = bool(ra and rb and not equivalent_targets(ra, rb))
            if divergent:
                divergent_count += 1
            # Keep every divergent entity + core anchors (digest-cited or high frequency >= 5).
            if divergent or cand.from_digest or cand.count >= 5:
                to_adjudicate.append((cand, ra, rb, divergent))
                distinct_vars = tuple(
                    dict.fromkeys(
                        v for v in (ra, rb) if v and not _is_generic_target(v, generic_words)
                    )
                )
                if distinct_vars:
                    variants_by_source[cand.source] = distinct_vars

        terms = self._adjudicate(
            to_adjudicate,
            book_synopsis=book_synopsis,
            style=style,
            existing_terms=existing_terms,
        )
        return BibleResult(
            terms=tuple(terms),
            variants_by_source=variants_by_source,
            candidate_count=len(candidates),
            divergent_count=divergent_count,
        )

    def _complete_json_dict(self, messages, *, operation: str) -> dict:
        data, _raw = self._complete_json_turn(messages, operation=operation)
        if isinstance(data, dict):
            return data
        raise JsonParseError(f"Expected JSON object from {operation}")
