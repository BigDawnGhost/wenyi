"""Zero-cost deterministic structural draft pre-screener.

Inspects aligned ``(source, draft)`` pairs before ``SparseSemanticEditor`` runs to pinpoint
high-precision structural anomalies (script/markup residue, unrequested inline glosses,
severe omission length-ratio drops, unlineated standalone or embedded verse, and glossary
canonical misses) so the sparse editor focuses directly on suspect paragraphs without
overriding the user's configured ``polish.body`` thinking level.

Pure functions only: zero LLM calls, zero pipeline or storage imports.
"""

from __future__ import annotations

import re
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
# Exclude common prose dialogue verbs ending in 吟道 (沉吟道 "mused", 笑吟吟道 "said smilingly",
# 呻吟道 "groaned") while still catching standalone 吟道 / 轻声吟道 / 低声吟道 verse recitations.
_EMBEDDED_ZH_VERSE_RE = re.compile(
    r"(?:《(?:破阵子|破陣子|鹊桥仙|鵲橋仙|西江月|沁园春|沁園春|水调歌头|水調歌頭|满江红|滿江紅|"
    r"念奴娇|念奴嬌|虞美人|菩萨蛮|菩薩蠻|临江仙|臨江仙|蝶恋花|蝶戀花|思帝乡|思帝鄉|清平乐|清平樂|"
    r"声声慢|聲聲慢|如梦令|如夢令|浪淘沙|江城子|青玉案|定风波|定風波)》|"
    r"(?:诗曰|詩曰|词曰|詞曰|偈曰|赞曰|讚曰|歌曰|有诗为证|有詩為證|(?<![沉吟呻])吟道|唱道)[：:，,]?\s*[「『“\"])"
)
_EMBEDDED_JA_VERSE_RE = re.compile(
    r"(?:短歌|俳句|和歌|詠んだ|歌った|記されていた)[^「『]{0,24}[「『][^」』]{12,60}[」』]"
)
_JA_TANKA_QUOTE_RE = re.compile(
    r"[「『][^」』\n]{3,10}[\s　]+[^」』\n]{4,10}[\s　]+[^」』\n]{3,10}[」』]"
)

# Pure Chinese numeral glossary names (e.g. 十一 -> Eleven) should not match inside chapter
# numbers (第七十一章), dates (十一月), times (十一点), or longer numerals (七十一).
_ZH_NUMERAL_ONLY_RE = re.compile(r"^[零〇一二三四五六七八九十百千万萬亿億两兩]+$")
_ZH_NUMERAL_LEFT_BOUNDARY = frozenset("第零〇一二三四五六七八九十百千万萬亿億两兩")
_ZH_NUMERAL_RIGHT_BOUNDARY = frozenset(
    "零〇一二三四五六七八九十百千万萬亿億章节節卷集篇幕年月日天时時分秒点點岁歲个個名位人件条條次遍倍成斤两兩米尺丈里元块塊钱錢"
)
# Common characters forming return/reply verb compounds after 回 (e.g. 十一回头 / 十一回到),
# distinguishing character-name subjects from counts like 十一回才找到 or 十一回合.
_ZH_HUI_VERB_NEXT = frozenset(
    "头頭到来來去答回应應复復覆身望忆憶想首眸道话話礼禮敬绝絕看转轉返归歸神"
)


@dataclass(frozen=True)
class ParagraphAlert:
    index: int
    code: str
    detail: str
    high_severity: bool = True


@dataclass(frozen=True)
class BatchRiskReport:
    alerts: tuple[ParagraphAlert, ...]

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


@lru_cache(maxsize=1 << 14)
def _is_generic(text: str, generic_words: frozenset[str]) -> bool:
    if not generic_words:
        return False
    words = _WORD_RE.findall(text.casefold())
    return bool(words) and all(w.replace("’", "'") in generic_words for w in words)


def has_embedded_verse(source: str, *, source_lang: str) -> bool:
    """Detect poems/lyrics embedded inside a longer prose/dialogue paragraph."""
    base = (source_lang or "").lower().split("-", 1)[0]
    if base == "zh":
        return bool(_EMBEDDED_ZH_VERSE_RE.search(source))
    if base == "ja":
        return bool(_EMBEDDED_JA_VERSE_RE.search(source) or _JA_TANKA_QUOTE_RE.search(source))
    return False


