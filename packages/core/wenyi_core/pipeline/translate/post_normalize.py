"""Deterministic post-translation repairs that hold for any book style.

Pure functions with zero model calls. A rule belongs here only when the paragraph's own
source proves the target is wrong:
- ``repair_escaped_newlines`` turns a leaked literal ``\\n`` escape glued to the text into
  a real line break, but only when the source paragraph has no literal ``\\n`` of its own,
  and never inside protected-object markers, Markdown code spans, LaTeX math spans or URLs.

Quotation marks, dashes, speech tags, line layout and names are deliberately left to the
model, the glossary prompt, Review and the opt-in export-time ``output.quote_style``:
deterministic rewrites of them corrupted correct text in other languages (case endings,
plurals), genres (scripts, technical manuals) and house styles.
"""

from __future__ import annotations

import re

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
# LaTeX commands beginning with '\n' whose suffixes should never be mistaken for leaked newlines
# even when written outside math delimiters immediately after punctuation (e.g. "a,\nu", ".\nRightarrow").
_LATEX_N_CMD_SUFFIXES = (
    r"eq|e|eg|i|abla|approx|leq|geq|leqslant|geqslant|less|gtr|mid|parallel|vdash|Vdash|vDash|VDash|"
    r"cong|sim|subseteq|supseteq|subset|supset|rightarrow|leftarrow|leftrightarrow|"
    r"Rightarrow|Leftarrow|Leftrightarrow|earrow|warrow|exists|shortmid|shortparallel|"
    r"succ|prec|triangleleft|triangleright|otin|olimits|obreak|ewline|ewpage|ewcommand|"
    r"ewenvironment|ewtheorem|oindent|olinebreak|umberwithin|onumber|opagebreak|ormalfont|"
    r"ormalsize|otag|ocite"
)
# Bare '\nu' is protected unless followed by a hyphen/dot/letter (so "a,\nu-boats" still normalizes).
_LATEX_N_GUARD = rf"(?:(?:{_LATEX_N_CMD_SUFFIXES})(?![A-Za-z])|u(?![A-Za-z.\-]))"
# After a period '.', also protect relative paths like '.\new' and bare '.\not'.
_PERIOD_N_GUARD = rf"(?:(?:{_LATEX_N_CMD_SUFFIXES}|ew|ot)(?![A-Za-z])|u(?![A-Za-z.\-]))"
# Match standalone leaked literal '\n' line breaks without touching LaTeX commands
# ('\neq', '\ne', '\nu', '\nabla', '\nRightarrow'), escaped backslashes ('\\n'), or paths ('C:\new', '.\new').
_ESCAPED_NEWLINE_RE = re.compile(
    rf"(?<=[,;!?…—\"”'’】》」』])(?<!\\)(?:\\n)+(?=[A-Za-z])(?!{_LATEX_N_GUARD})"
    rf"|(?<=\.)(?<!\\)(?:\\n)+(?=[A-Za-z])(?!{_PERIOD_N_GUARD})"
    # A standalone escape run must be glued to the preceding text and taken whole: "as \\n."
    # or "use \\n\\n to" in prose is notation, never a half-converted run.
    r"|(?<![\s\\])(?<!\\n)(?:\\n)+(?!\\n)(?![A-Za-z])"
)


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


def repair_escaped_newlines(source: str, target: str) -> str:
    """Turn leaked literal ``\\n`` escapes in ``target`` into real line breaks.

    A literal backslash-n that the source paragraph also contains is content (a programming
    book explaining escape sequences, a Windows path), so the target is left unchanged.
    """
    if not target or "\\n" not in target or "\\n" in (source or ""):
        return target
    if target.lstrip().startswith("```"):
        return target
    masked, saved = _mask_markers(target, _TYPOGRAPHY_PROTECTED_RE)
    updated = _ESCAPED_NEWLINE_RE.sub(lambda m: "\n" * (len(m.group(0)) // 2), masked)
    if updated == masked:
        return target
    return _unmask_markers(updated, saved)
