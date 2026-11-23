"""Deterministic post-translation hygiene and minority-name normalization.

Pure functions with zero model calls:
- ``normalize_latin_typography`` repairs raw ``--`` dashes, inconsistent ``said: "..."``
  speech-tag colons, stray trailing unmatched closing quotes, and isolated single-quote
  dialogue flips in Latin-script targets while preserving protected-object markers.
- ``normalize_minority_names`` replaces known minority renderings of canonical glossary
  entries with the established target using word-boundary and collision guards, logging
  every substitution for auditability.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from ...glossary.store import STATUS_OK, source_matches_text
from ...i18n.languages import glossary_generic_target_words
from ...models.glossary import GlossaryTerm

# Matches protected-object transport tokens such as 〘object:1〙 or ⟦N0⟧ so typography
# and minority-name normalization never alter them.
_PROTECTED_MARKER_RE = re.compile(r"〘object:[A-Za-z0-9_.:-]+〙|⟦[^⟧]+⟧")
# Also shields fenced and inline Markdown code spans during typography normalization so
# literal `\n` escapes, `--` flags, and code quotes are never rewritten.
_TYPOGRAPHY_PROTECTED_RE = re.compile(
    r"〘object:[A-Za-z0-9_.:-]+〙|⟦[^⟧]+⟧"
    r"|(?<!`)(`{3,})(?!`)[\s\S]*?(?<!`)\1(?!`)"
    r"|(?<!`)``(?!`)[^\n]+?(?<!`)``(?!`)"
    r"|(?<!`)`(?!`)[^`\n]+?(?<!`)`(?!`)"
)
_RAW_DOUBLE_DASH_RE = re.compile(r"(?<!-)(?: -- |--)(?!-)")
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
    r"(?:詩曰|诗曰|詞曰|词曰|但見|但见|道是|讚曰|赞曰|偈曰|歌曰|有詩為證|有诗为证|"
    r"詞寄|词寄|西江月|沁園春|沁园春|古風|古风|正是：|真是：|果然是：)"
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
    # Every line must look like a full prose/dialogue sentence (not short verse lines).
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

    Guards against model anticipation from ``[Following source paragraph]`` (e.g. printing
    a following poem both at the end of the intro paragraph and in the poem's own segments).
    """
    if not target or not next_target or "\n" not in target:
        return target
    # If the two sources themselves overlap strongly (legitimate repeated text), never trim.
    src_a, src_b = source.strip(), next_source.strip()
    if src_a and src_b and (src_b in src_a or src_a in src_b):
        return target
    next_seq = _word_seq(next_target)
    if len(next_seq) < 5:
        return target

    # Require the entire trailing blank-line-separated block (at least 5 words) to match
    # next_target's ordered word sequence (full match or exact prefix), so related but
    # distinct sentences (e.g. differing by a negation) are never deleted.
    if "\n\n" in target:
        head, tail = target.rsplit("\n\n", 1)
        tail_seq = _word_seq(tail)
        if head.strip() and 5 <= len(tail_seq) <= len(next_seq):
            if next_seq[: len(tail_seq)] == tail_seq:
                return head.rstrip()
    return target


def normalize_latin_typography(
    text: str,
    *,
    dominant_double_quotes: bool = True,
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
        updated = masked.replace("\\n", "\n")
        if updated != masked:
            masked = updated
            applied.append("escaped_newline")

    # 1. Normalize raw '--' to em dash '—' (skip markdown rules/code fences).
    if "--" in masked and not text.lstrip().startswith(("```", "---")):
        updated = _RAW_DOUBLE_DASH_RE.sub("—", masked)
        if updated != masked:
            masked = updated
            applied.append("em_dash")

    # 2. Normalize dialogue speech-tag colons: 'said: "' -> 'said, "'.
    updated = _SPEECH_COLON_RE.sub(r"\1,\2", masked)
    if updated != masked:
        masked = updated
        applied.append("speech_tag_colon")

    # 3. Repair isolated single-quote dialogue utterances when the book uses double quotes.
    if dominant_double_quotes and ('"' not in masked and "“" not in masked):
        quote_open, quote_close = ("“", "”")
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
        # A straight quote closing an unmatched curly opener, or opening a multi-paragraph
        # speech at paragraph end (after whitespace or when the source ends with an opener),
        # is not a stray closer.
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
) -> tuple[list[str], int]:
    """Apply chapter-aware typography hygiene across consecutive ``(source, target)`` pairs.

    Tracks open multi-paragraph speeches so valid closing quotes at the end of recited poems
    are preserved, unifies mixed curly/straight quote styles, collapses accidental mid-prose
    dialogue newlines, and trims leaked next-paragraph duplicate stanzas.
    """
    results: list[str] = []
    fix_count = 0
    open_quote_carry = False
    # Only unify quote marks when the chapter actually mixes curly and straight double quotes;
    # leave uniformly curly-quoted chapters (e.g. Vietnamese or curly-quote English) intact.
    prose_texts = [_mask_markers(t, _TYPOGRAPHY_PROTECTED_RE)[0] for _, t in pairs if t]
    has_straight = any('"' in t for t in prose_texts)
    has_curly = any(("“" in t or "”" in t) for t in prose_texts)
    should_unify_quotes = unify_quotes and has_straight and has_curly

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
            dominant_double_quotes=dominant_double_quotes,
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
            # A continuing paragraph that repeats an opening quote keeps the speech open.
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


