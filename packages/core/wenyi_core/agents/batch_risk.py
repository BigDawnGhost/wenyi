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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..glossary.store import STATUS_OK, normalize_match_text, source_matches_text
from ..i18n.languages import glossary_generic_target_words
from ..ingest.segmenter import is_verse_segment
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
_ZH_CONDITIONAL_RE = re.compile(
    r"(?:不[^，。！？]{2,18}[，,]就|若非|如若不然|若是[^，。！？]{2,16}[，,](?:便|就|则|則))"
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

_ZH_VERSE_LI_ALLUSION_RE = re.compile(r"八百里[^，。！？]{0,6}(?:分|炙|麾下)")
_EN_EIGHT_HUNDRED_LI_TRAP = re.compile(r"\b(?:eight\s+hundred|800)\s+li\b", re.I)

_ZH_AGENCY_PROMISE_RE = re.compile(r"(?:我答应替他|我答應替他|他答应替我|他答應替我)")
_ZH_AT_LEAST_RE = re.compile(r"至少")
_EN_AT_MOST_TRAP = re.compile(r"\b(?:no\s+more\s+than|at\s+most)\b", re.I)
_ZH_INLAW_KINSHIP_RE = re.compile(r"(?:大姨|二姨|三姨|小姨|大舅子|小舅子)")
_EN_AUNT_UNCLE_TRAP = re.compile(r"\b(?:aunts?|uncles?)\b", re.I)
_ZH_VOCATIVE_SHIFT_RE = re.compile(
    r"(?:别走|別走|莫走|休走|且慢|慢着|慢著)[^。！？]{0,8}[，,]\s*[\u4e00-\u9fff]{1,3}(?:大人|先生|将军|將軍|殿下|公子|师父|師父|长老|長老)[，,]"
)
_EN_CALQUE_SENTINEL_RE = re.compile(
    r"\b(?:dream(?:ed|ing)?\s+(?:its\s+|a\s+)?drunken\s+dream\s+of\s+death|"
    r"fire-dwelling\s+daoist|filled\s+in\s+lyrics|flashed\s+with\s+treasure\s+light|"
    r"to\s+death\s+with\s+her|ten\s+thousand\s+years)\b",
    re.I,
)
_ZH_CLASSICAL_PARTICLES_RE = re.compile(r"[之乎者也矣焉哉岂豈孰乃遂故既若非莫勿裣衽斂衽]")


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


def _target_contains_term(target_clean: str, canonical: str, *, target_lang: str) -> bool:
    norm_canon = " ".join(normalize_match_text(canonical).split())
    if not norm_canon:
        return True
    norm_tgt = " ".join(normalize_match_text(target_clean).split())
    if _is_cjk_lang(target_lang):
        return norm_canon in norm_tgt
    pat = re.compile(rf"(?<![^\W\d_]){re.escape(norm_canon)}(?![^\W\d_])", re.I)
    if pat.search(norm_tgt):
        return True
    # Also accept canonical without a leading article ("the Prince of De" -> "Prince of De").
    stripped = re.sub(r"^(?:the|a|an)\s+", "", norm_canon, flags=re.I)
    if stripped and stripped != norm_canon:
        pat2 = re.compile(rf"(?<![^\W\d_]){re.escape(stripped)}(?![^\W\d_])", re.I)
        return bool(pat2.search(norm_tgt))
    return False


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

    unlineated_couplets: list[int] = []
    classical_dense_paras = 0
    dialogue_paras = 0
    high_sev_count = 0

    for idx, (raw_src, raw_drf) in enumerate(zip(sources, drafts)):
        src = raw_src.strip()
        drf = raw_drf.strip()
        if not src or not drf:
            continue
        clean_drf = _strip_protected(drf)
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
        if is_verse_segment(src) and "\n" not in clean_drf:
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
        for term in entity_terms:
            if not source_matches_text(term.source, src):
                continue
            if _target_contains_term(clean_drf, term.target, target_lang=target_lang):
                continue
            # Avoid false positives when a longer glossary term covering term.source is matched.
            if any(
                other.source != term.source
                and term.source in other.source
                and source_matches_text(other.source, src)
                and _target_contains_term(clean_drf, other.target, target_lang=target_lang)
                for other in entity_terms
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
            if _ZH_VERSE_LI_ALLUSION_RE.search(src) and _EN_EIGHT_HUNDRED_LI_TRAP.search(clean_drf):
                alerts.append(
                    ParagraphAlert(
                        idx,
                        "allusion",
                        "'八百里' in this lyric alludes to the famed ox (roasted beef), not a distance of 800 li.",
                        high_severity=True,
                    )
                )
            if _ZH_AGENCY_PROMISE_RE.search(src):
                alerts.append(
                    ParagraphAlert(
                        idx,
                        "agency_direction",
                        "Verify who promised to act for whom (答应替他 = promised to do it for him).",
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
            if _ZH_VOCATIVE_SHIFT_RE.search(src):
                alerts.append(
                    ParagraphAlert(
                        idx,
                        "addressee_shift",
                        "Mid-sentence vocative after imperative; verify the vocative attaches to the right clause.",
                        high_severity=True,
                    )
                )
            if _EN_CALQUE_SENTINEL_RE.search(clean_drf):
                alerts.append(
                    ParagraphAlert(
                        idx,
                        "calque_or_idiom",
                        "Possible literalized idiom/calque or pronoun-referent glitch detected; verify against source.",
                        high_severity=True,
                    )
                )
            if len(_ZH_CLASSICAL_PARTICLES_RE.findall(src)) >= 4:
                classical_dense_paras += 1

        if any(q in src for q in ('"', "“", "「", "『")):
            dialogue_paras += 1
        if any(a.high_severity for a in alerts[para_alerts_before:]):
            high_sev_count += 1

    if len(unlineated_couplets) >= 2:
        alerts.append(
            ParagraphAlert(
                unlineated_couplets[0],
                "unlineated_verse",
                f"Paragraphs {unlineated_couplets} are rhymed verse couplets drafted without '\\n' line breaks.",
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
