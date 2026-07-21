"""Deterministic English quotation-mark normalization."""

from __future__ import annotations

from ...locales import message as ui_message


enabled = True


_ASCII_QUOTE_TRANSLATION = str.maketrans(
    {
        # Curly and low/high English-style double quotation marks.
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
        "«": '"',
        "»": '"',
        # Curly and low/high single quotation marks, including apostrophes.
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "‹": "'",
        "›": "'",
        # Full-width quotation marks.
        "＂": '"',
        "＇": "'",
        # Japanese corner brackets and their vertical-text variants.
        "「": '"',
        "」": '"',
        "〝": '"',
        "〞": '"',
        "〟": '"',
        "『": "'",
        "』": "'",
    }
)


def normalize(text: str) -> str:
    """Convert common non-ASCII quotation marks to straight ASCII forms."""
    return text.translate(_ASCII_QUOTE_TRANSLATION)


def normalize_segments(
    texts: list[str], continuations: list[bool] | None = None
) -> list[str]:
    """Normalize each segment independently without changing other punctuation."""
    if continuations is not None and len(continuations) != len(texts):
        raise ValueError(ui_message("error.punctuation_segment_count_mismatch"))
    return [normalize(text) for text in texts]
