"""Zero-cost deterministic draft-anomaly pre-screener and batch risk analyzer.

Inspects aligned ``(source, draft)`` pairs before ``SparseSemanticEditor`` runs to:
1. Pinpoint high-confidence paragraph anomalies (glossary misses, cross-script residue,
   omission/gloss-bloat outliers, unlineated standalone or embedded verse, and missing-marker
   semantic traps) so the sparse editor focuses directly on suspect paragraphs.
2. Decide whether the batch merits a provider-safe ``thinking_boost`` step up the
   reasoning ladder (e.g. ``low -> medium`` or ``low -> high``), concentrating extra compute
   only on high-risk batches while keeping routine prose fast and cheap.

Pure functions only: zero LLM calls, zero pipeline or storage imports.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache

from ..glossary.store import STATUS_OK, normalize_match_text, source_matches_normalized
from ..i18n.languages import glossary_generic_target_words
from ..ingest.segmenter import verse_block_indices
from ..models.glossary import GlossaryTerm

_PROTECTED_RE = re.compile(
    r"```[^\n]*\n.*?```|`[^`\n]+`|\$\$[\s\S]+?\$\$|\$[^$\n]+?\$|〘object:[A-Za-z0-9_.:-]+〙|⟦[^⟧]+⟧",
    re.DOTALL,
)
_CJK_CHAR_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_KANA_CHAR_RE = re.compile(r"[\u3040-\u30ff]")
_FURIGANA_RESIDUE_RE = re.compile(r"〘(?!object:)[^〙]+〙")
_WORD_RE = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*")
_PAREN_GLOSS_RE = re.compile(r"\((?:lit\.|literally|i\.e\.|meaning|a |the )?[^()]{4,60}\)")
_HALFWIDTH_IN_CJK_RE = re.compile(r"[\u4e00-\u9fff][,?!;:][\u4e00-\u9fff]")

# Embedded classical/literary poem/lyric signals inside a prose paragraph.
_EMBEDDED_ZH_VERSE_RE = re.compile(
    r"(?:《(?:破阵子|破陣子|鹊桥仙|鵲橋仙|西江月|沁园春|沁園春|水调歌头|水調歌頭|满江红|滿江紅|"
    r"念奴娇|念奴嬌|虞美人|菩萨蛮|菩薩蠻|临江仙|臨江仙|蝶恋花|蝶戀花|思帝乡|思帝鄉|清平乐|清平樂|"
    r"声声慢|聲聲慢|如梦令|如夢令|浪淘沙|江城子|青玉案|定风波|定風波)》|"
    r"(?:诗曰|詩曰|词曰|詞曰|偈曰|赞曰|讚曰|歌曰|有诗为证|有詩為證|吟道|唱道)[：:，,]?\s*[「『“\"])"
)
_EMBEDDED_JA_VERSE_RE = re.compile(
    r"(?:短歌|俳句|和歌|詠んだ|歌った|記されていた)[^「『]{0,24}[「『][^」』]{12,60}[」』]"
)
_JA_TANKA_QUOTE_RE = re.compile(
    r"[「『][^」』\n]{3,10}[\s　]+[^」』\n]{4,10}[\s　]+[^」』\n]{3,10}[」』]"
)

# High-precision missing-marker semantic traps (zh -> en).
# Explicit conditional introducers (如果/要是/假如/倘若/只要/万一/一旦 + optional short subject,
# or 若不) always fire without the bare-不 exclusion list. Bare 不…，就 fires only at a
# sentence/quote/ellipsis/paren/line start and uses a conservative heuristic exclusion list
# for common non-conditional temporal/modal 不-compounds (also intentionally excluding
# 不是…，就是 disjunctions). None of the spans may cross clause/quote/line boundaries.
_ZH_COND_SPAN = r"[^，,。！？”」』’）)\"'；;：:\n]"
_ZH_CONDITIONAL_RE = re.compile(
    rf"(?:如果|要是|假如|倘若|只要|万一|萬一|一旦){_ZH_COND_SPAN}{{0,4}}不{_ZH_COND_SPAN}{{1,18}}[，,](?:就|便|则|則)"
    rf"|若不{_ZH_COND_SPAN}{{1,18}}[，,](?:就|便|则|則)"
    rf"|(?:^|[。！？；;：:“「『‘（(…\"'\n])不"
    r"(?!到|等|久|管|论|論|仅|僅|但|过|過|禁|由|免|料|愧|顾|顧|待|及|必|用|需要|知|清楚|敢|会|會|可能|太|是|能因|代表|一会|一會|片刻|远|遠|得不|幸|多时|多時|多久|一时|一時|大一会|大一會|消|出|经意|經意|得已)"
    rf"{_ZH_COND_SPAN}{{2,18}}[，,]就"
    rf"|若非|如若不然|若是{_ZH_COND_SPAN}{{2,16}}[，,](?:便|就|则|則)"
)
_EN_CONDITIONAL_MARKERS = re.compile(
    r"\b(?:if|unless|otherwise|or else|then|either|were it not)\b", re.I
)

_ZH_DELAYED_CAI_RE = re.compile(
    r"(?:良久|半晌|许久|許久|过了许久|過了許久|过了半晌)[，,]?[^。！？]{0,16}才"
)
_EN_DELAYED_MARKERS = re.compile(r"\b(?:only|until|at last|finally|before)\b", re.I)

_ZH_NEG_IMPERATIVE_RE = re.compile(
    r"(?:莫|切莫|休|切勿)[^，。！？]{1,16}(?:只管|只顾|只顧|尽管|儘管)"
)
_EN_AFFIRM_IMPERATIVE_TRAP = re.compile(r"\b(?:just\s+(?:keep\s+)?|simply\s+keep)\b", re.I)

_ZH_ROYAL_GU_COMPOUNDS = re.compile(r"孤[儿兒身城单單独獨立零苦舟寂影芳鸿鴻雁陋陋癖军軍立]")
_EN_LONE_TRAP = re.compile(r"\b(?:the\s+)?lone(?:\s+one)?\b", re.I)

_ZH_CLASSICAL_GU_RE = re.compile(r"[，,；;。]\s*故[\u4e00-\u9fff]{1,6}[之矣也焉。，]")
_EN_DELIBERATELY_TRAP = re.compile(r"\b(?:deliberately|on purpose|intentionally)\b", re.I)

_EN_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_EN_QUOTED_SPAN_RE = re.compile(r'"[^"]*"|“[^”]*”')
_EN_MALE_PRONOUN_RE = re.compile(r"\b(?:he|him|his)\b", re.I)
_EN_FEMALE_PRONOUN_RE = re.compile(r"\b(?:she|her|hers)\b", re.I)
_EN_MID_CAP_NAME_RE = re.compile(r"(?<!^)(?<![.!?]\s)\b[A-Z][a-z]{2,}\b")
_ZH_DISGUISE_CUE_RE = re.compile(
    r"(?:乔装|喬裝|女扮男装|女扮男裝|男扮女装|男扮女裝|易容|化装|化裝|伪装|偽裝|扮作)"
)
_ZH_QUOTED_SPAN_RE = re.compile(r"“[^”]*”|「[^」]*」|『[^』]*』")

_ZH_AT_LEAST_RE = re.compile(r"至少")
_EN_AT_MOST_TRAP = re.compile(r"\b(?:no\s+more\s+than|at\s+most)\b", re.I)
_ZH_INLAW_KINSHIP_RE = re.compile(r"(?:大姨|二姨|三姨|小姨|大舅子|小舅子)")
_EN_AUNT_UNCLE_TRAP = re.compile(r"\b(?:aunts?|uncles?)\b", re.I)
_EN_CALQUE_SENTINEL_RE = re.compile(
    r"\b(?:long\s+live\s+the\s+ten\s+thousand|ten\s+thousand\s+years|the\s+sir)\b",
    re.I,
)
_ZH_CLASSICAL_PARTICLES_RE = re.compile(r"[之乎者也矣焉哉岂豈孰乃遂故既若非莫勿]")


@dataclass(frozen=True)
class ParagraphAlert:
    index: int
    code: str
    detail: str
    high_severity: bool = True


@dataclass(frozen=True)
class BatchRiskReport:
    alerts: tuple[ParagraphAlert, ...]
    should_escalate: bool
    reasons: tuple[str, ...]

    def format_prompt_alerts(self, *, max_items: int = 6) -> str:
        """Render a compact, bounded pre-screen hint block for ``SparseSemanticEditor``."""
        if not self.alerts:
            return ""
        # Prioritize high-severity alerts first, keeping original paragraph order within ties.
        ranked = sorted(self.alerts, key=lambda a: (not a.high_severity, a.index))[:max_items]
        ranked.sort(key=lambda a: a.index)
        lines = ["[Pre-screen alerts — verify these specific paragraphs carefully before deciding]"]
        for alert in ranked:
            lines.append(f"- [{alert.index}] ({alert.code}): {alert.detail}")
        return "\n".join(lines)


def _strip_protected(text: str) -> str:
    return _PROTECTED_RE.sub(" ", text)


def _is_cjk_lang(lang: str) -> bool:
    base = (lang or "").lower().split("-", 1)[0]
    return base in {"zh", "ja", "ko"}


def _is_latin_lang(lang: str) -> bool:
    base = (lang or "").lower().split("-", 1)[0]
    return base in {"en", "fr", "de", "es", "it", "pt", "vi"}


@lru_cache(maxsize=1 << 14)
def _normalized_canonical(canonical: str) -> str:
    """Whitespace-collapsed normalized form of a glossary canonical target.

    Keyed only on bounded glossary vocabulary strings (never paragraph text).
    """
    return " ".join(normalize_match_text(canonical).split())


@lru_cache(maxsize=1 << 14)
def _normalized_target_strip(target: str) -> str:
    """Normalized, edge-stripped target string cached for phantom overlap checks."""
    return normalize_match_text(target).strip()


def _en_plural_body(form: str) -> str | None:
    """Return a regex body for ``form`` tolerating common English ``-s/-es/-ies`` plurals.

    Only the final word is inflected, and only when it is pure ASCII letters of length >= 3:
    consonant+y alternates y/ies ("City" -> "Cities"), ``-ch`` accepts ``-s`` or ``-es``
    ("Church" -> "Churches", "Monarch" -> "Monarchs"), sibilant endings (s/x/z/sh) take an
    optional "es" ("Glass" -> "Glasses"), and other words take an optional "s"
    ("Empire" -> "Empires"; vowel+y words like "Key" become "Keys"). Irregular plurals
    (e.g. ``Hero`` -> ``Heroes``, ``Quiz`` -> ``Quizzes``, ``-f/-ves``) are intentionally not
    guessed. Word boundaries stay with the caller, so "Ann" never matches "Anna" and "War"
    never matches "Ward".
    """
    words = form.split()
    if not words:
        return None
    final = words[-1]
    if len(final) < 3 or not final.isascii() or not final.isalpha():
        return None
    if final.endswith("y") and final[-2] not in "aeiou":
        tail = rf"{re.escape(final[:-1])}(?:y|ies)"
    elif final.endswith("ch"):
        tail = rf"{re.escape(final)}(?:e?s)?"
    elif final.endswith(("s", "x", "z", "sh")):
        tail = rf"{re.escape(final)}(?:es)?"
    else:
        tail = rf"{re.escape(final)}s?"
    head = " ".join(re.escape(w) for w in words[:-1])
    return f"{head} {tail}" if head else tail


def _latin_term_body(form: str, tgt_base: str, *, allow_plural: bool = True) -> str:
    """Regex body for one normalized canonical form; English optionally adds plural inflection."""
    if tgt_base == "en" and allow_plural:
        body = _en_plural_body(form)
        if body is not None:
            return body
    return re.escape(form)


@lru_cache(maxsize=1 << 14)
def _target_term_patterns(
    canonical: str, tgt_base: str, allow_plural: bool = True
) -> tuple[re.Pattern[str], ...]:
    """Compile boundary-anchored Latin patterns for ``canonical`` once per target language.

    Covers the full normalized form plus its leading-article-stripped variant
    ("the Duke of North" -> "Duke of North"); English non-person targets additionally
    tolerate common ``-s/-es/-ies`` plural inflection of the final word.
    """
    norm_canon = _normalized_canonical(canonical)
    if not norm_canon:
        return ()
    forms = [norm_canon]
    stripped = re.sub(r"^(?:the|a|an)\s+", "", norm_canon, flags=re.I)
    if stripped and stripped != norm_canon:
        forms.append(stripped)
    return tuple(
        re.compile(
            rf"(?<![^\W\d_]){_latin_term_body(form, tgt_base, allow_plural=allow_plural)}(?![^\W\d_])",
            re.I,
        )
        for form in forms
    )


def _target_contains_term(
    target_clean: str,
    canonical: str,
    *,
    target_lang: str,
    norm_tgt: str | None = None,
    allow_plural: bool = True,
) -> bool:
    norm_canon = _normalized_canonical(canonical)
    if not norm_canon:
        return True
    if norm_tgt is None:
        norm_tgt = " ".join(normalize_match_text(target_clean).split())
    if _is_cjk_lang(target_lang):
        return norm_canon in norm_tgt
    tgt_base = (target_lang or "").lower().split("-", 1)[0]
    for pat in _target_term_patterns(canonical, tgt_base, allow_plural):
        if pat.search(norm_tgt):
            return True
    return False


_OPEN_QUOTES = frozenset("\"“‘'«‹「『([")
_CLOSE_QUOTES = frozenset("\"”’'»›」』)]")
_SENTENCE_END = frozenset(".!?…")
_INLINE_SPACE_RE = re.compile(r"[^\S\n]+")
_LEADING_ARTICLE_RE = re.compile(r"^(?:the|a|an)\s+", re.I)
_LEADING_THE_RE = re.compile(r"^the\s+", re.I)
_SINGLE_WORD_RE = re.compile(r"[^\W\d_]+")
_TITLE_ABBREVIATIONS = frozenset(
    {
        "mr",
        "mrs",
        "ms",
        "dr",
        "st",
        "sr",
        "jr",
        "prof",
        "rev",
        "gen",
        "col",
        "lt",
        "sgt",
        "capt",
        "hon",
        "rep",
        "sen",
        "gov",
    }
)
# Ordinary English words (>=4 chars) that double as given names/surnames. A capitalized
# single-word glossary target equal to one of these is ambiguous at sentence start.
_EN_NAME_WORD_COLLISIONS = frozenset(
    {
        "will",
        "hope",
        "white",
        "black",
        "brown",
        "green",
        "gray",
        "grey",
        "young",
        "rose",
        "mark",
        "bill",
        "frank",
        "grace",
        "faith",
        "june",
        "april",
        "august",
        "summer",
        "autumn",
        "winter",
        "dawn",
        "iris",
        "lily",
        "rich",
        "long",
        "king",
        "wood",
        "stone",
        "hill",
        "field",
        "bell",
        "miles",
        "chase",
        "hunter",
        "page",
        "price",
        "reed",
        "rush",
        "cook",
        "baker",
        "carter",
        "mason",
        "fisher",
        "porter",
        "ward",
        "wells",
        "star",
        "river",
        "storm",
        "wolf",
        "bear",
        "hawk",
        "swift",
        "patience",
        "honor",
        "honour",
        "victor",
        "sterling",
        "earl",
        "duke",
        "prince",
        "noble",
        "sunny",
        "golden",
        "silver",
    }
)


def _is_sentence_initial(text: str, start: int) -> bool:
    """Return True if ``text[start:]`` begins a sentence, line, or quoted utterance."""
    j = start
    while j > 0 and text[j - 1] in _OPEN_QUOTES:
        j -= 1
    if j < start:
        # Directly after an opening quote: an utterance start ("…asked, "Will you stay?"").
        return j == 0 or text[j - 1].isspace() or text[j - 1] in "—–-:"
    k = j
    while k > 0 and text[k - 1] == " ":
        k -= 1
    if k == 0 or text[k - 1] == "\n":
        return True
    if k == j:
        # NFKC expands '…' to '...', so check both raw ellipsis/dashes and '...'.
        return text[k - 1] in "…—–" or text[max(0, k - 3) : k] == "..."
    while k > 0 and text[k - 1] in _CLOSE_QUOTES:
        k -= 1
    if not (k > 0 and text[k - 1] in _SENTENCE_END):
        return False
    if text[k - 1] == ".":
        # Honorific abbreviations ("Mr. White", "Dr. Hope") are not sentence boundaries.
        word_end = k - 1
        word_start = word_end
        while word_start > 0 and text[word_start - 1].isalpha():
            word_start -= 1
        if text[word_start:word_end].casefold() in _TITLE_ABBREVIATIONS:
            return False
    return True


def _lowercase_words(drafts: Sequence[str]) -> frozenset[str]:
    """Collect casefolded words that appear lowercase-initial anywhere in ``drafts``."""
    return frozenset(
        w.casefold()
        for d in drafts
        for w in _WORD_RE.findall(unicodedata.normalize("NFKC", d))
        if w[:1].islower()
    )


@lru_cache(maxsize=1 << 14)
def _phantom_form_data(
    canonical: str, term_type: str
) -> tuple[tuple[re.Pattern[str], str, bool], ...] | None:
    """Compile ``_phantom_target_hit`` forms once per ``(canonical, term_type)``.

    Returns per-form ``(pattern, casefolded, is_single_word)`` so callers can apply the
    batch-dependent ambiguity rules (curated English collisions and words seen lowercase
    elsewhere) per hit without recompiling. Returns None for an effectively empty canonical.
    """
    canon = " ".join(unicodedata.normalize("NFKC", canonical).replace("’", "'").split())
    if not canon:
        return None
    forms = [canon]
    # Support sentence-initial capitalization of lowercase-particle names ("de Gaulle" -> "De Gaulle").
    if canon[:1].islower():
        forms.append(canon[:1].upper() + canon[1:])
    article_re = _LEADING_THE_RE if term_type == "person" else _LEADING_ARTICLE_RE
    stripped = article_re.sub("", canon)
    if stripped and stripped != canon and stripped not in forms:
        forms.append(stripped)
    entries = []
    for form in forms:
        folded = form.casefold()
        form_pattern = r"\s+".join(re.escape(part) for part in form.split())
        pat = re.compile(rf"(?<![^\W\d_]){form_pattern}(?![^\W\d_])")
        entries.append((pat, folded, bool(_SINGLE_WORD_RE.fullmatch(form))))
    return tuple(entries)


def _phantom_target_hit(
    target_clean: str,
    canonical: str,
    *,
    target_lang: str,
    lowercase_words: frozenset[str] = frozenset(),
    term_type: str = "",
    prepared_text: str | None = None,
    norm_tgt: str | None = None,
) -> bool:
    """Return True if ``target_clean`` really uses the proper noun ``canonical``.

    Stricter than :func:`_target_contains_term` (kept for canonical-miss checks) on
    Latin-script targets: matching is case-sensitive, so ordinary lowercase "will", "hope"
    or "white" never count as the names "Will", "Hope" or "White". A single-word target that
    is also an ordinary word (curated English collisions for ``en``, or a word written
    lowercase elsewhere in the batch) is ignored at sentence/utterance start, where
    capitalization is ambiguous; a mid-sentence capitalized use still counts. Distinctive
    names ("Jingxiang fell.") still match at sentence start. ``prepared_text`` and
    ``norm_tgt`` let batch callers pass pre-normalized forms of ``target_clean``.
    """
    if not _is_latin_lang(target_lang):
        return _target_contains_term(
            target_clean, canonical, target_lang=target_lang, norm_tgt=norm_tgt
        )
    tgt_base = (target_lang or "").lower().split("-", 1)[0]
    entries = _phantom_form_data(canonical, term_type)
    if entries is None:
        return False
    text = (
        prepared_text
        if prepared_text is not None
        else _INLINE_SPACE_RE.sub(
            " ", unicodedata.normalize("NFKC", target_clean).replace("’", "'")
        )
    )
    for pat, folded, is_single_word in entries:
        is_en_collision = tgt_base == "en" and folded in _EN_NAME_WORD_COLLISIONS
        ambiguous = is_single_word and (is_en_collision or folded in lowercase_words)
        for m in pat.finditer(text):
            if not ambiguous or not _is_sentence_initial(text, m.start()):
                return True
    return False


def _batch_target_prehit(
    canonical: str, batch_text: str, *, target_lang: str, term_type: str
) -> bool:
    """Cheap batch-level pre-filter: can ``canonical`` hit anywhere in ``batch_text``?

    ``batch_text`` must be the batch's target drafts pre-normalized the way the exact
    matcher normalizes single paragraphs, then casefolded with all whitespace (including
    the newlines joining paragraphs) collapsed to single spaces. Under that normalization
    a plain substring test is a sound superset of :func:`_phantom_target_hit`: every exact
    per-paragraph hit stays a contiguous substring, so no candidate is ever lost, while
    sentence-initial ambiguity and word boundaries are deliberately ignored (the exact
    matcher re-runs on surviving candidates).
    """
    if _is_latin_lang(target_lang):
        entries = _phantom_form_data(canonical, term_type)
        return entries is not None and any(folded in batch_text for _, folded, _ in entries)
    norm_canon = _normalized_canonical(canonical)
    if not norm_canon:
        return True
    if _is_cjk_lang(target_lang):
        return norm_canon in batch_text
    tgt_base = (target_lang or "").lower().split("-", 1)[0]
    return any(pat.search(batch_text) for pat in _target_term_patterns(canonical, tgt_base, True))


@lru_cache(maxsize=1 << 14)
def _is_generic(text: str, generic_words: frozenset[str]) -> bool:
    if not generic_words:
        return False
    words = _WORD_RE.findall(text.casefold())
    return bool(words) and all(w.replace("’", "'") in generic_words for w in words)


def _source_mentions_term_or_stem(
    term: GlossaryTerm,
    src: str,
    *,
    src_base: str,
    norm_src: str | None = None,
) -> bool:
    """Return True if ``src`` contains ``term.source``, an alias, or (for persons) a name stem.

    ``norm_src`` lets batch callers pass the pre-normalized source paragraph so matching
    does not re-normalize it for every term.
    """
    if norm_src is None:
        norm_src = normalize_match_text(src)
    if source_matches_normalized(term.source, norm_src):
        return True
    if any(a and source_matches_normalized(a, norm_src) for a in term.aliases):
        return True
    if term.type != "person":
        return False
    clean_term = term.source.strip()
    if src_base in {"zh", "ja"}:
        # For Chinese/Japanese personal names, allow bare given-name/surname mentions without
        # triggering false phantom alerts (e.g. '林' for '林青' or '明远' for '陈明远').
        if len(clean_term) >= 2:
            for i in range(len(clean_term) - 1):
                if clean_term[i : i + 2] in src:
                    return True
            if clean_term[0] in src or clean_term[-1] in src:
                return True
    else:
        for tok in _WORD_RE.findall(clean_term):
            if len(tok) >= 4 and re.search(rf"\b{re.escape(tok)}\b", src, re.I):
                return True
    return False


def _has_suspicious_pronoun_flip(src: str, clean_drf: str, *, src_base: str, tgt_base: str) -> bool:
    """Detect mid-sentence masculine<->feminine pronoun flips in disguise/concealment scenes."""
    if tgt_base != "en":
        return False
    has_disguise_cue = (src_base == "zh" and bool(_ZH_DISGUISE_CUE_RE.search(src))) or (
        src_base == "ja" and any(k in src for k in ("男装", "女装", "変装", "正体"))
    )
    if not has_disguise_cue:
        return False
    narration = _EN_QUOTED_SPAN_RE.sub(" ", clean_drf)
    for sent in _EN_SENTENCE_SPLIT_RE.split(narration):
        sent = sent.strip()
        if sent and _EN_MALE_PRONOUN_RE.search(sent) and _EN_FEMALE_PRONOUN_RE.search(sent):
            return True
    return False


def has_embedded_verse(source: str, *, source_lang: str) -> bool:
    """Detect poems/lyrics embedded inside a longer prose/dialogue paragraph."""
    base = (source_lang or "").lower().split("-", 1)[0]
    if base == "zh":
        return bool(_EMBEDDED_ZH_VERSE_RE.search(source))
    if base == "ja":
        return bool(_EMBEDDED_JA_VERSE_RE.search(source) or _JA_TANKA_QUOTE_RE.search(source))
    return False


def assess_batch_risk(
    sources: Sequence[str],
    drafts: Sequence[str],
    *,
    source_lang: str,
    target_lang: str,
    glossary_terms: Sequence[GlossaryTerm] = (),
    variants_by_source: Mapping[str, Sequence[str]] | None = None,
) -> BatchRiskReport:
    """Scan ``(sources, drafts)`` for concrete defects and high-risk semantic complexity."""
    alerts: list[ParagraphAlert] = []
    reasons: list[str] = []
    src_base = (source_lang or "").lower().split("-", 1)[0]
    tgt_base = (target_lang or "").lower().split("-", 1)[0]
    generic_words = glossary_generic_target_words(target_lang)
    variants_map = variants_by_source or {}

    # Filter glossary terms to high-signal entity mappings for miss detection.
    entity_terms = [
        t
        for t in glossary_terms
        if t.status == STATUS_OK
        and t.type in {"person", "place", "organization", "term"}
        and len(t.source.strip()) >= 2
        and len(t.target.strip()) >= 2
        and not _is_generic(t.target, generic_words)
    ]

    # Strip protected markup once per draft so batch_lowercase_words and paragraph checks
    # share the result, then pre-normalize each non-empty pair once.
    para_srcs = [raw_src.strip() for raw_src in sources]
    para_drfs = [raw_drf.strip() for raw_drf in drafts]
    stripped_drfs = [_strip_protected(d) if d else "" for d in para_drfs]

    batch_lowercase_words = (
        _lowercase_words(stripped_drfs)
        if _is_latin_lang(tgt_base) and entity_terms
        else frozenset()
    )

    clean_drfs: list[str] = []
    norm_srcs: list[str] = []
    norm_drfs: list[str] = []
    phantom_texts: list[str] = []
    for src, drf, clean_drf in zip(para_srcs, para_drfs, stripped_drfs):
        if not src or not drf:
            clean_drfs.append("")
            norm_srcs.append("")
            norm_drfs.append("")
            phantom_texts.append("")
            continue
        clean_drfs.append(clean_drf)
        norm_srcs.append(normalize_match_text(src))
        norm_drfs.append(" ".join(normalize_match_text(clean_drf).split()))
        phantom_texts.append(
            _INLINE_SPACE_RE.sub(" ", unicodedata.normalize("NFKC", clean_drf).replace("’", "'"))
        )

    # Pre-filter terms once per batch. "\n" is a non-word character, so word boundaries at
    # paragraph starts and ends stay valid in the joined text and CJK substring matches are
    # unaffected; a term with no batch-level hit can never fire in any single paragraph.
    batch_norm_src = "\n".join(norm_srcs)
    miss_candidates = [
        t for t in entity_terms if source_matches_normalized(t.source, batch_norm_src)
    ]
    phantom_candidates: list[tuple[GlossaryTerm, tuple[GlossaryTerm, ...]]] = []
    phantom_indices = [
        i
        for i, t in enumerate(entity_terms)
        if t.type in {"place", "organization", "person"} and len(t.target.strip()) >= 4
    ]
    if phantom_indices:
        # Casefold and collapse whitespace (including the paragraph-joining newlines) once
        # so the batch pre-filter needs only plain substring checks; any exact per-paragraph
        # hit stays a contiguous substring, so no candidate is lost.
        if _is_latin_lang(tgt_base):
            batch_tgt_text = " ".join("\n".join(phantom_texts).casefold().split())
        else:
            batch_tgt_text = " ".join("\n".join(norm_drfs).split())
        prehit_indices = [
            i
            for i in phantom_indices
            if _batch_target_prehit(
                entity_terms[i].target,
                batch_tgt_text,
                target_lang=target_lang,
                term_type=entity_terms[i].type,
            )
        ]
        if prehit_indices:
            norm_targets = [_normalized_target_strip(t.target) for t in entity_terms]
            for i in prehit_indices:
                t = entity_terms[i]
                # Skip if another glossary term with overlapping target wording actually matches
                # src; precompute the overlapping pairs once so the per-paragraph check only has
                # to test whether the overlapping term's source appears in that paragraph.
                norm_tgt_term = norm_targets[i]
                overlapping = tuple(
                    other
                    for other, other_norm in zip(entity_terms, norm_targets)
                    if other is not t
                    and (norm_tgt_term in other_norm or other_norm in norm_tgt_term)
                )
                phantom_candidates.append((t, overlapping))

    verse_indices = verse_block_indices(sources)
    unlineated_couplets: list[int] = []
    classical_dense_paras = 0
    dialogue_paras = 0
    high_sev_count = 0

    for idx, (src, drf) in enumerate(zip(para_srcs, para_drfs)):
        if not src or not drf:
            continue
        clean_drf = clean_drfs[idx]
        norm_src = norm_srcs[idx]
        norm_drf = norm_drfs[idx]
        para_alerts_before = len(alerts)

        # 1. Cross-script / furigana residue
        if _FURIGANA_RESIDUE_RE.search(clean_drf):
            alerts.append(
                ParagraphAlert(
                    idx,
                    "script_residue",
                    "Leaked furigana marker 〘...〙 in draft; remove markup.",
                    high_severity=True,
                )
            )
        elif _is_latin_lang(tgt_base) and (
            _CJK_CHAR_RE.search(clean_drf) or _KANA_CHAR_RE.search(clean_drf)
        ):
            alerts.append(
                ParagraphAlert(
                    idx,
                    "script_residue",
                    "Untranslated CJK/kana characters remain in target draft.",
                    high_severity=True,
                )
            )
        elif tgt_base == "zh" and _HALFWIDTH_IN_CJK_RE.search(clean_drf):
            alerts.append(
                ParagraphAlert(
                    idx,
                    "punctuation",
                    "Half-width ASCII punctuation inside Chinese prose.",
                    high_severity=False,
                )
            )

        # 2. Unrequested parenthetical gloss (when source has no parentheses)
        if "(" not in src and "（" not in src and _PAREN_GLOSS_RE.search(clean_drf):
            alerts.append(
                ParagraphAlert(
                    idx,
                    "inline_gloss",
                    "Draft inserted a parenthetical gloss absent from source; remove or integrate smoothly.",
                    high_severity=True,
                )
            )

        # 3. Calibrated length-ratio omission check (only on substantial paragraphs)
        if src_base == "zh" and tgt_base == "en" and len(src) >= 45:
            words = len(_WORD_RE.findall(clean_drf))
            # Typical zh->en is ~0.55–1.10 English words per Chinese char; <0.30 signals omission.
            if words < max(8, int(len(src) * 0.28)):
                alerts.append(
                    ParagraphAlert(
                        idx,
                        "possible_omission",
                        "Draft looks unusually short vs. source; verify no clause or name was dropped.",
                        high_severity=True,
                    )
                )
        elif src_base == "en" and tgt_base == "zh":
            src_words = len(_WORD_RE.findall(src))
            zh_chars = len(_CJK_CHAR_RE.findall(clean_drf))
            if src_words >= 30 and zh_chars < int(src_words * 0.65):
                alerts.append(
                    ParagraphAlert(
                        idx,
                        "possible_omission",
                        "Chinese draft looks unusually short vs. English source; verify no clause was dropped.",
                        high_severity=True,
                    )
                )

        # 4. Standalone & embedded verse lineation
        if idx in verse_indices and "\n" not in clean_drf:
            unlineated_couplets.append(idx)
        elif has_embedded_verse(src, source_lang=source_lang) and "\n" not in clean_drf:
            alerts.append(
                ParagraphAlert(
                    idx,
                    "embedded_verse",
                    "Contains an embedded poem/lyric rendered inline; format the verse with explicit '\\n' line breaks.",
                    high_severity=True,
                )
            )

        # 5. Glossary canonical miss (cap at 1 alert per paragraph to avoid noise)
        for term in miss_candidates:
            if not source_matches_normalized(term.source, norm_src):
                continue
            if _target_contains_term(
                clean_drf,
                term.target,
                target_lang=target_lang,
                norm_tgt=norm_drf,
                allow_plural=term.type != "person",
            ):
                continue
            # Avoid false positives when a longer glossary term covering term.source is matched.
            if any(
                other.source != term.source
                and term.source in other.source
                and source_matches_normalized(other.source, norm_src)
                and _target_contains_term(
                    clean_drf,
                    other.target,
                    target_lang=target_lang,
                    norm_tgt=norm_drf,
                    allow_plural=other.type != "person",
                )
                for other in miss_candidates
            ):
                continue
            variant_note = ""
            known_vars = variants_map.get(term.source, ())
            if any(v and v in clean_drf for v in known_vars):
                variant_note = " (known variant drift detected)"
            alerts.append(
                ParagraphAlert(
                    idx,
                    "glossary_miss",
                    f"Source contains '{term.source}' (canonical: '{term.target}'){variant_note}; verify consistent rendering.",
                    high_severity=True,
                )
            )
            break

        # 5b. Phantom entity / reverse-glossary substitution check (language-agnostic):
        # Catch when a familiar glossary place/organization/person appears in the draft even
        # though its source term does NOT appear in this source paragraph (e.g. substituting one city name for another).
        for term, overlapping in phantom_candidates:
            if not _phantom_target_hit(
                clean_drf,
                term.target,
                target_lang=target_lang,
                lowercase_words=batch_lowercase_words,
                term_type=term.type,
                prepared_text=phantom_texts[idx],
                norm_tgt=norm_drf,
            ):
                continue
            if _source_mentions_term_or_stem(term, src, src_base=src_base, norm_src=norm_src):
                continue
            # Skip if another glossary term with overlapping target wording actually matches src.
            if any(
                _source_mentions_term_or_stem(other, src, src_base=src_base, norm_src=norm_src)
                for other in overlapping
            ):
                continue
            alerts.append(
                ParagraphAlert(
                    idx,
                    "phantom_entity",
                    f"Draft uses glossary {term.type} '{term.target}' ('{term.source}'), which does not "
                    "appear in this source paragraph; verify no different proper noun/place was substituted.",
                    high_severity=True,
                )
            )
            break

        # 5c. Intra-sentence pronoun-gender consistency check (e.g. disguise scenes).
        if _has_suspicious_pronoun_flip(src, clean_drf, src_base=src_base, tgt_base=tgt_base):
            alerts.append(
                ParagraphAlert(
                    idx,
                    "pronoun_consistency",
                    "Narration mixes masculine and feminine pronouns within one sentence; verify "
                    "whether a single character's pronouns flipped mid-sentence and keep them consistent.",
                    high_severity=True,
                )
            )

        # 6. High-precision missing-marker semantic traps (zh -> en)
        if src_base == "zh" and tgt_base == "en":
            if _ZH_CONDITIONAL_RE.search(src) and not _EN_CONDITIONAL_MARKERS.search(clean_drf):
                alerts.append(
                    ParagraphAlert(
                        idx,
                        "conditional_logic",
                        "Source has a correlative conditional (不…就 / 若非 / 若…则) but draft lacks conditional phrasing.",
                        high_severity=True,
                    )
                )
            if _ZH_DELAYED_CAI_RE.search(src) and not _EN_DELAYED_MARKERS.search(clean_drf):
                alerts.append(
                    ParagraphAlert(
                        idx,
                        "temporal_logic",
                        "Source has delayed realization (良久/半晌…才 = 'only after a long while did…'); verify timing.",
                        high_severity=True,
                    )
                )
            if _ZH_NEG_IMPERATIVE_RE.search(src) and _EN_AFFIRM_IMPERATIVE_TRAP.search(clean_drf):
                alerts.append(
                    ParagraphAlert(
                        idx,
                        "negation_scope",
                        "Source has negated imperative (莫…只管…); verify negation scope wasn't flipped affirmative.",
                        high_severity=True,
                    )
                )
            if (
                "孤" in src
                and not _ZH_ROYAL_GU_COMPOUNDS.search(src)
                and _EN_LONE_TRAP.search(clean_drf)
            ):
                alerts.append(
                    ParagraphAlert(
                        idx,
                        "royal_pronoun",
                        "Source uses royal self-reference '孤' but draft says 'lone'; render naturally as 'I'/'we'.",
                        high_severity=True,
                    )
                )
            if _ZH_CLASSICAL_GU_RE.search(src) and _EN_DELIBERATELY_TRAP.search(clean_drf):
                alerts.append(
                    ParagraphAlert(
                        idx,
                        "classical_logic",
                        "Classical '故' here may mean 'therefore/so' rather than 'deliberately'; verify.",
                        high_severity=True,
                    )
                )
            if (
                _ZH_AT_LEAST_RE.search(src)
                and _EN_AT_MOST_TRAP.search(clean_drf)
                and "at least" not in clean_drf.lower()
            ):
                alerts.append(
                    ParagraphAlert(
                        idx,
                        "quantifier_inversion",
                        "Source says '至少' ('at least') but draft says 'no more than / at most'; fix inverted polarity.",
                        high_severity=True,
                    )
                )
            if (
                _ZH_INLAW_KINSHIP_RE.search(src)
                and _EN_AUNT_UNCLE_TRAP.search(clean_drf)
                and "in-law" not in clean_drf.lower()
            ):
                alerts.append(
                    ParagraphAlert(
                        idx,
                        "kinship_relation",
                        "Verify in-law kinship (大姨/二姨/小舅子 from a spouse's perspective = sister-in-law/brother-in-law, not aunt/uncle).",
                        high_severity=True,
                    )
                )
            if _EN_CALQUE_SENTINEL_RE.search(clean_drf):
                alerts.append(
                    ParagraphAlert(
                        idx,
                        "calque_or_idiom",
                        "Possible literalized register calque detected; verify against source.",
                        high_severity=True,
                    )
                )
            if len(_ZH_CLASSICAL_PARTICLES_RE.findall(src)) >= 4:
                classical_dense_paras += 1

        if any(q in src for q in ('"', "“", "「", "『")):
            dialogue_paras += 1
        if any(a.high_severity for a in alerts[para_alerts_before:]):
            high_sev_count += 1

    if unlineated_couplets:
        alerts.append(
            ParagraphAlert(
                unlineated_couplets[0],
                "unlineated_verse",
                f"Paragraphs {unlineated_couplets} are verse lines drafted without '\\n' line breaks.",
                high_severity=False,
            )
        )

    # Escalation rule: escalate ONLY when concrete semantic/glossary/omission/embedded-verse
    # anomalies fire OR the batch is unusually dense in classical/literary logic + dialogue.
    should_escalate = False
    if high_sev_count >= 1:
        should_escalate = True
        reasons.append(f"high_sev_alerts:{high_sev_count}")
    elif classical_dense_paras >= 3 and dialogue_paras >= 3:
        should_escalate = True
        reasons.append(f"classical_debate:{classical_dense_paras}")

    return BatchRiskReport(
        alerts=tuple(alerts),
        should_escalate=should_escalate,
        reasons=tuple(reasons),
    )
