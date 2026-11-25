"""Fast post-parallel book-wide consistency sweep (10-20s vs. 33m legacy Review).

Closes the residual cross-chapter consistency gap of parallel chapter translation with:
1. Book-wide glossary-miss detection across chapters, repaired via surgical sparse patches.
2. Mid-frequency non-glossary term drift detection (statistical co-occurrence across
   chapters to spot split renderings like 'retainer'/'adviser' or 'sovereign'/'king'),
   adjudicated in one tiny call and repaired via surgical sparse patches.

Repeated source paragraphs are never overwritten with an earlier chapter's translation:
the same words can need a different rendering in a different scene (speaker, gender,
tense). Book-wide quote memory reaches the drafting model as a prompt reference instead.

Never mutates segments with offset-bound annotation/protected-object/DOCX-style placements.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ...agents.batch_risk import _is_generic, _target_contains_term
from ...agents.book_bible import _CJK_EDGE_PARTICLES, _CJK_STOP_CHARS
from ...agents.sparse_editor import SparseSemanticEditor
from ...glossary.store import STATUS_OK, normalize_match_text, source_matches_text
from ...i18n.languages import (
    glossary_context_dependent_sources,
    glossary_generic_target_words,
)
from ...models.document import Chapter, Segment
from ...models.glossary import TYPE_NAVIGATION, GlossaryTerm
from ...storage.protocol import Storage

_LOGGER = logging.getLogger(__name__)
_EN_CONTENT_WORD_RE = re.compile(r"\b[A-Za-z][A-Za-z'-]{3,}\b")
_EN_STOPWORDS = frozenset(
    {
        "that",
        "this",
        "with",
        "from",
        "they",
        "them",
        "their",
        "there",
        "were",
        "have",
        "been",
        "would",
        "could",
        "should",
        "about",
        "into",
        "over",
        "after",
        "before",
        "under",
        "then",
        "when",
        "where",
        "which",
        "while",
        "what",
        "your",
        "said",
        "asked",
        "replied",
        "will",
        "just",
        "only",
        "even",
        "also",
        "very",
        "much",
        "more",
        "most",
        "some",
        "such",
        "than",
        "into",
        "upon",
        "here",
        "made",
        "make",
        "like",
        "back",
        "down",
        "still",
        "never",
        "again",
        "well",
        "know",
        "come",
        "came",
        "went",
        "eyes",
        "hand",
        "head",
        "face",
        "voice",
        "looked",
        "turned",
        "heard",
    }
)
# High-frequency Chinese pronouns/particles/adverbs that never represent title/terminology drift.
_ZH_NOISE_CHARS = frozenset("我你他她它这這那了的着著过過吗嗎呢吧啊哦嗯就也都很再又")
_ZH_FUNCTION_BIGRAMS = frozenset(
    {
        "已经",
        "已經",
        "不是",
        "没有",
        "沒有",
        "什么",
        "什麼",
        "怎么",
        "怎麼",
        "可以",
        "因为",
        "因為",
        "所以",
        "但是",
        "如果",
        "虽然",
        "雖然",
        "而且",
        "或者",
        "自己",
        "时候",
        "時候",
        "只是",
        "不过",
        "不過",
        "还是",
        "還是",
        "然后",
        "然後",
        "知道",
        "觉得",
        "覺得",
        "看到",
        "看见",
        "看見",
        "说道",
        "說道",
        "问道",
        "問道",
        "答道",
        "笑道",
        "心中",
        "心里",
        "心裡",
        "如今",
        "原来",
        "原來",
        "实在",
        "實在",
        "其实",
        "其實",
        "自然",
        "当然",
        "當然",
        "应该",
        "應該",
        "必须",
        "必須",
        "一定",
        "一点",
        "一點",
        "一下",
        "一起",
        "一样",
        "一樣",
        "一直",
        "不能",
        "不会",
        "不會",
        "不知",
        "不敢",
        "不想",
        "不要",
        "不得",
        "无法",
        "無法",
    }
)
# Everyday Chinese vocabulary that dominates raw frequency on long books but whose split
# renderings ("need"/"require", "chance"/"opportunity") are ordinary synonym choice, not drift.
_ZH_EVERYDAY_GRAMS = frozenset(
    {
        "需要",
        "电话",
        "電話",
        "手机",
        "手機",
        "离开",
        "離開",
        "希望",
        "另外",
        "立刻",
        "马上",
        "馬上",
        "告诉",
        "告訴",
        "机会",
        "機會",
        "现在",
        "現在",
        "今天",
        "明天",
        "昨天",
        "东西",
        "東西",
        "事情",
        "地方",
        "时间",
        "時間",
        "问题",
        "問題",
        "开始",
        "開始",
        "回来",
        "回來",
        "出来",
        "出來",
        "起来",
        "起來",
        "进来",
        "進來",
        "下来",
        "下來",
        "过去",
        "過去",
        "准备",
        "準備",
        "突然",
        "忽然",
        "终于",
        "終於",
        "刚才",
        "剛才",
        "似乎",
        "好像",
        "仿佛",
        "彷彿",
        "继续",
        "繼續",
        "决定",
        "決定",
        "发现",
        "發現",
        "感觉",
        "感覺",
        "认为",
        "認為",
        "喜欢",
        "喜歡",
        "帮助",
        "幫助",
        "回答",
        "回头",
        "回頭",
        "点头",
        "點頭",
        "摇头",
        "搖頭",
        "声音",
        "聲音",
        "眼睛",
        "身体",
        "身體",
        "房间",
        "房間",
        "门口",
        "門口",
        "朋友",
        "孩子",
        "女人",
        "男人",
        "非常",
        "特别",
        "特別",
        "当时",
        "當時",
        "之后",
        "之後",
        "以后",
        "以後",
        "以前",
        "一口",
        "一切",
        "一定",
        "一般",
        "夜里",
        "夜裡",
        "夜裏",
        "屋里",
        "屋裡",
        "屋裏",
        "家里",
        "家裡",
        "家裏",
        "手里",
        "手裡",
        "手裏",
        "眼里",
        "眼裡",
        "眼裏",
        "嘴里",
        "嘴裡",
        "嘴裏",
    }
)
# Structural determiner/locative boundary fragments that never form valid 3-char domain terms
# (matched as 2-char substrings as well as exact matches, unlike everyday nouns like 手里/地方/朋友
# which appear inside real 3-char compounds such as 手里剑, 地方官, 朋友圈).
_ZH_DRIFT_FRAGMENT_SUBSTRINGS = frozenset(
    {
        "整个",
        "整個",
        "每个",
        "每個",
        "一个",
        "一個",
        "一些",
        "一直",
        "一起",
        "一样",
        "一樣",
        "一下",
        "一边",
        "一邊",
        "某个",
        "某個",
        "某些",
        "间里",
        "間裡",
        "間裏",
        "子里",
        "子裡",
        "子裏",
    }
)
# Smooth monotonic document-frequency cap for non-watched n-grams across all book sizes:
# an unglossed n-gram appearing in more than max(200, 1% of paragraphs) paragraphs is
# everyday vocabulary rather than mid-frequency term drift.
_DF_CAP_MIN_HITS = 200
_DF_CAP_RATIO = 0.01

# Irregular English inflections, as base -> other forms (pairs collapse to one target).
_EN_IRREGULAR_FORMS: dict[str, tuple[str, ...]] = {
    "be": ("was", "were", "been", "being", "is", "are"),
    "do": ("did", "done", "does"),
    "have": ("had", "has"),
    "leave": ("left",),
    "tell": ("told",),
    "take": ("took", "taken"),
    "make": ("made",),
    "find": ("found",),
    "give": ("gave", "given"),
    "keep": ("kept",),
    "hold": ("held",),
    "think": ("thought",),
    "bring": ("brought",),
    "feel": ("felt",),
    "hear": ("heard",),
    "know": ("knew", "known"),
    "see": ("saw", "seen"),
    "say": ("said",),
    "speak": ("spoke", "spoken"),
    "stand": ("stood",),
    "sit": ("sat",),
    "send": ("sent",),
    "spend": ("spent",),
    "lose": ("lost",),
    "meet": ("met",),
    "lead": ("led",),
    "buy": ("bought",),
    "catch": ("caught",),
    "teach": ("taught",),
    "fight": ("fought",),
    "seek": ("sought",),
    "win": ("won",),
    "run": ("ran",),
    "begin": ("began", "begun"),
    "become": ("became",),
    "come": ("came",),
    "go": ("went", "gone"),
    "get": ("got", "gotten"),
    "pay": ("paid",),
    "lay": ("laid",),
    "lie": ("lay", "lain"),
    "write": ("wrote", "written"),
    "drive": ("drove", "driven"),
    "ride": ("rode", "ridden"),
    "rise": ("rose", "risen"),
    "break": ("broke", "broken"),
    "choose": ("chose", "chosen"),
    "draw": ("drew", "drawn"),
    "drink": ("drank", "drunk"),
    "eat": ("ate", "eaten"),
    "fall": ("fell", "fallen"),
    "fly": ("flew", "flown"),
    "forget": ("forgot", "forgotten"),
    "forgive": ("forgave", "forgiven"),
    "freeze": ("froze", "frozen"),
    "grow": ("grew", "grown"),
    "hide": ("hid", "hidden"),
    "ring": ("rang", "rung"),
    "shake": ("shook", "shaken"),
    "show": ("showed", "shown"),
    "sing": ("sang", "sung"),
    "sink": ("sank", "sunk"),
    "sleep": ("slept",),
    "steal": ("stole", "stolen"),
    "strike": ("struck", "stricken"),
    "swear": ("swore", "sworn"),
    "swim": ("swam", "swum"),
    "tear": ("tore", "torn"),
    "throw": ("threw", "thrown"),
    "wake": ("woke", "woken"),
    "wear": ("wore", "worn"),
    "weave": ("wove", "woven"),
    "blow": ("blew", "blown"),
    "understand": ("understood",),
    "build": ("built",),
    "sell": ("sold",),
    "light": ("lit",),
    "bleed": ("bled",),
    "flee": ("fled",),
    "feed": ("fed",),
    "bind": ("bound",),
    "bend": ("bent",),
    "dig": ("dug",),
    "hang": ("hung",),
    "shoot": ("shot",),
    "slay": ("slew", "slain"),
    "swing": ("swung",),
}
_EN_IRREGULAR_LEMMAS: dict[str, frozenset[str]] = {}
for _base, _forms in _EN_IRREGULAR_FORMS.items():
    for _form in (_base, *_forms):
        _EN_IRREGULAR_LEMMAS[_form] = _EN_IRREGULAR_LEMMAS.get(_form, frozenset()) | {_base}
del _base, _forms, _form
_EN_VOWELS = frozenset("aeiou")


@dataclass(frozen=True)
class DriftCluster:
    source: str
    total_hits: int
    competing_targets: tuple[tuple[str, int], ...]
    paragraph_refs: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class ConsistencySweepResult:
    glossary_misses_patched: int
    drift_clusters_checked: int
    drift_patches_applied: int


def _has_offset_bound_metadata(seg: Segment) -> bool:
    meta = seg.meta or {}
    return bool(
        meta.get("annotations")
        or meta.get("epub_annotations")
        or meta.get("pdf_annotations")
        or meta.get("docx_runs")
        or meta.get("docx_styles")
        or meta.get("object_repairs")
    )


def _norm_en_word(word: str) -> str:
    low = word.casefold()
    if len(low) >= 5 and low.endswith("s") and not low.endswith("ss"):
        return low[:-1]
    return low


def _en_lemma_candidates(word: str) -> frozenset[str]:
    """Return candidate base lemmas for one English word (inflections only, no -er nouns).

    Intentionally loose for drift pre-screening (e.g. irregular homographs like rose/rise
    collapse conservatively rather than raising false-positive drift clusters).
    """
    w = word.casefold()
    cands: set[str] = {w}
    if w in _EN_IRREGULAR_LEMMAS:
        cands.update(_EN_IRREGULAR_LEMMAS[w])
    # _norm_en_word strips trailing 's' from >=5-char '-ies' plurals (enemies -> enemie)
    # and sibilant '-es' forms (princesses -> princesse, churches -> churche, teaches -> teache).
    if len(w) >= 4 and w.endswith("ie") and w[-3] not in _EN_VOWELS:
        cands.add(w[:-2] + "y")
    if len(w) >= 4 and w.endswith(("che", "she", "sse", "xe", "ze")):
        cands.add(w[:-1])
    if len(w) >= 5 and w.endswith("ies") and w[-4] not in _EN_VOWELS:
        cands.add(w[:-3] + "y")
    if len(w) >= 5 and w.endswith("ied") and w[-4] not in _EN_VOWELS:
        cands.add(w[:-3] + "y")
    if len(w) >= 4 and w.endswith("es"):
        cands.add(w[:-2])
        cands.add(w[:-1])
    elif len(w) >= 4 and w.endswith("s") and not w.endswith("ss"):
        cands.add(w[:-1])
    if len(w) >= 5 and w.endswith("ed"):
        cands.add(w[:-2])
        cands.add(w[:-1])
        if len(w) >= 6 and w[-3] == w[-4] and w[-3] not in _EN_VOWELS:
            cands.add(w[:-3])
    if len(w) >= 6 and w.endswith("ing"):
        stem = w[:-3]
        cands.add(stem)
        cands.add(stem + "e")
        if len(stem) >= 3 and stem[-1] == stem[-2] and stem[-1] not in _EN_VOWELS:
            cands.add(stem[:-1])
    # Expand any candidate base that is itself an irregular form (e.g. leaving -> leave).
    for cand in list(cands):
        if cand in _EN_IRREGULAR_LEMMAS:
            cands.update(_EN_IRREGULAR_LEMMAS[cand])
    return frozenset(cands)


def _same_en_lemma(left: str, right: str) -> bool:
    """Return True if two English words are inflections of one lemma (need/needed, left/leaving)."""
    a, b = left.casefold(), right.casefold()
    if a == b:
        return True
    return bool(_en_lemma_candidates(a) & _en_lemma_candidates(b))


def _is_everyday_gram(gram: str) -> bool:
    """Return True for everyday vocabulary/fragments that never signal term drift."""
    return (
        gram in _ZH_EVERYDAY_GRAMS
        or gram in _ZH_DRIFT_FRAGMENT_SUBSTRINGS
        or any(gram[i : i + 2] in _ZH_DRIFT_FRAGMENT_SUBSTRINGS for i in range(len(gram) - 1))
    )


def _has_mid_sentence_capital(word: str, targets: Sequence[str]) -> bool:
    """Return True if ``word`` appears capitalized mid-sentence at least twice."""
    if not word:
        return False
    cap = word[0].upper() + word[1:]
    pat = re.compile(rf"(?<=[a-z,;:—]\s){re.escape(cap)}s?\b")
    hits = 0
    for tgt in targets:
        if pat.search(tgt):
            hits += 1
            if hits >= 2:
                return True
    return False


def _protected_segment_ids(chapter: Chapter) -> set[int]:
    protected: set[int] = set()
    head: Segment | None = None
    for seg in chapter.text_segments:
        if not seg.cont or head is None:
            head = seg
        if _has_offset_bound_metadata(head):
            protected.add(id(seg))
    return protected


def detect_mid_frequency_drift(
    chapters: Sequence[Chapter],
    *,
    source_lang: str,
    target_lang: str,
    existing_glossary_sources: set[str],
    min_hits: int = 4,
    max_clusters: int = 12,
    extra_watch_sources: Mapping[str, Sequence[str]] | None = None,
) -> list[DriftCluster]:
    """Deterministically detect recurring non-glossary CJK terms with split English renderings."""
    src_base = (source_lang or "").lower().split("-", 1)[0]
    tgt_base = (target_lang or "").lower().split("-", 1)[0]
    if src_base != "zh" or tgt_base != "en" or max_clusters <= 0:
        return []

    blocked = {
        normalize_match_text(s).strip() for s in glossary_context_dependent_sources(source_lang)
    }
    norm_existing = {normalize_match_text(s).strip() for s in existing_glossary_sources}
    watch_map = extra_watch_sources or {}
    watched_norms = {normalize_match_text(s).strip() for s in watch_map}

    # Index 2..3-char CJK noun-like terms occurring across >=2 chapters.
    term_paras: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    term_chapters: dict[str, set[int]] = defaultdict(set)
    global_word_df: Counter[str] = Counter()
    total_paras = 0

    for ch in chapters:
        for seg_idx, seg in enumerate(ch.text_segments):
            src = (seg.source or "").strip()
            tgt = (seg.target or "").strip()
            if not src or not tgt:
                continue
            total_paras += 1
            words = {
                _norm_en_word(w)
                for w in _EN_CONTENT_WORD_RE.findall(tgt)
                if w.casefold() not in _EN_STOPWORDS
            }
            for w in words:
                global_word_df[w] += 1

            seen_grams: set[str] = set()
            for chunk in re.findall(r"[\u3400-\u9fff\uf900-\ufaff]{2,}", src):
                clen = len(chunk)
                for n in (2, 3):
                    if clen < n:
                        continue
                    for start in range(clen - n + 1):
                        gram = chunk[start : start + n]
                        if (
                            gram in seen_grams
                            or gram in _ZH_FUNCTION_BIGRAMS
                            or gram[0] in _CJK_EDGE_PARTICLES
                            or gram[-1] in _CJK_EDGE_PARTICLES
                            or any(c in _ZH_NOISE_CHARS for c in gram)
                            or all(c in _CJK_STOP_CHARS for c in gram)
                        ):
                            continue
                        norm = normalize_match_text(gram).strip()
                        if norm not in watched_norms and (
                            norm in blocked
                            or _is_everyday_gram(gram)
                            or norm in norm_existing
                            or any(norm in ex for ex in norm_existing)
                        ):
                            continue
                        seen_grams.add(gram)
                        term_paras[gram].append((ch.index, seg_idx, tgt))
                        term_chapters[gram].add(ch.index)

    if total_paras < 10:
        return []

    # Ranked as (not watched, no title signal, -split quality, -hits, -len, source).
    ranked: list[tuple[tuple[bool, bool, float, int, int, str], DriftCluster]] = []
    for gram, occurrences in term_paras.items():
        is_watched = normalize_match_text(gram).strip() in watched_norms
        if len(occurrences) < (3 if is_watched else min_hits) or len(term_chapters[gram]) < 2:
            continue
        n_occ = len(occurrences)
        if not is_watched:
            max_hits = max(_DF_CAP_MIN_HITS, int(total_paras * _DF_CAP_RATIO))
            if n_occ > max_hits:
                continue
        # Score distinctive target words that co-occur strongly with this source n-gram.
        occ_words = [
            {
                _norm_en_word(m)
                for m in _EN_CONTENT_WORD_RE.findall(tgt)
                if m.casefold() not in _EN_STOPWORDS
            }
            for _ci, _si, tgt in occurrences
        ]
        word_counts: Counter[str] = Counter()
        for words in occ_words:
            word_counts.update(words)

        # Collapse inflections of one English lemma (need/needed, leave/left) into the more
        # frequent form so tense/number variation is never reported as a split rendering.
        rep: dict[str, str] = {}
        reps: list[str] = []
        for word, _cnt in sorted(word_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:16]:
            rep[word] = next((r for r in reps if _same_en_lemma(word, r)), word)
            if rep[word] == word:
                reps.append(word)
        lemma_sets = [{rep[w] for w in words if w in rep} for words in occ_words]
        lemma_counts: Counter[str] = Counter()
        for lemmas in lemma_sets:
            lemma_counts.update(lemmas)
        lemma_df: Counter[str] = Counter()
        for word, r in rep.items():
            lemma_df[r] += global_word_df[word]

        distinctive: list[tuple[str, int]] = []
        min_lift = 1.15 if is_watched else 1.35
        for word, cnt in sorted(lemma_counts.items(), key=lambda kv: (-kv[1], reps.index(kv[0]))):
            share = cnt / n_occ
            base_rate = min(lemma_df[word], total_paras) / total_paras
            if (
                cnt >= 2
                and 0.18 <= share <= 0.85
                and base_rate > 0
                and (share / base_rate) >= min_lift
            ):
                distinctive.append((word, cnt))

        if len(distinctive) >= 2:
            # Check mutual exclusivity of top two distinctive target words (split rendering signal!).
            (w1, c1), (w2, c2) = distinctive[0], distinctive[1]
            both = sum(1 for lemmas in lemma_sets if w1 in lemmas and w2 in lemmas)
            if both <= min(c1, c2) * 0.30 and (c1 + c2) >= n_occ * 0.55:
                targets = [tgt for _ci, _si, tgt in occurrences]
                quality = (
                    (min(c1, c2) / max(c1, c2))
                    * (1.0 - both / min(c1, c2))
                    * min(1.0, (c1 + c2) / n_occ)
                    * math.log2(1 + min(c1, c2))
                )
                titled = _has_mid_sentence_capital(w1, targets) or _has_mid_sentence_capital(
                    w2, targets
                )
                ranked.append(
                    (
                        (not is_watched, not titled, -quality, -n_occ, -len(gram), gram),
                        DriftCluster(
                            source=gram,
                            total_hits=n_occ,
                            competing_targets=((w1, c1), (w2, c2)),
                            paragraph_refs=tuple((ci, si) for ci, si, _ in occurrences),
                        ),
                    )
                )
                continue

        # For watched recurring titles, also catch mid-sentence capitalization splits
        # (e.g. "the King" in Ch 1 vs "the king" in Ch 2) that case-folding hides.
        if is_watched and word_counts:
            top_word, top_cnt = word_counts.most_common(1)[0]
            if top_cnt >= 3 and (top_cnt / n_occ) >= 0.40:
                cap_hits = 0
                low_hits = 0
                cap_chs: set[int] = set()
                low_chs: set[int] = set()
                pat = re.compile(rf"(?<=[a-z,;:—]\s)({re.escape(top_word)}s?)\b", re.I)
                for ci, _si, tgt in occurrences:
                    matches = pat.findall(tgt)
                    if not matches:
                        continue
                    has_cap = any(m[0].isupper() for m in matches)
                    has_low = any(m[0].islower() for m in matches)
                    if has_cap and not has_low:
                        cap_hits += 1
                        cap_chs.add(ci)
                    elif has_low and not has_cap:
                        low_hits += 1
                        low_chs.add(ci)
                if cap_hits >= 2 and low_hits >= 2 and len(cap_chs | low_chs) >= 2:
                    cap_quality = (min(cap_hits, low_hits) / max(cap_hits, low_hits)) * math.log2(
                        1 + min(cap_hits, low_hits)
                    )
                    ranked.append(
                        (
                            (
                                False,
                                False,
                                -cap_quality,
                                -n_occ,
                                -len(gram),
                                gram,
                            ),
                            DriftCluster(
                                source=gram,
                                total_hits=n_occ,
                                competing_targets=(
                                    (top_word.capitalize(), cap_hits),
                                    (top_word.lower(), low_hits),
                                ),
                                paragraph_refs=tuple((ci, si) for ci, si, _ in occurrences),
                            ),
                        )
                    )

    # Watched titles first, then proper-noun/title splits, then highest-evidence balanced
    # splits; drop overlapping fragments of the same phrase sharing the same competing targets.
    ranked.sort(key=lambda item: item[0])
    selected: list[tuple[DriftCluster, frozenset[str], frozenset[tuple[int, int]]]] = []
    for _key, cluster in ranked:
        pair = frozenset(w.casefold() for w, _ in cluster.competing_targets)
        refs = frozenset(cluster.paragraph_refs)
        if any(
            pair == prev_pair
            and (
                cluster.source in prev.source
                or prev.source in cluster.source
                or len(refs & prev_refs) >= 0.7 * min(len(refs), len(prev_refs))
            )
            for prev, prev_pair, prev_refs in selected
        ):
            continue
        selected.append((cluster, pair, refs))
        if len(selected) >= max_clusters:
            break
    return [cluster for cluster, _pair, _refs in selected]


class ConsistencySweepService:
    """Fast post-translation cross-chapter unification pass."""

    def __init__(self, sparse_editor: SparseSemanticEditor, *, source_lang: str, target_lang: str):
        self._editor = sparse_editor
        self._src = source_lang
        self._tgt = target_lang

    def run(
        self,
        store: Storage,
        *,
        chapter_indices: Sequence[int],
        terms: Sequence[GlossaryTerm],
        style: str = "",
        conventions: Sequence[str] = (),
        divergent_titles: Mapping[str, Sequence[str]] | None = None,
        max_patch_paragraphs: int = 24,
    ) -> ConsistencySweepResult:
        """Scan completed chapters for cross-chapter quote/glossary drift and patch surgically."""
        if len(chapter_indices) < 2:
            return ConsistencySweepResult(0, 0, 0)

        chapters = [store.load_chapter(ci) for ci in chapter_indices]
        protected_by_ch = {ch.index: _protected_segment_ids(ch) for ch in chapters}
        modified_chapters: set[int] = set()

        # 1. Book-wide glossary miss sweep (surgical sparse patches for missed canonical terms).
        generic_words = glossary_generic_target_words(self._tgt)
        entity_terms = [
            t
            for t in terms
            if t.status == STATUS_OK
            and t.type in {"person", "place", "organization", "term"}
            and len(t.source.strip()) >= 2
            and len(t.target.strip()) >= 2
            and not _is_generic(t.target, generic_words)
        ]

        drifted_items: list[tuple[Chapter, Segment, GlossaryTerm]] = []
        seen_seg_ids: set[int] = set()
        for ch in chapters:
            prot = protected_by_ch[ch.index]
            for seg in ch.text_segments:
                if id(seg) in prot or id(seg) in seen_seg_ids or not seg.target:
                    continue
                for term in entity_terms:
                    if not source_matches_text(term.source, seg.source):
                        continue
                    if _target_contains_term(
                        seg.target,
                        term.target,
                        target_lang=self._tgt,
                        allow_plural=term.type != "person",
                    ):
                        continue
                    if any(
                        other.source != term.source
                        and term.source in other.source
                        and source_matches_text(other.source, seg.source)
                        and _target_contains_term(
                            seg.target,
                            other.target,
                            target_lang=self._tgt,
                            allow_plural=other.type != "person",
                        )
                        for other in entity_terms
                    ):
                        continue
                    drifted_items.append((ch, seg, term))
                    seen_seg_ids.add(id(seg))
                    break
                if len(drifted_items) >= max_patch_paragraphs:
                    break
            if len(drifted_items) >= max_patch_paragraphs:
                break

        glossary_patched = 0
        if drifted_items:
            for offset in range(0, len(drifted_items), 8):
                chunk = drifted_items[offset : offset + 8]
                chunk_sources = [seg.source for _, seg, _ in chunk]
                chunk_drafts = [seg.target or "" for _, seg, _ in chunk]
                chunk_terms = list({t.source: t for _, _, t in chunk}.values())
                res = self._editor.edit_batch(
                    chunk_sources,
                    chunk_drafts,
                    glossary_terms=chunk_terms,
                    style=style,
                )
                if not res.failed:
                    for idx in res.patched_indices:
                        ch, seg, _ = chunk[idx]
                        if res.targets[idx] != seg.target:
                            seg.target = res.targets[idx]
                            modified_chapters.add(ch.index)
                            glossary_patched += 1

        # 2. Detect mid-frequency title/term drift and unify confirmed splits surgically.
        drift_clusters = detect_mid_frequency_drift(
            chapters,
            source_lang=self._src,
            target_lang=self._tgt,
            existing_glossary_sources={t.source for t in terms if t.type != TYPE_NAVIGATION},
            extra_watch_sources=divergent_titles,
        )
        drift_patched = 0
        drift_decisions: dict[str, str] = {}
        if drift_clusters:
            drift_decisions = self._editor.adjudicate_drift(
                [
                    {
                        "source": c.source,
                        "targets": list(c.competing_targets),
                    }
                    for c in drift_clusters
                ],
                style=style,
                conventions=conventions,
            )
        if drift_decisions:
            drift_items: list[tuple[Chapter, Segment, GlossaryTerm]] = []
            seen_drift_segs: set[int] = set()
            max_drift_paras = min(16, max_patch_paragraphs)
            for ch in chapters:
                prot = protected_by_ch[ch.index]
                for seg in ch.text_segments:
                    if id(seg) in prot or id(seg) in seen_drift_segs or not seg.target:
                        continue
                    for src_key, canonical_tgt in drift_decisions.items():
                        if not source_matches_text(src_key, seg.source):
                            continue
                        stripped_canon = canonical_tgt.strip()
                        core_canon = re.sub(r"^(?:the|a|an)\s+", "", stripped_canon, flags=re.I)
                        if self._tgt.lower().split("-", 1)[0] == "en" and core_canon:
                            if any(c.isupper() for c in core_canon):
                                # Respect intentional capitalization (e.g. "King" vs "king").
                                if re.search(rf"\b{re.escape(core_canon)}s?\b", seg.target):
                                    continue
                            else:
                                # Lowercase canonical (e.g. "king"): skip only if no mid-sentence
                                # capitalized variant remains and the canonical term is present.
                                cap_variant = core_canon[0].upper() + core_canon[1:]
                                has_mid_cap = bool(
                                    re.search(
                                        rf"(?<=[a-z,;:—]\s){re.escape(cap_variant)}s?\b",
                                        seg.target,
                                    )
                                )
                                if not has_mid_cap and _target_contains_term(
                                    seg.target, canonical_tgt, target_lang=self._tgt
                                ):
                                    continue
                        elif _target_contains_term(
                            seg.target, canonical_tgt, target_lang=self._tgt
                        ):
                            continue
                        drift_items.append(
                            (
                                ch,
                                seg,
                                GlossaryTerm(source=src_key, target=canonical_tgt, type="term"),
                            )
                        )
                        seen_drift_segs.add(id(seg))
                        break
                    if len(drift_items) >= max_drift_paras:
                        break
                if len(drift_items) >= max_drift_paras:
                    break
            for offset in range(0, len(drift_items), 8):
                chunk = drift_items[offset : offset + 8]
                chunk_sources = [seg.source for _, seg, _ in chunk]
                chunk_drafts = [seg.target or "" for _, seg, _ in chunk]
                chunk_terms = list({t.source: t for _, _, t in chunk}.values())
                res = self._editor.edit_batch(
                    chunk_sources,
                    chunk_drafts,
                    glossary_terms=chunk_terms,
                    style=style,
                )
                if not res.failed:
                    for idx in res.patched_indices:
                        ch, seg, _ = chunk[idx]
                        if res.targets[idx] != seg.target:
                            seg.target = res.targets[idx]
                            modified_chapters.add(ch.index)
                            drift_patched += 1

        for ch in chapters:
            if ch.index in modified_chapters:
                store.save_chapter(ch)

        if glossary_patched or drift_clusters or drift_patched:
            store.log_event(
                "consistency_sweep_completed",
                glossary_misses_patched=glossary_patched,
                drift_patches_applied=drift_patched,
                drift_decisions=drift_decisions,
                drift_clusters=[
                    {
                        "source": c.source,
                        "hits": c.total_hits,
                        "targets": list(c.competing_targets),
                    }
                    for c in drift_clusters
                ],
            )
        return ConsistencySweepResult(
            glossary_misses_patched=glossary_patched,
            drift_clusters_checked=len(drift_clusters),
            drift_patches_applied=drift_patched,
        )
