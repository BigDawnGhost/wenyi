"""Normalize export-copy punctuation to mainland Simplified Chinese conventions.
Convert Japanese and straight ASCII quotes to paired curly forms while preserving
apostrophes. Convert half-width sentence punctuation adjacent to CJK into full-width forms.
Normalize ellipses and dashes to Chinese doubled forms.
Be conservative around English and numbers: preserve internal punctuation in examples such
as 9.11 and Mr. Smith. These deterministic transformations affect only disposable export
copies, never formal state.
"""

from __future__ import annotations

import re

_CJK = (
    "一-鿿"  # CJK unified ideographs.
    "぀-ヿ"  # Kana, included conservatively.
    "＀-￯"  # Full-width symbols.
    "“”‘’（）《》【】、，。！？：；…—"
)
_CJK_RE = f"[{_CJK}]"

# Half-width to full-width punctuation.
_HALF_TO_FULL = {",": "，", ".": "。", "!": "！", "?": "？", ":": "：", ";": "；"}


def _convert_quotes(
    text: str,
    *,
    double_open: bool = True,
    single_open: bool = True,
) -> tuple[str, bool, bool]:
    """Convert Japanese/ASCII quotes and return updated single/double-quote state."""
    # Map Japanese quotation marks directly.
    text = text.translate(str.maketrans({"「": "“", "」": "”", "『": "‘", "』": "’"}))

    # Alternate straight double quotes between opening and closing curly forms.
    out = []
    for ch in text:
        if ch == '"':
            out.append("“" if double_open else "”")
            double_open = not double_open
        else:
            out.append(ch)
    text = "".join(out)

    # Apostrophes within words do not change quote state. Trailing apostrophes and closing quotes
    # both use the right-curly form, but only close quote state when already inside a quotation.
    out = []
    for index, ch in enumerate(text):
        if ch == "'":
            before = text[index - 1] if index else ""
            after = text[index + 1] if index + 1 < len(text) else ""
            before_letter = before.isascii() and before.isalpha()
            after_letter = after.isascii() and after.isalpha()
            if before_letter and after_letter:
                out.append("’")
            elif before_letter and not single_open:
                out.append("’")
                single_open = True
            elif before_letter:
                out.append("’")
            else:
                out.append("‘" if single_open else "’")
                single_open = not single_open
        else:
            out.append(ch)
    return "".join(out), double_open, single_open


def _convert_ellipsis_dash(text: str) -> str:
    """Normalize ellipsis and dash variants to Chinese doubled forms."""
    text = re.sub(r"。{3,}", "……", text)
    text = re.sub(r"・{2,}", "……", text)
    text = re.sub(r"\.{3,}", "……", text)
    text = re.sub(r"…+", "……", text)  # Normalize one or more ellipsis symbols to the doubled form.
    text = re.sub(r"-{2,}", "——", text)
    text = re.sub(r"—{1,}", "——", text)  # Normalize em dashes to the doubled form.
    return text


def _convert_halfwidth(text: str) -> str:
    """Convert half-width sentence punctuation adjacent to CJK into full-width forms."""

    def repl(m: re.Match) -> str:
        """Replace matched half-width punctuation through the mapping table."""
        return _HALF_TO_FULL[m.group(0)]

    # Convert when CJK is on the left. With CJK only on the right, preserve punctuation following
    # ASCII letters/digits to avoid corrupting abbreviation or version boundaries next to CJK.
    pattern = re.compile(rf"(?<={_CJK_RE})[,.!?:;]|[,.!?:;](?={_CJK_RE})")
    return pattern.sub(
        lambda match: (
            match.group(0)
            if match.start() > 0
            and text[match.start() - 1].isascii()
            and text[match.start() - 1].isalnum()
            else repl(match)
        ),
        text,
    )


def _normalize_with_quote_state(
    text: str,
    *,
    double_open: bool,
    single_open: bool,
) -> tuple[str, bool, bool]:
    """Normalize one paragraph using the supplied quote state and return the new state."""
    if not text:
        return text, double_open, single_open
    text, double_open, single_open = _convert_quotes(
        text,
        double_open=double_open,
        single_open=single_open,
    )
    text = _convert_ellipsis_dash(text)
    text = _convert_halfwidth(text)
    text = re.sub(r"([，。！？：；、])\s+", r"\1", text)
    text = re.sub(rf"([”’》】])\s+(?={_CJK_RE})", r"\1", text)
    return text, double_open, single_open


def normalize_zh(text: str) -> str:
    """Normalize a Chinese translation paragraph to Simplified Chinese full-width punctuation."""
    normalized, _, _ = _normalize_with_quote_state(
        text,
        double_open=True,
        single_open=True,
    )
    return normalized


def normalize_zh_segments(
    texts: list[str],
    continuations: list[bool] | None = None,
) -> list[str]:
    """Normalize logical paragraphs, carrying quote state only across cont=True continuations.
    Unbalanced quotes in an ordinary paragraph must not affect the next paragraph and cause
    cascading changes.
    """
    if continuations is None:
        continuations = [False] * len(texts)
    if len(continuations) != len(texts):
        raise ValueError("texts and continuations must have the same length")

    normalized: list[str] = []
    double_open = True
    single_open = True
    for index, (text, continuation) in enumerate(zip(texts, continuations)):
        if index == 0 or not continuation:
            double_open = True
            single_open = True
        value, double_open, single_open = _normalize_with_quote_state(
            text,
            double_open=double_open,
            single_open=single_open,
        )
        normalized.append(value)
    return normalized