def _glossary_source_matches(term_source: str, norm_text: str, *, src_base: str) -> bool:
    """Match ``term_source`` in ``norm_text``, guarding pure CJK numeral names against headings/counts."""
    if not source_matches_normalized(term_source, norm_text):
        return False
    if src_base != "zh":
        return True
    key = normalize_match_text(term_source).strip()
    if not key or not _ZH_NUMERAL_ONLY_RE.fullmatch(key):
        return True
    start = 0
    while True:
        pos = norm_text.find(key, start)
        if pos == -1:
            return False
        left_ok = pos == 0 or norm_text[pos - 1] not in _ZH_NUMERAL_LEFT_BOUNDARY
        end = pos + len(key)
        if end >= len(norm_text):
            right_ok = True
        else:
            next_ch = norm_text[end]
            if next_ch in _ZH_NUMERAL_RIGHT_BOUNDARY:
                right_ok = False
            elif next_ch == "回":
                after_hui = norm_text[end + 1] if end + 1 < len(norm_text) else ""
                right_ok = after_hui in _ZH_HUI_VERB_NEXT
            else:
                right_ok = True
        if left_ok and right_ok:
            return True
        start = pos + 1


def assess_batch_risk(
    sources: Sequence[str],
    drafts: Sequence[str],
    *,
    source_lang: str,
    target_lang: str,
    glossary_terms: Sequence[GlossaryTerm] = (),
    variants_by_source: Mapping[str, Sequence[str]] | None = None,
) -> BatchRiskReport:
    """Scan ``(sources, drafts)`` for concrete structural anomalies and glossary misses."""
    alerts: list[ParagraphAlert] = []
    src_base = (source_lang or "").lower().split("-", 1)[0]
    tgt_base = (target_lang or "").lower().split("-", 1)[0]
    generic_words = glossary_generic_target_words(target_lang)
    variants_map = variants_by_source or {}

    # Filter glossary terms to high-signal entity mappings for miss detection. Navigation
    # titles (type="navigation") are headings, not inline entities.
    entity_terms = [
        t
        for t in glossary_terms
        if t.status == STATUS_OK
        and t.type in {"person", "place", "organization", "term"}
        and len(t.source.strip()) >= 2
        and len(t.target.strip()) >= 2
        and not _is_generic(t.target, generic_words)
    ]

    para_srcs = [raw_src.strip() for raw_src in sources]
    para_drfs = [raw_drf.strip() for raw_drf in drafts]
    clean_drfs: list[str] = []
    norm_srcs: list[str] = []
    norm_drfs: list[str] = []
    for src, drf in zip(para_srcs, para_drfs):
        if not src or not drf:
            clean_drfs.append("")
            norm_srcs.append("")
            norm_drfs.append("")
            continue
        clean = _strip_protected(drf)
        clean_drfs.append(clean)
        norm_srcs.append(normalize_match_text(src))
        norm_drfs.append(" ".join(normalize_match_text(clean).split()))

    # Pre-filter terms once per batch. "\n" is a non-word character, so word boundaries at
    # paragraph starts and ends stay valid in the joined text and CJK substring matches are
    # unaffected; a term with no batch-level hit can never fire in any single paragraph.
    batch_norm_src = "\n".join(norm_srcs)
    miss_candidates = [
        t for t in entity_terms if source_matches_normalized(t.source, batch_norm_src)
    ]

    verse_indices = verse_block_indices(sources)
    unlineated_couplets: list[int] = []

    for idx, (src, drf) in enumerate(zip(para_srcs, para_drfs)):
        if not src or not drf:
            continue
        clean_drf = clean_drfs[idx]
        norm_src = norm_srcs[idx]
        norm_drf = norm_drfs[idx]

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
            # Typical zh->en is ~0.55–1.10 English words per Chinese char; <0.28 signals omission.
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
            if not _glossary_source_matches(term.source, norm_src, src_base=src_base):
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
                and _glossary_source_matches(other.source, norm_src, src_base=src_base)
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

    if unlineated_couplets:
        alerts.append(
            ParagraphAlert(
                unlineated_couplets[0],
                "unlineated_verse",
                f"Paragraphs {unlineated_couplets} are verse lines drafted without '\\n' line breaks.",
                high_severity=False,
            )
        )

    return BatchRiskReport(alerts=tuple(alerts))