def _is_generic_english(phrase: str, generic_words: frozenset[str]) -> bool:
    words = _WORD_RE.findall(phrase.casefold())
    if not words:
        return True
    return all(word.replace("’", "'") in generic_words for word in words)


def _latin_boundary_pattern(phrase: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![^\W\d_]){re.escape(phrase)}(?![^\W\d_])")


def _auto_space_variants(canonical: str) -> list[str]:
    """Generate safe space-split Pinyin variants for capitalized compound names like Sanzang."""
    if " " in canonical or len(canonical) < 5 or not canonical.isalpha():
        return []
    if not (canonical[0].isupper() and canonical[1:].islower()):
        return []
    # Known safe split only when explicitly observed or matched by two title-cased syllables.
    # Match e.g. "San Zang" -> "Sanzang" via regex check on actual text.
    return []


def _find_split_pinyin_variants(canonical: str, target_text: str) -> list[str]:
    """Detect space-split Title-Case spellings of ``canonical`` (e.g. 'San Zang' for 'Sanzang')."""
    if " " in canonical or len(canonical) < 5 or not canonical.isalpha():
        return []
    if not (canonical[0].isupper() and canonical[1:].islower()):
        return []
    # Split canonical into two parts of length >= 2 and collect any "Part1 Part2" occurrences.
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

    Safety guards:
    - Only applies to ``STATUS_OK`` terms whose canonical target has >=2 chars, contains a
      capital letter, and is not made solely of generic English pronouns/titles.
    - Only replaces a variant ``V`` in paragraph ``i`` when ``term.source`` occurs in
      ``source_i``, the canonical target does not already appear in ``target_i``, and ``V``
      is not a substring of any other glossary term's target.
    - Also fixes space-split Title-Case Pinyin (e.g. ``San Zang`` -> ``Sanzang``).
    """
    generic_words = glossary_generic_target_words(target_lang)
    variants_map = variants_by_source or {}

    all_canonical_lower = [t.target.strip().casefold() for t in terms if t.target.strip()]

    def collides(variant: str, canonical: str) -> bool:
        """Never replace a variant that is part of another glossary entry's target."""
        v_low, c_low = variant.casefold(), canonical.casefold()
        return any(v_low in other and other != c_low for other in all_canonical_lower)

    eligible_terms: list[
        tuple[GlossaryTerm, re.Pattern[str], list[tuple[str, re.Pattern[str]]]]
    ] = []
    for term in terms:
        if term.status != STATUS_OK:
            continue
        canonical = term.target.strip()
        if len(canonical) < 2 or not any(ch.isupper() for ch in canonical):
            continue
        if _is_generic_english(canonical, generic_words):
            continue
        canon_pat = re.compile(
            rf"(?<![^\W\d_]){re.escape(canonical)}(?![^\W\d_])",
            re.IGNORECASE,
        )
        raw_variants = [v.strip() for v in variants_map.get(term.source, ()) if v and v.strip()]
        safe_variants: list[tuple[str, re.Pattern[str]]] = []
        seen_v: set[str] = set()
        for variant in sorted(raw_variants, key=len, reverse=True):
            v_low = variant.casefold()
            if v_low == canonical.casefold() or v_low in seen_v:
                continue
            if len(variant) < 2 or not any(ch.isupper() for ch in variant):
                continue
            if _is_generic_english(variant, generic_words):
                continue
            if collides(variant, canonical):
                continue
            seen_v.add(v_low)
            safe_variants.append((variant, _latin_boundary_pattern(variant)))
        eligible_terms.append((term, canon_pat, safe_variants))

    results: list[str] = []
    substitutions: list[dict[str, Any]] = []
    for idx, (source, target) in enumerate(pairs):
        updated = target
        if not updated or not source:
            results.append(updated)
            continue
        masked, saved = _mask_markers(updated)
        for term, canon_pat, safe_variants in eligible_terms:
            if not source_matches_text(term.source, source):
                continue
            canonical = term.target.strip()
            if canon_pat.search(masked):
                continue
            candidates = [variant for variant, var_pat in safe_variants if var_pat.search(masked)]
            seen_candidates = {v.casefold() for v in candidates}
            for split_var in _find_split_pinyin_variants(canonical, masked):
                if split_var.casefold() not in seen_candidates and not collides(
                    split_var, canonical
                ):
                    candidates.append(split_var)
                    seen_candidates.add(split_var.casefold())
            if not candidates:
                continue
            # Match every variant in one non-overlapping longest-first pass so replacing one
            # spelling never re-matches inside the inserted canonical name.
            ordered = sorted(candidates, key=len, reverse=True)
            combined_pat = re.compile(
                rf"(?<![^\W\d_])(?:{'|'.join(re.escape(v) for v in ordered)})(?![^\W\d_])"
            )
            matched_variants: list[str] = []

            def _replace(match: re.Match[str], _canon: str = canonical) -> str:
                matched = match.group(0)
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
