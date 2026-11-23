"""Deterministic post-translation hygiene and minority-name normalization.

Pure functions with zero model calls:
- ``normalize_latin_typography`` repairs raw ``--`` dashes, inconsistent ``said: "..."``
  speech-tag colons, stray trailing unmatched closing quotes, and isolated single-quote
  dialogue flips in Latin-script targets while preserving protected-object markers,
  Markdown code spans, and LaTeX math spans.
- ``normalize_chapter_cjk_typography`` applies conservative typography cleanup for Chinese
  targets (escaped ``\\n``, raw ``--`` -> ``——``, stray half-width punctuation between Han
  characters) with the same code/math/marker protection.
- ``normalize_minority_names`` replaces known minority renderings of canonical glossary
  entries with the established target across both Latin and uncased CJK targets using
  boundary and collision guards, logging every substitution for auditability.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from ...glossary.store import STATUS_OK, source_matches_text
from ...i18n.languages import glossary_generic_target_words
from ...models.glossary import GlossaryTerm

# Matches protected-object transport tokens such as 〘object:1〙 or ⟦N0⟧.
_PROTECTED_MARKER_RE = re.compile(r"〘object:[A-Za-z0-9_.:-]+〙|⟦[^⟧]+⟧")
# Also shields fenced/inline Markdown code spans, LaTeX math spans, and URLs during hygiene.
_TYPOGRAPHY_PROTECTED_RE = re.compile(
    r"〘object:[A-Za-z0-9_.:-]+〙|⟦[^⟧]+⟧"
    r"|(?<!`)(`{3,})(?!`)[\s\S]*?(?<!`)\1(?!`)"
    r"|(?<!`)``(?!`)[^\n]+?(?<!`)``(?!`)"
    r"|(?<!`)`(?!`)[^`\n]+?(?<!`)`(?!`)"
    r"|(?<!\\)\$\$[\s\S]*?(?<!\\)\$\$"
    r"|(?<!\\)\\\[[\s\S]*?(?<!\\)\\\]"
    r"|(?<!\\)\\\([\s\S]*?(?<!\\)\\\)"
    r"|(?<!\\)\$(?!\$)(?!\s)(?:\\.|[^$\\\n])+?(?<!\s)\$(?![$\d])"
    r"|https?://[^\s\"'“”‘’]+"
)
# Match standalone leaked literal '\n' line breaks without touching LaTeX commands
# ('\neq', '\nu', '\nabla', '\nRightarrow'), escaped backslashes ('\\n'), or paths ('C:\new').
_ESCAPED_NEWLINE_RE = re.compile(
    r"(?<!\\)(?:\\n)+(?![A-Za-z])"
    r"|(?<=[.,;!?…—\"”'’】》」』])(?<!\\)(?:\\n)+(?=[A-Za-z])"
)
_RAW_DOUBLE_DASH_RE = re.compile(r"(?<!-)(?: -- |--)(?!-)")
# Normalize mid-sentence spaced em dashes (" — ") to tight American em dashes ("—") while
# leaving line-start/attribution dashes ("—History...") untouched.
_SPACED_EM_DASH_RE = re.compile(r"(?<=\S)[ \t]+—[ \t]+(?=\S)")
_SPEECH_COLON_RE = re.compile(
    r"\b(said|asked|replied|answered|cried|shouted|whispered|laughed|added|"
    r"murmured|exclaimed|called|muttered|snapped|sighed|retorted|yelled|groaned|"
    r"report|reported|recounted|announced|declared|proclaimed|chanted|recited|"
    r"continued|explained|pleaded|begged|warned)"
    r':(\s*[“"])',
    re.IGNORECASE,
)
_SINGLE_QUOTE_UTTERANCE_RE = re.compile(
    r"(?P<prefix>(?:^|(?<=[\s,])))(?:'|‘)(?P<body>[A-Z][^'‘’\n]{10,}?[,.!?…])(?:'|’)(?=$|[\s,;.)])"
)
_WORD_RE = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*")
_SOURCE_CLOSING_QUOTE_CHARS = ("」", "』", "”", '"')
_SOURCE_OPENING_QUOTE_CHARS = ("「", "『", "“", '"', "《", "«")
_VERSE_INTRODUCER_RE = re.compile(
    r"(?:詩曰|诗曰|詞曰|词曰|但見|但见|道是|讚曰|赞曰|偈曰|歌曰|有詩為證|有詩为证|有诗为证|"
    r"詞寄|词寄|西江月|沁園春|沁园春|古風|古风|破阵子|破陣子|鹊桥仙|鵲橋仙|思帝乡|思帝鄉|"
    r"正是：|真是：|果然是：|吟道|唱道)"
)
_COUPLET_SOURCE_RE = re.compile(r"^[^，。！？]{4,10}[，、][^，。！？]{4,10}[。！？；]$")


def _mask_markers(
    text: str, pattern: re.Pattern[str] = _PROTECTED_MARKER_RE
) -> tuple[str, list[str]]:
    saved: list[str] = []

    def _repl(match: re.Match[str]) -> str:
        idx = len(saved)
        saved.append(match.group(0))
        return f"\ue000OBJ{idx}\ue001"

    return pattern.sub(_repl, text), saved


def _unmask_markers(text: str, saved: list[str]) -> str:
    for idx, marker in enumerate(saved):
        text = text.replace(f"\ue000OBJ{idx}\ue001", marker)
    return text


def _unify_quote_marks(text: str) -> str:
    """Standardize mixed curly and straight quotation marks to uniform straight quotes."""
    return text.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")


def _collapse_stray_prose_dialogue_breaks(source: str, target: str) -> str:
    """Join accidental newline splits between dialogue turns inside a continuous prose paragraph.

    Strictly skips verse segments, paragraphs with verse introducers, short couplets, or
    any multi-line block containing unquoted short poetic lines.
    """
    if "\n" not in target or "\n" in source:
        return target
    src = source.strip()
    if len(src) < 35 or _VERSE_INTRODUCER_RE.search(src) or _COUPLET_SOURCE_RE.match(src):
        return target
    lines = [ln.strip() for ln in target.splitlines() if ln.strip()]
    if len(lines) < 2:
        return target
    if all(
        (len(ln) >= 35 or ('"' in ln or "“" in ln))
        and ln.endswith((".", "!", "?", '."', '!"', '?"', ".”", "!”", "?”"))
        for ln in lines
    ) and any(('"' in ln or "“" in ln) for ln in lines):
        return " ".join(lines)
    return target


_SEQ_TOKEN_RE = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*|\d+")


def _word_seq(text: str) -> list[str]:
    return [w.lower() for w in _SEQ_TOKEN_RE.findall(text)]


def _trim_leaked_next_paragraph(
    source: str,
    target: str,
    next_source: str,
    next_target: str,
) -> str:
    """Strip a trailing newline-separated block in ``target`` that duplicates ``next_target``.

    Guards against model anticipation from ``[Following source paragraph]`` across both
    blank-line (``\\n\\n``) and single-newline (``\\n``) trailing duplicates.
    """
    if not target or not next_target or "\n" not in target:
        return target
    src_a, src_b = source.strip(), next_source.strip()
    if src_a and src_b and (src_b in src_a or src_a in src_b):
        return target
    next_seq = _word_seq(next_target)
    if len(next_seq) < 5:
        return target

    for sep in ("\n\n", "\n"):
        if sep in target:
            head, tail = target.rsplit(sep, 1)
            tail_seq = _word_seq(tail)
            if head.strip() and 5 <= len(tail_seq) <= len(next_seq):
                if next_seq[: len(tail_seq)] == tail_seq:
                    return head.rstrip()
    return target


def normalize_latin_typography(
    text: str,
    *,
    dominant_double_quotes: bool = True,
    quote_pair: tuple[str, str] = ("“", "”"),
    source: str = "",
    open_quote_carry: bool = False,
) -> tuple[str, list[str]]:
    """Apply conservative, deterministic English/Latin typography fixes to one paragraph."""
    if not text or not text.strip():
        return text, []

    masked, saved = _mask_markers(text, _TYPOGRAPHY_PROTECTED_RE)
    applied: list[str] = []

    # 0. Repair leaked literal '\n' escape sequences in non-code literary prose/verse.
    if "\\n" in masked and not text.lstrip().startswith("```"):
        updated = _ESCAPED_NEWLINE_RE.sub(lambda m: "\n" * (len(m.group(0)) // 2), masked)
        if updated != masked:
            masked = updated
            applied.append("escaped_newline")

    # 1. Normalize raw '--' to em dash '—' (skip markdown rules/code fences).
    if "--" in masked and not text.lstrip().startswith(("```", "---")):
        updated = _RAW_DOUBLE_DASH_RE.sub("—", masked)
        if updated != masked:
            masked = updated
            applied.append("em_dash")

    # 1b. Unify mid-sentence spaced em dashes (" — ") to tight em dashes ("—") in US English prose.
    if dominant_double_quotes and " — " in masked:
        updated = _SPACED_EM_DASH_RE.sub("—", masked)
        if updated != masked:
            masked = updated
            applied.append("em_dash_spacing")

    # 2. Normalize dialogue speech-tag colons: 'said: "' -> 'said, "'.
    updated = _SPEECH_COLON_RE.sub(r"\1,\2", masked)
    if updated != masked:
        masked = updated
        applied.append("speech_tag_colon")

    # 3. Repair isolated single-quote dialogue utterances when the book uses double quotes.
    if dominant_double_quotes and ('"' not in masked and "“" not in masked):
        quote_open, quote_close = quote_pair
        updated = _SINGLE_QUOTE_UTTERANCE_RE.sub(
            lambda m: f"{m.group('prefix')}{quote_open}{m.group('body')}{quote_close}",
            masked,
        )
        if updated != masked:
            masked = updated
            applied.append("single_quote_dialogue")

    # 4. Drop unambiguous stray unmatched closing quote at paragraph end (0 opens, 1 close),
    # ONLY when we are not closing a multi-paragraph open speech and the source has no close quote.
    src_has_close = any(ch in source for ch in _SOURCE_CLOSING_QUOTE_CHARS)
    src_ends_open = source.rstrip().endswith(_SOURCE_OPENING_QUOTE_CHARS)
    if not open_quote_carry and not src_has_close:
        if (
            masked.count("“") == 0
            and masked.count("”") == 1
            and masked.count('"') % 2 == 0
            and masked.rstrip().endswith("”")
        ):
            pos = masked.rfind("”")
            masked = masked[:pos] + masked[pos + 1 :]
            applied.append("stray_closing_quote")
        elif (
            not src_ends_open
            and masked.count('"') == 1
            and masked.count("“") <= masked.count("”")
            and masked.rstrip().endswith('"')
        ):
            pos = masked.rfind('"')
            if pos > 0 and not masked[pos - 1].isspace() and not masked[pos - 1].isdigit():
                masked = masked[:pos] + masked[pos + 1 :]
                applied.append("stray_closing_quote")

    return _unmask_markers(masked, saved), applied


def normalize_chapter_latin_typography(
    pairs: Sequence[tuple[str, str]],
    *,
    dominant_double_quotes: bool = True,
    unify_quotes: bool = True,
    target_lang: str = "en",
) -> tuple[list[str], int]:
    """Apply chapter-aware typography hygiene across consecutive ``(source, target)`` pairs."""
    results: list[str] = []
    fix_count = 0
    open_quote_carry = False

    # British English, French, and German have their own single/guillemet/low-high quote norms;
    # never force US double-quote rewrites on them.
    effective_double_quotes = dominant_double_quotes and target_lang not in {
        "en-GB",
        "fr",
        "de",
    }

    prose_texts = [_mask_markers(t, _TYPOGRAPHY_PROTECTED_RE)[0] for _, t in pairs if t]
    has_straight = any('"' in t for t in prose_texts)
    has_curly = any(("“" in t or "”" in t) for t in prose_texts)
    should_unify_quotes = unify_quotes and has_straight and has_curly
    # Match the chapter's existing quote style so step 3 never introduces mixed straight/curly quotes.
    quote_pair = ('"', '"') if (has_straight and not has_curly) else ("“", "”")

    n = len(pairs)
    for idx, (source, target) in enumerate(pairs):
        if not target or not target.strip():
            results.append(target)
            continue

        updated = target
        next_src, next_tgt = pairs[idx + 1] if idx + 1 < n else ("", "")

        trimmed = _trim_leaked_next_paragraph(source, updated, next_src, next_tgt)
        if trimmed != updated:
            updated = trimmed
            fix_count += 1

        collapsed = _collapse_stray_prose_dialogue_breaks(source, updated)
        if collapsed != updated:
            updated = collapsed
            fix_count += 1

        cleaned, applied = normalize_latin_typography(
            updated,
            dominant_double_quotes=effective_double_quotes,
            quote_pair=quote_pair,
            source=source,
            open_quote_carry=open_quote_carry,
        )
        if applied and cleaned != updated:
            updated = cleaned
            fix_count += 1

        if should_unify_quotes:
            masked, saved = _mask_markers(updated, _TYPOGRAPHY_PROTECTED_RE)
            unified = _unify_quote_marks(masked)
            if unified != masked:
                updated = _unmask_markers(unified, saved)
                fix_count += 1

        # Update chapter-level open double-quote carry state on prose outside code spans.
        quote_text, _ = _mask_markers(updated, _TYPOGRAPHY_PROTECTED_RE)
        stripped = quote_text.strip()
        dq_count = quote_text.count('"')
        curly_delta = quote_text.count("“") - quote_text.count("”")
        if curly_delta != 0:
            open_quote_carry = (open_quote_carry and curly_delta >= 0) or curly_delta > 0
        elif dq_count % 2 == 1:
            if open_quote_carry and stripped.startswith('"') and not stripped.endswith('"'):
                open_quote_carry = True
            else:
                open_quote_carry = not open_quote_carry
        elif open_quote_carry and (
            (stripped.startswith('"') and dq_count >= 2)
            or (stripped.startswith("“") and "”" in stripped)
        ):
            open_quote_carry = False

        results.append(updated)
    return results, fix_count


def normalize_chapter_cjk_typography(
    pairs: Sequence[tuple[str, str]],
) -> tuple[list[str], int]:
    """Apply conservative, zero-cost typography cleanup for Chinese targets."""
    results: list[str] = []
    fix_count = 0
    for _source, target in pairs:
        if not target or not target.strip():
            results.append(target)
            continue
        masked, saved = _mask_markers(target, _TYPOGRAPHY_PROTECTED_RE)
        changed = False
        if "\\n" in masked and not target.lstrip().startswith("```"):
            updated = _ESCAPED_NEWLINE_RE.sub(lambda m: "\n" * (len(m.group(0)) // 2), masked)
            if updated != masked:
                masked = updated
                changed = True
        if "--" in masked and not target.lstrip().startswith(("```", "---")):
            updated = _RAW_DOUBLE_DASH_RE.sub("——", masked)
            if updated != masked:
                masked = updated
                changed = True
        final = _unmask_markers(masked, saved)
        if changed and final != target:
            fix_count += 1
        results.append(final)
    return results, fix_count


def _is_generic_english(phrase: str, generic_words: frozenset[str]) -> bool:
    if not generic_words:
        return False
    words = _WORD_RE.findall(phrase.casefold())
    if not words:
        return True
    return all(word.replace("’", "'") in generic_words for word in words)


def _latin_boundary_pattern(phrase: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![^\W\d_]){re.escape(phrase)}(?![^\W\d_])")


def _find_split_pinyin_variants(canonical: str, target_text: str) -> list[str]:
    """Detect space-split Title-Case spellings of ``canonical`` (e.g. 'San Zang' for 'Sanzang')."""
    if " " in canonical or len(canonical) < 5 or not canonical.isalpha():
        return []
    if not (canonical[0].isupper() and canonical[1:].islower()):
        return []
    lower = canonical.lower()
    found: list[str] = []
    for split_at in range(2, len(canonical) - 1):
        candidate = canonical[:split_at] + " " + lower[split_at:].capitalize()
        if _latin_boundary_pattern(candidate).search(target_text):
            found.append(candidate)
    return found


def normalize_minority_names(
    pairs: Sequence[tuple[str, str]],
    terms: Sequence[GlossaryTerm],
    variants_by_source: Mapping[str, Sequence[str]] | None = None,
    *,
    target_lang: str = "en",
) -> tuple[list[str], list[dict[str, Any]]]:
    """Deterministically normalize minority name renderings in ``pairs`` to canonical terms.

    Works for both cased scripts (Latin/Cyrillic/Greek) and uncased CJK targets
    (``zh``/``zh-Hant``/``ja``/``ko``), masking protected markers, code spans, and LaTeX.
    """
    generic_words = glossary_generic_target_words(target_lang)
    variants_map = variants_by_source or {}
    is_cjk_target = (target_lang or "").lower().split("-", 1)[0] in {"zh", "ja", "ko"}

    all_canonical_lower = [t.target.strip().casefold() for t in terms if t.target.strip()]

    def collides(variant: str, canonical: str) -> bool:
        """Never replace a variant that is part of another glossary entry's target."""
        v_low, c_low = variant.casefold(), canonical.casefold()
        return any(v_low in other and other != c_low for other in all_canonical_lower)

    eligible_terms: list[tuple[GlossaryTerm, list[tuple[str, re.Pattern[str]]]]] = []
    for term in terms:
        if term.status != STATUS_OK:
            continue
        canonical = term.target.strip()
        if len(canonical) < 2:
            continue
        if is_cjk_target:
            if term.type not in {"person", "place", "organization"}:
                continue
        else:
            if not any(ch.isupper() for ch in canonical):
                continue
            if _is_generic_english(canonical, generic_words):
                continue

        raw_variants = [v.strip() for v in variants_map.get(term.source, ()) if v and v.strip()]
        safe_variants: list[tuple[str, re.Pattern[str]]] = []
        seen_v: set[str] = set()
        for variant in sorted(raw_variants, key=len, reverse=True):
            v_low = variant.casefold()
            if v_low == canonical.casefold() or v_low in seen_v:
                continue
            if len(variant) < 2:
                continue
            if not is_cjk_target:
                if not any(ch.isupper() for ch in variant):
                    continue
                if _is_generic_english(variant, generic_words):
                    continue
            if collides(variant, canonical):
                continue
            seen_v.add(v_low)
            pat = (
                re.compile(re.escape(variant))
                if is_cjk_target
                else _latin_boundary_pattern(variant)
            )
            safe_variants.append((variant, pat))
        eligible_terms.append((term, safe_variants))

    results: list[str] = []
    substitutions: list[dict[str, Any]] = []
    for idx, (source, target) in enumerate(pairs):
        updated = target
        if not updated or not source:
            results.append(updated)
            continue
        masked, saved = _mask_markers(updated, _TYPOGRAPHY_PROTECTED_RE)
        for term, safe_variants in eligible_terms:
            if not source_matches_text(term.source, source):
                continue
            canonical = term.target.strip()
            candidates = [variant for variant, var_pat in safe_variants if var_pat.search(masked)]
            seen_candidates = {v.casefold() for v in candidates}
            if not is_cjk_target:
                for split_var in _find_split_pinyin_variants(canonical, masked):
                    if split_var.casefold() not in seen_candidates and not collides(
                        split_var, canonical
                    ):
                        candidates.append(split_var)
                        seen_candidates.add(split_var.casefold())
            if not candidates:
                continue
            # Match the canonical spelling alongside every variant in one non-overlapping
            # longest-first pass so a shorter variant (e.g. "Gao") never matches inside an
            # existing canonical occurrence (e.g. "Old Gao" -> never "Old Old Gao"), while
            # still normalizing genuine intra-paragraph mixed spellings elsewhere in the text.
            ordered = sorted(dict.fromkeys([canonical, *candidates]), key=len, reverse=True)
            alternatives = [
                rf"(?i:{re.escape(v)})" if v.casefold() == canonical.casefold() else re.escape(v)
                for v in ordered
            ]
            if is_cjk_target:
                combined_pat = re.compile("|".join(alternatives))
            else:
                combined_pat = re.compile(rf"(?<![^\W\d_])(?:{'|'.join(alternatives)})(?![^\W\d_])")
            matched_variants: list[str] = []
            canon_cf = canonical.casefold()

            def _replace(
                match: re.Match[str],
                _canon: str = canonical,
                _canon_cf: str = canon_cf,
            ) -> str:
                matched = match.group(0)
                if matched.casefold() == _canon_cf:
                    return matched
                if matched not in matched_variants:
                    matched_variants.append(matched)
                return _canon

            masked = combined_pat.sub(_replace, masked)
            for variant in matched_variants:
                substitutions.append(
                    {
                        "index": idx,
                        "source": term.source,
                        "from": variant,
                        "to": canonical,
                    }
                )
        results.append(_unmask_markers(masked, saved))
    return results, substitutions
