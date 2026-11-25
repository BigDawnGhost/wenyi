"""Pre-translation Book Bible compiler with dual-context entropy probing.

Build a compact, high-signal frozen glossary and book-wide convention sheet *before* chapter 1:
1. Deterministically mine recurring entity candidates from chapter digests and whole-book
   source frequency across CJK (Chinese & Japanese incl. katakana/honorifics/ruby-stripped
   kanji), bicameral scripts (Latin, Greek, Cyrillic with connectors/titles), and unicameral
   scripts (Arabic, Hebrew, Hangul, Indic, etc.).
2. Extract two distant short source snippets per candidate and translate them in separate,
   independent calls using the economical ``glossary.extract`` route to measure whether the
   model disagrees with itself (high entropy) or produces generic pronouns/titles.
3. Cluster linked morpheme families and adjudicate all divergent entities + core cast anchors
   with synopsis + style context (chunking large books safely), emitting canonical terms,
   spoiler-safe gender metadata, and a compact book-wide convention sheet.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

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

# Full contiguous CJK runs inside target-language digests (digests quote original source
# names in source spelling). Runs longer than _CJK_SPAN_MAX_LEN are prose clauses and are
# skipped whole rather than sliced into arbitrary fragments.
_CJK_SPAN_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]{2,}")
_CJK_SPAN_MAX_LEN = 12
# Japanese katakana entity spans (allow middle-dot / equals / prolonged sound mark).
_JA_KATAKANA_RE = re.compile(r"[\u30a0-\u30ff][\u30a0-\u30ffー・＝=]{1,15}[\u30a0-\u30ffー]")
# Japanese honorific-anchored character names (capture stem before honorific).
_JA_HONORIFIC_NAME_RE = re.compile(
    r"([\u3400-\u9fff\uf900-\ufaff]{1,5}|[\u30a0-\u30ffー]{2,12})"
    r"(?:さん|様|さま|殿|どの|君|くん|ちゃん|氏|先生|先輩|卿)"
)
# Japanese/Chinese bracketed title/artifact names 《...》 or 『...』.
_BRACKETED_TITLE_RE = re.compile(r"[《『]([^《》『』\n]{2,14})[》』]")
_RUBY_MARKER_RE = re.compile(r"〘[^〙]*〙")


def _bicameral_letter_classes() -> tuple[str, str]:
    """Return Unicode uppercase and lowercase letter ranges across Latin, Greek and Cyrillic."""
    upper: list[str] = []
    lower: list[str] = []
    for start, end in ((0x0041, 0x024F), (0x0370, 0x052F), (0x1E00, 0x1FFF)):
        for cp in range(start, end + 1):
            ch = chr(cp)
            cat = unicodedata.category(ch)
            if cat == "Lu":
                upper.append(ch)
            elif cat == "Ll":
                lower.append(ch)
    return "".join(upper), "".join(lower)


_UPPER_LETTERS, _LOWER_LETTERS = _bicameral_letter_classes()
_CAP_WORD = rf"(?:[{_UPPER_LETTERS}](?:['’][{_UPPER_LETTERS}])?[{_LOWER_LETTERS}]+(?:-[{_UPPER_LETTERS}][{_LOWER_LETTERS}]+)?)"
_TITLE_PREFIX = r"(?:(?:Mr|Mrs|Ms|Dr|St|Sir|Lord|Lady|King|Queen|Prince|Princess|Duke|Count|Baron|General|Captain|Professor)\.?\s+)"
_CONNECTOR = r"(?:\s+(?:of|the|de|du|des|la|le|van|von|der|den)){1,2}"
# Capitalized multi-word / proper-noun sequences across Latin (incl. Extended), Greek and Cyrillic.
_LATIN_ENTITY_RE = re.compile(
    rf"\b{_TITLE_PREFIX}?{_CAP_WORD}(?:(?:{_CONNECTOR})?\s+{_CAP_WORD}){{0,4}}\b"
)
_LATIN_SIMPLE_RE = re.compile(rf"\b{_CAP_WORD}(?:\s+{_CAP_WORD}){{0,3}}\b")
_WORD_RE = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*")

# High-frequency sentence-initial Latin words that are never proper nouns on their own.
_LATIN_SENTENCE_NOISE = frozenset(
    {
        "the",
        "this",
        "that",
        "these",
        "those",
        "they",
        "their",
        "them",
        "there",
        "then",
        "when",
        "where",
        "while",
        "which",
        "what",
        "who",
        "whom",
        "whose",
        "with",
        "without",
        "within",
        "from",
        "into",
        "upon",
        "under",
        "over",
        "after",
        "before",
        "between",
        "through",
        "during",
        "against",
        "about",
        "however",
        "moreover",
        "therefore",
        "meanwhile",
        "although",
        "though",
        "because",
        "since",
        "unless",
        "until",
        "indeed",
        "perhaps",
        "still",
        "never",
        "always",
        "often",
        "sometimes",
        "suddenly",
        "finally",
        "first",
        "second",
        "third",
        "chapter",
        "prologue",
        "epilogue",
        "introduction",
        "preface",
        "part",
        "book",
        "volume",
        "section",
        "note",
        "author",
        "but",
        "and",
        "for",
        "not",
        "nor",
        "yet",
        "one",
        "two",
        "three",
        "some",
        "many",
        "most",
        "all",
        "any",
        "each",
        "every",
        "both",
        "such",
        "other",
        "another",
        "here",
        "now",
        "thus",
        "hence",
        "even",
        "only",
        "just",
        "also",
        "very",
        "much",
        "more",
        "less",
        "well",
        "yes",
        "his",
        "her",
        "its",
        "our",
        "your",
        "she",
        "him",
        "you",
    }
)


def _is_unicameral_non_cjk_letter(ch: str) -> bool:
    """Return True for uncased letter characters outside CJK ideographs (Arabic, Hebrew, Hangul, Indic, etc.)."""
    cp = ord(ch)
    if 0x3400 <= cp <= 0x9FFF or 0xF900 <= cp <= 0xFAFF or 0x20000 <= cp <= 0x2FA1F:
        return False
    if 0x3040 <= cp <= 0x30FF:
        return False
    return unicodedata.category(ch) == "Lo"


def _extract_unicameral_candidates(text: str) -> set[str]:
    """Extract 1-2 word spans in uncased non-CJK scripts without relying on capitalization."""
    runs: list[list[str]] = []
    current_run: list[str] = []
    token: list[str] = []
    base_count = 0
    for ch in text:
        if _is_unicameral_non_cjk_letter(ch):
            token.append(ch)
            base_count += 1
        elif token and unicodedata.category(ch) in ("Mn", "Mc"):
            token.append(ch)
        else:
            if token:
                if base_count >= 2:
                    current_run.append("".join(token))
                elif current_run:
                    runs.append(current_run)
                    current_run = []
                token = []
                base_count = 0
            if ch not in (" ", "\t") and current_run:
                runs.append(current_run)
                current_run = []
    if token and base_count >= 2:
        current_run.append("".join(token))
    if current_run:
        runs.append(current_run)

    found: set[str] = set()
    for run in runs:
        for i, word in enumerate(run):
            found.add(word)
            if i + 1 < len(run):
                found.add(f"{word} {run[i + 1]}")
    return found


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
    conventions: tuple[str, ...] = ()
    divergent_titles: tuple[tuple[str, tuple[str, ...]], ...] = ()


def select_voice_excerpts(
    source_corpus: str,
    *,
    source_lang: str,
    max_excerpts: int = 3,
) -> list[str]:
    """Deterministically pick 2-3 representative source paragraphs (narration + dialogue) for voice calibration.

    Returns an empty list for very short texts (<2 qualifying paragraphs) so small fixtures and
    trivial documents do not incur extra calibration calls.
    """
    if not source_corpus or max_excerpts <= 0:
        return []
    src_base = (source_lang or "").lower().split("-", 1)[0]
    is_cjk = src_base in {"zh", "ja", "ko"}
    clean_corpus = _RUBY_MARKER_RE.sub("", source_corpus)
    raw_paras = [p.strip() for p in re.split(r"\n+", clean_corpus) if p.strip()]
    qualifying: list[str] = []
    for para in raw_paras:
        if is_cjk:
            if 45 <= len(para) <= 280:
                qualifying.append(para)
        else:
            words = len(_WORD_RE.findall(para))
            if 25 <= words <= 95:
                qualifying.append(para)
    if len(qualifying) < 2:
        return []

    def _has_quote(text: str) -> bool:
        return any(q in text for q in ('"', "“", "「", "『"))

    selected: list[str] = []
    # 1. Early narration paragraph (no dialogue quotes preferred).
    early_narr = next((p for p in qualifying if not _has_quote(p)), None)
    if early_narr:
        selected.append(early_narr)
    # 2. Early dialogue / rhetoric paragraph.
    early_dial = next((p for p in qualifying if _has_quote(p) and p not in selected), None)
    if early_dial:
        selected.append(early_dial)
    # 3. Contrast paragraph from the second half of the book.
    if len(selected) < max_excerpts and len(qualifying) >= 3:
        later_pool = qualifying[len(qualifying) // 2 :]
        later_pick = next((p for p in later_pool if p not in selected), None)
        if later_pick:
            selected.append(later_pick)
    # Fill remaining slots if needed (still requiring at least 2 distinct paragraphs).
    for p in qualifying:
        if len(selected) >= max_excerpts:
            break
        if p not in selected:
            selected.append(p)
    return selected[:max_excerpts] if len(selected) >= 2 else []


# A reading, or one character plus the combining or sound marks NFKC composes into it.
_MATCH_UNIT_RE = re.compile(
    r"〘[^〙]*〙|.[\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f"
    r"\u3099\u309a\uff9e\uff9f]*",
    re.DOTALL,
)


def _match_index(corpus: str) -> tuple[str, list[int]]:
    """Normalize ``corpus`` for matching and map each normalized offset to its raw offset.

    Readings are dropped and each character, with any combining marks after it, is
    normalized on its own, so a span found in the result can be cut from the original text
    with its case intact.
    """
    pieces: list[str] = []
    offsets: list[int] = []
    for match in _MATCH_UNIT_RE.finditer(corpus):
        if match.group(0).startswith("〘") and len(match.group(0)) > 1:
            continue
        piece = normalize_match_text(match.group(0))
        pieces.append(piece)
        offsets.extend([match.start()] * len(piece))
    offsets.append(len(corpus))
    return "".join(pieces), offsets


def _extract_distant_snippets(
    source: str,
    corpus: str,
    match_index: tuple[str, list[int]],
    *,
    radius: int = 38,
) -> tuple[str, str]:
    """Return two distant raw-text snippets (first and ~2/3rd occurrence) for ``source``."""
    normalized, offsets = match_index
    spans = source_occurrence_spans(source, normalized)
    if not spans:
        return source, source
    second_idx = max(1, (len(spans) * 2) // 3) if len(spans) > 1 else 0
    first, second = spans[0], spans[min(second_idx, len(spans) - 1)]

    def _slice(span: tuple[int, int]) -> str:
        lo = max(0, offsets[span[0]] - radius)
        hi = min(len(corpus), offsets[span[1]] + radius)
        return " ".join(corpus[lo:hi].split())

    return _slice(first), _slice(second)


def _is_generic_target(target: str, generic_words: frozenset[str]) -> bool:
    if not generic_words:
        return False
    words = _WORD_RE.findall(target.casefold())
    if not words:
        return True
    return all(w.replace("’", "'") in generic_words for w in words)


_JA_TRAILING_HONORIFIC_CHARS = frozenset("様殿君氏卿")
_BARE_PRONOUN_TARGETS = frozenset(
    {
        "i",
        "me",
        "my",
        "we",
        "us",
        "our",
        "you",
        "your",
        "he",
        "him",
        "his",
        "she",
        "her",
        "they",
        "them",
        "their",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "one",
        "sir",
        "madam",
        "yes",
        "no",
    }
)


def _is_bare_pronoun_target(target: str) -> bool:
    words = _WORD_RE.findall(target.casefold())
    return not words or all(w.replace("’", "'") in _BARE_PRONOUN_TARGETS for w in words)


def _clean_latin_candidate(token: str) -> str:
    cleaned = token.strip()
    if cleaned.startswith(("The ", "A ", "An ")) and len(cleaned.split()) >= 2:
        cleaned = cleaned.split(" ", 1)[1].strip()
    return cleaned


def _is_latin_noise(token: str) -> bool:
    parts = [p.strip(".,").casefold() for p in token.split() if p.strip(".,")]
    if not parts:
        return True
    if len(parts) == 1:
        return parts[0] in _LATIN_SENTENCE_NOISE
    return all(p in _LATIN_SENTENCE_NOISE for p in parts)


def _mine_japanese_spans(text: str) -> set[str]:
    """Extract kanji, katakana, bracketed titles, and honorific-anchored names from Japanese text."""
    clean = _RUBY_MARKER_RE.sub("", text)
    found: set[str] = set()
    for m in _CJK_SPAN_RE.finditer(clean):
        tok = m.group(0).strip()
        if (
            2 <= len(tok) <= _CJK_SPAN_MAX_LEN
            and tok[0] not in _CJK_EDGE_PARTICLES
            and tok[-1] not in _CJK_EDGE_PARTICLES
        ):
            found.add(tok)
    for m in _JA_KATAKANA_RE.finditer(clean):
        tok = m.group(0).strip("・＝=")
        if len(tok) >= 2:
            found.add(tok)
    for m in _JA_HONORIFIC_NAME_RE.finditer(clean):
        stem = m.group(1).strip("・＝=")
        if len(stem) >= 2:
            found.add(stem)
    for m in _BRACKETED_TITLE_RE.finditer(clean):
        inner = m.group(1).strip()
        if 2 <= len(inner) <= 14:
            found.add(inner)
    return found


def mine_bible_candidates(
    digests: Sequence[str],
    source_corpus: str,
    *,
    source_lang: str,
    existing_sources: set[str] | None = None,
    max_candidates: int = 120,
) -> list[BibleCandidate]:
    """Deterministically collect recurring entity candidates from digests + source corpus.

    Priority goes to source-spelling entities explicitly quoted in chapter digests,
    supplemented by high-frequency CJK, Japanese (kanji/katakana/honorifics), Latin,
    Greek/Cyrillic, and unicameral spans from the source corpus.
    """
    if not source_corpus.strip():
        return []
    existing = {normalize_match_text(s).strip() for s in (existing_sources or ())}
    blocked = {
        normalize_match_text(s).strip() for s in glossary_context_dependent_sources(source_lang)
    }
    clean_corpus = _RUBY_MARKER_RE.sub("", source_corpus)
    normalized_corpus = normalize_match_text(clean_corpus)
    src_base = (source_lang or "").lower().split("-", 1)[0]
    is_ja_source = src_base == "ja"
    is_zh_source = src_base == "zh" or glossary_single_character_names_only(source_lang)
    is_cjk_source = is_zh_source or is_ja_source

    digest_candidates: set[str] = set()
    joined_digests = "\n".join(digests)
    if is_ja_source:
        digest_candidates.update(_mine_japanese_spans(joined_digests))
    elif is_zh_source:
        for match in _CJK_SPAN_RE.finditer(joined_digests):
            token = match.group(0).strip()
            if (
                2 <= len(token) <= _CJK_SPAN_MAX_LEN
                and token[0] not in _CJK_EDGE_PARTICLES
                and token[-1] not in _CJK_EDGE_PARTICLES
            ):
                digest_candidates.add(token)
        for match in _BRACKETED_TITLE_RE.finditer(joined_digests):
            inner = match.group(1).strip()
            if 2 <= len(inner) <= 10:
                digest_candidates.add(inner)
    else:
        nfc_digests = unicodedata.normalize("NFC", joined_digests)
        for pat in (_LATIN_ENTITY_RE, _LATIN_SIMPLE_RE):
            for match in pat.finditer(nfc_digests):
                token = match.group(0).strip()
                if len(token) >= 3 and not _is_latin_noise(token):
                    digest_candidates.add(token)
        digest_candidates.update(_extract_unicameral_candidates(nfc_digests))

    # Mine recurring candidates from the source corpus too (for ALL scripts!).
    corpus_counts: Counter[str] = Counter()
    ja_priority_stems: set[str] = set()
    if is_ja_source:
        for m in _JA_KATAKANA_RE.finditer(clean_corpus):
            tok = m.group(0).strip("・＝=")
            if len(tok) >= 2:
                corpus_counts[tok] += 1
                ja_priority_stems.add(tok)
        for m in _JA_HONORIFIC_NAME_RE.finditer(clean_corpus):
            stem = m.group(1).strip("・＝=")
            if len(stem) >= 2:
                corpus_counts[stem] += 1
                ja_priority_stems.add(stem)
        for m in _BRACKETED_TITLE_RE.finditer(clean_corpus):
            inner = m.group(1).strip()
            if 2 <= len(inner) <= 14:
                corpus_counts[inner] += 1
                ja_priority_stems.add(inner)
        for chunk in re.findall(r"[\u3400-\u9fff\uf900-\ufaff]{2,}", clean_corpus):
            clen = len(chunk)
            for n in (2, 3, 4, 5):
                if clen < n:
                    continue
                for start in range(clen - n + 1):
                    gram = chunk[start : start + n]
                    if (
                        gram[0] in _CJK_EDGE_PARTICLES
                        or gram[-1] in _CJK_EDGE_PARTICLES
                        or gram[-1] in _JA_TRAILING_HONORIFIC_CHARS
                    ):
                        continue
                    corpus_counts[gram] += 1
    elif is_zh_source:
        for chunk in re.findall(r"[\u3400-\u9fff\uf900-\ufaff]{2,}", clean_corpus):
            clen = len(chunk)
            for n in (2, 3, 4, 5):
                if clen < n:
                    continue
                for start in range(clen - n + 1):
                    gram = chunk[start : start + n]
                    if gram[0] in _CJK_EDGE_PARTICLES or gram[-1] in _CJK_EDGE_PARTICLES:
                        continue
                    corpus_counts[gram] += 1
    else:
        nfc_corpus = unicodedata.normalize("NFC", clean_corpus)
        # Count mid-sentence or multi-word capitalized entities directly from source_corpus.
        for match in _LATIN_ENTITY_RE.finditer(nfc_corpus):
            token = _clean_latin_candidate(match.group(0))
            if len(token) < 3 or _is_latin_noise(token):
                continue
            pos = match.start()
            is_multiword = " " in token or "-" in token or "'" in token or "’" in token
            if not is_multiword and pos > 0:
                prev_chunk = nfc_corpus[max(0, pos - 3) : pos]
                if not prev_chunk.strip() or prev_chunk.rstrip().endswith(
                    (".", "!", "?", "\n", '"', "“")
                ):
                    continue
            elif not is_multiword and pos == 0:
                continue
            corpus_counts[token] += 1

    # Score and filter candidates with exact corpus occurrence counts.
    raw_scored: dict[str, tuple[int, bool]] = {}
    for cand in digest_candidates:
        norm = normalize_match_text(cand).strip()
        if not norm or norm in existing or norm in blocked:
            continue
        count = len(source_occurrence_spans(cand, normalized_corpus))
        if count >= 2:
            raw_scored[cand] = (count, True)

    # Supplement with top recurring non-digest candidates.
    for gram, approx_count in corpus_counts.most_common(500):
        if is_ja_source:
            min_needed = 2 if (gram in ja_priority_stems or len(gram) >= 3) else 3
        elif is_cjk_source:
            min_needed = 4 if len(gram) == 2 else 3
        else:
            min_needed = 2 if " " in gram else 3
        if approx_count < 2:
            break
        if approx_count < min_needed or gram in raw_scored:
            continue
        norm = normalize_match_text(gram).strip()
        if not norm or norm in existing or norm in blocked:
            continue
        if is_cjk_source and all(ch in _CJK_STOP_CHARS for ch in gram):
            continue
        count = len(source_occurrence_spans(gram, normalized_corpus))
        if count >= min_needed:
            raw_scored[gram] = (count, False)

    # Prune substrings mostly shadowed by a longer candidate. Never let a non-digest corpus
    # n-gram shadow a digest-validated entity.
    sorted_keys = sorted(
        raw_scored.keys(),
        key=lambda k: (raw_scored[k][1], raw_scored[k][0], len(k), k),
        reverse=True,
    )
    kept_keys: list[str] = []
    for cand in sorted_keys:
        c_count, c_from_digest = raw_scored[cand]
        shadowed = False
        for other in raw_scored:
            if other == cand or len(other) <= len(cand) or cand not in other:
                continue
            o_count, o_from_digest = raw_scored[other]
            if c_from_digest and not o_from_digest:
                continue
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
    match_index = _match_index(source_corpus)
    for cand in kept_keys:
        count, from_digest = raw_scored[cand]
        snip_a, snip_b = _extract_distant_snippets(cand, source_corpus, match_index)
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


def _cluster_candidate_rows(
    candidates: Sequence[tuple[BibleCandidate, str, str, bool]],
) -> list[str]:
    """Order and group candidates sharing a >=2-char stem so linked families are adjudicated together."""
    if not candidates:
        return []
    sources = [c.source for c, _, _, _ in candidates]
    # Build stem -> indices map for CJK 2-char prefixes/suffixes or multi-word tokens.
    stem_groups: dict[str, list[int]] = defaultdict(list)
    for idx, src in enumerate(sources):
        clean = src.strip()
        if " " in clean:
            for tok in clean.split():
                if len(tok) >= 4 and tok.casefold() not in _LATIN_SENTENCE_NOISE:
                    stem_groups[tok.casefold()].append(idx)
        elif len(clean) >= 3:
            # Share 2-char prefix (e.g. 青云楼 / 青云阁) or 2-char suffix (e.g. 镇北王 / 镇南王).
            stem_groups[clean[:2]].append(idx)
            stem_groups[clean[-2:]].append(idx)

    # Emit multi-member families first (tagged as family clusters), then remaining standalone items.
    emitted: set[int] = set()
    rows: list[str] = []

    def _fmt(idx: int) -> str:
        cand, rend_a, rend_b, divergent = candidates[idx]
        tag = "DIVERGENT" if divergent else "ANCHOR"
        return (
            f"- {cand.source} (count={cand.count}, {tag}) | probeA={rend_a!r} | probeB={rend_b!r}"
        )

    for stem, indices in sorted(stem_groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        fresh = list(dict.fromkeys(i for i in indices if i not in emitted))
        if len(fresh) >= 2:
            rows.append(f"[Linked family: {stem}]")
            for i in fresh:
                rows.append(_fmt(i))
                emitted.add(i)
    for idx in range(len(candidates)):
        if idx not in emitted:
            rows.append(_fmt(idx))
    return rows


class BookBibleAgent(Agent):
    """Dual-context entropy probe + chunked global adjudicator for pre-translation bible seeding."""

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
            f"For each item, give the natural {self.tgt} rendering of the named entity "
            "as used in its snippet. If the span is an ordinary verb, common noun, pronoun, or "
            "context-dependent form of address (like 'I', 'my lord', 'said'), return an empty string. "
            'Include EVERY item in renderings (use "" to exclude it), keyed by the EXACT source text.\n'
            'Return ONLY JSON: {"renderings": {"<exact_source>": "<target or empty>", ...}}.'
        )

        def _query_chunk(subchunk: list[tuple[str, str]], *, retry: bool = False) -> dict[str, Any]:
            lines = [f"{i}. {src} | {snip}" for i, (src, snip) in enumerate(subchunk)]
            reminder = (
                "The previous reply omitted or returned a non-string value for these keys. "
                'Include EVERY source string as a JSON key with a string value ("" to exclude).\n'
                if retry
                else ""
            )
            example_src = subchunk[0][0] if subchunk else "source"
            user = (
                f"Source language: {self.src}, Target language: {self.tgt}\n"
                f"{reminder}Render each term in context:\n"
                + "\n".join(lines)
                + f'\nReturn JSON: {{"renderings": {{"{example_src}": "target", ...}}}}'
            )
            data = self._complete_json_dict(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                operation="glossary.extract",
            )
            raw_map = data.get("renderings") if isinstance(data, dict) else None
            if not isinstance(raw_map, dict):
                raise ValueError("Book Bible probe returned no renderings map")
            return raw_map

        def _lookup_entry(raw_map: dict[str, Any], idx: int, src: str) -> tuple[bool, Any]:
            """Resolve candidate by exact source key, or safely accept numeric index fallback."""
            if src in raw_map:
                return True, raw_map[src]
            idx_key = str(idx)
            if idx_key in raw_map:
                return True, raw_map[idx_key]
            prefixed = f"{idx}. {src}"
            if prefixed in raw_map:
                return True, raw_map[prefixed]
            return False, None

        for offset in range(0, len(items), batch_size):
            chunk = list(items[offset : offset + batch_size])
            raw_map = _query_chunk(chunk)
            missing: list[tuple[str, str]] = []
            chunk_ok = 0
            for idx, (src, snip) in enumerate(chunk):
                found, val = _lookup_entry(raw_map, idx, src)
                if found and isinstance(val, str):
                    results[src] = val.strip()
                    chunk_ok += 1
                else:
                    missing.append((src, snip))
            if missing:
                try:
                    retry_map = _query_chunk(missing, retry=True)
                except (JsonParseError, TruncatedResponseError, ValueError) as exc:
                    if is_resumable_provider_interrupt(exc) or chunk_ok == 0:
                        raise
                    _LOGGER.warning(
                        "Book Bible probe retry failed (%s); dropping %d unresolved candidate(s): %s",
                        exc,
                        len(missing),
                        [src for src, _ in missing[:5]],
                    )
                    continue
                still_missing: list[str] = []
                still_invalid: list[str] = []
                for idx, (src, _) in enumerate(missing):
                    found, val = _lookup_entry(retry_map, idx, src)
                    if not found:
                        still_missing.append(src)
                    elif not isinstance(val, str):
                        still_invalid.append(src)
                    else:
                        results[src] = val.strip()
                        chunk_ok += 1
                if still_missing or still_invalid:
                    if chunk_ok == 0:
                        if still_invalid and not still_missing:
                            raise ValueError(
                                f"Book Bible probe returned a non-string rendering for: {still_invalid[0]}"
                            )
                        raise ValueError(f"Book Bible probe omitted candidate: {still_missing[0]}")
                    _LOGGER.warning(
                        "Book Bible probe dropped %d unresolved candidate(s) after retry "
                        "(omitted=%d, non_string=%d): %s",
                        len(still_missing) + len(still_invalid),
                        len(still_missing),
                        len(still_invalid),
                        (still_missing + still_invalid)[:5],
                    )
        return results

    def _adjudicate_chunk(
        self,
        candidates: Sequence[tuple[BibleCandidate, str, str, bool]],
        *,
        book_synopsis: str,
        style: str,
        existing_terms: Sequence[GlossaryTerm],
        include_conventions: bool = True,
        divergent_titles: Sequence[tuple[BibleCandidate, str, str]] = (),
    ) -> tuple[list[GlossaryTerm], list[str]]:
        """Adjudicate one bounded chunk of candidates with global synopsis + anchor terms."""
        if not candidates and not (include_conventions and divergent_titles):
            return [], []
        existing_block = "\n".join(
            f"- {t.source} => {t.target} ({t.type})" for t in existing_terms[:60]
        )
        rows = _cluster_candidate_rows(candidates)
        title_rows = [
            f"- {cand.source} (count={cand.count}) | probeA={ra!r} | probeB={rb!r}"
            for cand, ra, rb in divergent_titles[:10]
        ]
        conv_schema = (
            ', "conventions": ["Short book-wide convention rule (max 8 rules total)"]'
            if include_conventions
            else ""
        )
        conv_rule = (
            "7. Also return 5-8 concise book-wide `conventions` for recurring NON-glossary elements "
            "(social/institutional ranks, honorific gradation, verse/tune-title formatting, uniform date style). "
            "Explicitly lock ONE canonical rendering and capitalization for every item listed under "
            "[Divergent recurring titles/offices/motifs] AND for any recurring title/rank/office you exclude "
            "from `terms` (keeping them in `conventions`, NOT in `terms`). "
            "Prioritize natural, idiomatic phrasing in the target language that suits this "
            "book's genre and era; NEVER prescribe stiff word-for-word calques of source forms of "
            "address, self-reference or titles. Never collapse distinct ranks into one rendering.\n"
            if include_conventions
            else ""
        )
        system = (
            f"You are a senior literary translation editor compiling a frozen Book Bible ({self.src} -> {self.tgt}). "
            "Given recurring book entities and their two independent draft probe renderings, decide ONE canonical "
            "translation for each genuine proper noun, title, epithet, place, organization, or setting-specific term.\n"
            "Rules:\n"
            "1. EXCLUDE ordinary words, verb phrases, idioms, pronouns, generic standalone common nouns/titles, "
            "context-dependent bare forms of address, and truncated stems/fragments of a longer compound term "
            "already present in the candidate list.\n"
            "2. Resolve every DIVERGENT entry decisively into ONE consistent canonical target.\n"
            f"3. Maintain ONE coherent naming policy across the whole book suitable for its genre and era. "
            f"Every target must be written in the target language ({self.tgt}).\n"
            "4. Keep linked name families (grouped under [Linked family: ...]) strictly consistent: a person, "
            "their estate/pavilion/tower, and parallel noble/official titles must share one consistent root and "
            "syntactic template.\n"
            "5. Spoiler-safe gender: ONLY set `male` or `female` when unambiguous from the start of the book. "
            "If a character is disguised, cross-dressing, or has a hidden gender/identity revealed later, set "
            '`gender: "unknown"` and note "follow scene POV pronouns; do not spoil reveal".\n'
            "6. Respect any existing locked glossary mappings listed below.\n"
            f"{conv_rule}"
            'Return ONLY JSON: {"terms": [{"source": "...", "target": "...", "type": "person|place|organization|term", '
            '"gender": "male|female|unknown", "aliases": [], "note": "..."}]' + conv_schema + "}."
        )
        user = (
            f"[Style guide]\n{style or '(none)'}\n\n"
            f"[Book synopsis]\n{book_synopsis or '(none)'}\n\n"
            f"[Existing locked glossary]\n{existing_block or '(none)'}\n\n"
            + (
                "[Divergent recurring titles/offices/motifs — lock ONE rendering in conventions, NOT in terms]\n"
                + "\n".join(title_rows)
                + "\n\n"
                if (include_conventions and title_rows)
                else ""
            )
            + f"[Candidates to adjudicate ({len(candidates)})]\n"
            + "\n".join(rows)
            + '\n\nReturn JSON: {"terms": [...]'
            + (', "conventions": [...]' if include_conventions else "")
            + "}"
        )
        data = self._complete_json_dict(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            operation="glossary.extract",
        )
        raw_terms = data.get("terms") if isinstance(data, dict) else None
        if not isinstance(raw_terms, list):
            raise ValueError("Book Bible adjudication returned no terms list")

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
            raw_aliases = item.get("aliases")
            aliases = [
                a.strip()
                for a in (raw_aliases if isinstance(raw_aliases, list) else [])
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

        conventions: list[str] = []
        if include_conventions and isinstance(data.get("conventions"), list):
            for entry in data["conventions"]:
                if isinstance(entry, str) and entry.strip():
                    conventions.append(entry.strip()[:280])
                if len(conventions) >= 8:
                    break
        return out, conventions

    def _adjudicate(
        self,
        candidates: Sequence[tuple[BibleCandidate, str, str, bool]],
        *,
        book_synopsis: str,
        style: str,
        existing_terms: Sequence[GlossaryTerm],
        chunk_size: int = 55,
    ) -> list[GlossaryTerm]:
        """Settle all kept candidates, chunking large sets safely so output never truncates."""
        terms, _conventions = self._adjudicate_with_conventions(
            candidates,
            book_synopsis=book_synopsis,
            style=style,
            existing_terms=existing_terms,
            chunk_size=chunk_size,
        )
        return terms

    def _adjudicate_with_conventions(
        self,
        candidates: Sequence[tuple[BibleCandidate, str, str, bool]],
        *,
        book_synopsis: str,
        style: str,
        existing_terms: Sequence[GlossaryTerm],
        chunk_size: int = 55,
        divergent_titles: Sequence[tuple[BibleCandidate, str, str]] = (),
    ) -> tuple[list[GlossaryTerm], tuple[str, ...]]:
        """Settle all kept candidates and compile book-wide non-glossary conventions."""
        if not candidates and not divergent_titles:
            return [], ()
        all_terms: list[GlossaryTerm] = []
        conventions: list[str] = []
        anchors = list(existing_terms)
        if not candidates:
            _terms, chunk_convs = self._adjudicate_chunk(
                (),
                book_synopsis=book_synopsis,
                style=style,
                existing_terms=anchors,
                include_conventions=True,
                divergent_titles=divergent_titles,
            )
            return [], tuple(chunk_convs[:8])
        step = len(candidates) if len(candidates) <= 55 else min(chunk_size, 45)
        for offset in range(0, len(candidates), step):
            chunk = candidates[offset : offset + step]
            chunk_terms, chunk_convs = self._adjudicate_chunk(
                chunk,
                book_synopsis=book_synopsis,
                style=style,
                existing_terms=anchors,
                include_conventions=(offset == 0),
                divergent_titles=divergent_titles if offset == 0 else (),
            )
            all_terms.extend(chunk_terms)
            anchors.extend(chunk_terms)
            if chunk_convs:
                conventions.extend(chunk_convs)
        return all_terms, tuple(conventions[:8])

    def compile_voice_exemplars(
        self,
        excerpts: Sequence[str],
        *,
        terms: Sequence[GlossaryTerm],
        style: str = "",
        conventions: Sequence[str] = (),
    ) -> tuple[tuple[str, str], ...]:
        """Compile 2-3 gold-standard micro-excerpts to pin voice, register and punctuation across waves."""
        items = [e.strip() for e in excerpts if e and e.strip()][:3]
        if len(items) < 2:
            return ()
        glossary_lines = "\n".join(f"- {t.source} -> {t.target}" for t in terms[:60])
        conv_block = "\n".join(f"- {c}" for c in conventions)
        prompt_vals = dict(self.language_policy.prompt_values)
        punct_rule = prompt_vals.get("punct_rule", "")
        lines = [f"{i}. {span}" for i, span in enumerate(items)]
        system = (
            f"You are performing pre-translation voice calibration ({self.src} -> {self.tgt}). "
            "Translate each short representative excerpt from this work into publication-grade "
            f"{self.tgt} prose that establishes the benchmark voice, sentence rhythm, dialogue cadence, "
            "and punctuation discipline for every parallel chapter to follow. "
            "Follow [Style guide], [Book conventions], and [Glossary] strictly. "
            f"{punct_rule}\n"
            'Return ONLY JSON: {"exemplars": {"0": "<gold translation>", ...}}.'
        )
        user = (
            f"[Style guide]\n{style or '(none)'}\n\n"
            + (f"[Book conventions]\n{conv_block}\n\n" if conv_block else "")
            + f"[Glossary]\n{glossary_lines or '(none)'}\n\n"
            + "[Representative source excerpts]\n"
            + "\n".join(lines)
            + '\n\nReturn ONLY JSON: {"exemplars": {"0": "..."}}'
        )
        try:
            data = self._complete_json_dict(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                operation="translation.body",
                thinking_boost=1,
            )
        except Exception as exc:  # noqa: BLE001 - optional voice anchor; chapters still translate.
            if is_resumable_provider_interrupt(exc):
                raise
            _LOGGER.warning("Compiling voice exemplars failed (%s); skipping", exc)
            return ()
        raw_map = data.get("exemplars") if isinstance(data, dict) else None
        if not isinstance(raw_map, dict):
            return ()
        paired: list[tuple[str, str]] = []
        for idx, src in enumerate(items):
            val = raw_map.get(str(idx))
            if not isinstance(val, str):
                val = raw_map.get(src)
            if isinstance(val, str) and val.strip():
                paired.append((src, val.strip()))
        return tuple(paired)

    def pretranslate_quotes(
        self,
        spans: Sequence[str],
        *,
        terms: Sequence[GlossaryTerm],
        style: str = "",
        conventions: Sequence[str] = (),
    ) -> dict[str, str]:
        """Pre-render cross-chapter repeated verbatim quotes/epigraphs once for parallel stability."""
        items = [s.strip() for s in spans if s and s.strip()][:15]
        if not items:
            return {}
        glossary_lines = "\n".join(f"- {t.source} -> {t.target}" for t in terms[:60])
        conv_block = "\n".join(f"- {c}" for c in conventions)
        lines = [f"{i}. {span}" for i, span in enumerate(items)]
        system = (
            f"You are a literary translator rendering recurring book quotations, inscriptions, and "
            f"verse passages ({self.src} -> {self.tgt}) into canonical target phrasing that every "
            "chapter will reuse verbatim. Format multi-clause verse with explicit '\\n' line breaks.\n"
            'Return ONLY JSON: {"quotes": {"0": "<canonical translation>", ...}}.'
        )
        user = (
            f"[Style guide]\n{style or '(none)'}\n\n"
            + (f"[Book conventions]\n{conv_block}\n\n" if conv_block else "")
            + f"[Glossary]\n{glossary_lines or '(none)'}\n\n"
            + "[Recurring quotations]\n"
            + "\n".join(lines)
            + '\n\nReturn JSON: {"quotes": {"0": "..."}}'
        )
        try:
            data = self._complete_json_dict(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                operation="glossary.extract",
            )
        except Exception as exc:  # noqa: BLE001 - optional optimization; chapters still translate.
            if is_resumable_provider_interrupt(exc):
                raise
            _LOGGER.warning("Pre-translating repeated quotes failed (%s); skipping", exc)
            return {}
        raw_quotes = data.get("quotes") if isinstance(data, dict) else None
        if not isinstance(raw_quotes, dict):
            return {}
        rendered: dict[str, str] = {}
        for idx, span in enumerate(items):
            val = raw_quotes.get(str(idx))
            if not isinstance(val, str):
                val = raw_quotes.get(span)
            if isinstance(val, str) and val.strip():
                rendered[span] = val.strip()
        return rendered

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
                terms=(),
                variants_by_source={},
                candidate_count=0,
                divergent_count=0,
                conventions=(),
            )

        # Dual-context entropy probe: snippet A and snippet B in two separate calls so neither
        # biases the other.
        probe_a = self._probe_snippets([(c.source, c.snippet_a) for c in candidates])
        probe_b = self._probe_snippets([(c.source, c.snippet_b) for c in candidates])

        generic_words = glossary_generic_target_words(self.tgt)
        to_adjudicate: list[tuple[BibleCandidate, str, str, bool]] = []
        divergent_generic: list[tuple[BibleCandidate, str, str]] = []
        variants_by_source: dict[str, tuple[str, ...]] = {}
        divergent_count = 0
        for cand in candidates:
            ra = probe_a.get(cand.source, "").strip()
            rb = probe_b.get(cand.source, "").strip()
            if not ra and not rb:
                continue
            ra_gen = not ra or _is_generic_target(ra, generic_words)
            rb_gen = not rb or _is_generic_target(rb, generic_words)
            if ra_gen and rb_gen:
                # Keep high-frequency generic titles/offices (e.g. 国主 -> King/sovereign) for
                # book-wide capitalization & lexical conventions even when the two probes agree.
                if (
                    ra
                    and rb
                    and not _is_bare_pronoun_target(ra)
                    and not _is_bare_pronoun_target(rb)
                    and (
                        ((cand.count >= 4 or cand.from_digest) and not equivalent_targets(ra, rb))
                        or cand.count >= 6
                    )
                ):
                    divergent_generic.append((cand, ra, rb))
                continue
            divergent = bool(not ra or not rb or not equivalent_targets(ra, rb))
            if divergent:
                divergent_count += 1
                # Also surface half-generic divergent titles (e.g. 'the sovereign' vs 'the king')
                # into the convention title ledger so they get locked even if excluded from `terms`.
                if ra and rb and (ra_gen or rb_gen) and (cand.count >= 4 or cand.from_digest):
                    divergent_generic.append((cand, ra, rb))
                    continue
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

        divergent_generic.sort(key=lambda item: (-item[0].count, item[0].source))
        terms, conventions = self._adjudicate_with_conventions(
            to_adjudicate,
            book_synopsis=book_synopsis,
            style=style,
            existing_terms=existing_terms,
            divergent_titles=divergent_generic[:10],
        )
        kept_term_sources = {normalize_match_text(t.source).strip() for t in terms}
        watched_titles: list[tuple[str, tuple[str, ...]]] = [
            (cand.source, tuple(dict.fromkeys((ra, rb)))) for cand, ra, rb in divergent_generic[:10]
        ]
        watched_seen = {normalize_match_text(src).strip() for src, _ in watched_titles}
        # Any high-frequency divergent candidate excluded from `terms` (e.g. recurring offices like 状元)
        # is still watched by ConsistencySweepService for cross-chapter drift.
        for cand, ra, rb, divergent in to_adjudicate:
            norm = normalize_match_text(cand.source).strip()
            if (
                divergent
                and ra
                and rb
                and cand.count >= 4
                and norm not in kept_term_sources
                and norm not in watched_seen
            ):
                watched_titles.append((cand.source, tuple(dict.fromkeys((ra, rb)))))
                watched_seen.add(norm)
        divergent_title_pairs = tuple(watched_titles[:15])
        return BibleResult(
            terms=tuple(terms),
            variants_by_source=variants_by_source,
            candidate_count=len(candidates),
            divergent_count=divergent_count,
            conventions=conventions,
            divergent_titles=divergent_title_pairs,
        )

    def _complete_json_dict(
        self,
        messages,
        *,
        operation: str,
        thinking_boost: int = 0,
    ) -> dict:
        data, _raw = self._complete_json_turn(
            messages,
            operation=operation,
            thinking_boost=thinking_boost,
        )
        if isinstance(data, dict):
            return data
        raise JsonParseError(f"Expected JSON object from {operation}")
