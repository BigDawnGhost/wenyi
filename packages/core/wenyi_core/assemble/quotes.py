"""Pure-function quote-style normalization applied only during export."""

from __future__ import annotations

import re
from typing import Literal

QuoteStyle = Literal["preserve", "typographic", "straight"]

# Latin-script targets whose quotes can be straightened without changing their style.
LATIN_SCRIPT_TARGETS = frozenset({"en", "fr", "de", "es", "it", "pt", "vi"})
# Typographic mode applies English quote pairing and elisions, so it only fits English.
TYPOGRAPHIC_TARGETS = frozenset({"en"})
# Every curly quotation mark, including low-9 openers and reversed forms, maps to ASCII.
_STRAIGHT_QUOTES = str.maketrans(
    {
        "\u201c": '"',
        "\u201d": '"',
        "\u201e": '"',
        "\u201f": '"',
        "\u2018": "'",
        "\u2019": "'",
        "\u201a": "'",
        "\u201b": "'",
    }
)

_MARKUP_TOKEN_RE = re.compile(r"""(<(?:[^>"']|"[^"]*"|'[^']*')*>|〘[^〙]*〙)""")
_BLOCK_TAG_RE = re.compile(
    r"^</?(?:p|div|li|blockquote|h[1-6]|tr|td|th|dt|dd|section|article|"
    r"header|footer|aside|main|nav|figure|figcaption|br|hr)\b",
    re.IGNORECASE,
)

# Leading word/decade elisions that always take a right single quotation mark (apostrophe).
_ELISION_WORDS = frozenset(
    {
        "em",
        "tis",
        "twas",
        "twere",
        "twill",
        "twould",
        "cause",
        "bout",
        "round",
        "til",
        "neath",
        "n",
        "clock",
        "scuse",
        "cept",
        "fraid",
        "gainst",
        "mongst",
        "cross",
        "fore",
        "mid",
        "midst",
        "tween",
        "twixt",
    }
)
_DECADE_ELISION_RE = re.compile(r"^[0-9]{2}s?(?![A-Za-z0-9])")
_WORD_ELISION_RE = re.compile(r"^([A-Za-z]+)(?![A-Za-z0-9])")

_OPENING_PREDECESSORS = frozenset(" \t\r\n([{\u2014\u2013\u2026/-")
_CLOSING_FOLLOWERS = frozenset(" \t\r\n.,!?;:)]}\u2014\u2013\u2026")


def _base_language(target_lang: str | None) -> str:
    if not target_lang:
        return ""
    return target_lang.strip().replace("_", "-").split("-")[0].lower()


def supports_quote_normalization(target_lang: str | None, style: QuoteStyle = "straight") -> bool:
    """Return True when ``style`` keeps the target language's quote conventions intact."""
    if style == "preserve":
        return False
    base = _base_language(target_lang)
    if style == "typographic":
        return base in TYPOGRAPHIC_TARGETS
    return base in LATIN_SCRIPT_TARGETS


def _is_word_char(char: str) -> bool:
    return bool(char) and (char.isalnum() or char == "_")


def _is_leading_elision(after_text: str) -> bool:
    """Detect leading contractions such as 'em, 'tis, 'cause and '90s."""
    if _DECADE_ELISION_RE.match(after_text):
        return True
    match = _WORD_ELISION_RE.match(after_text)
    if match and match.group(1).lower() in _ELISION_WORDS:
        return True
    return False


def _normalize_typographic_block(tokens: list[str], plain_indices: list[int]) -> None:
    """Normalize one block-bounded slice of plain text tokens in place."""
    raw_plain = [tokens[i] for i in plain_indices]
    normalized_plain = [
        chunk.replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        for chunk in raw_plain
    ]
    raw_joined = "".join(raw_plain)
    joined_plain = "".join(normalized_plain)
    if '"' not in joined_plain and "'" not in joined_plain:
        return

    out_chars = list(joined_plain)
    total = len(out_chars)
    for idx, ch in enumerate(out_chars):
        prev_ch = out_chars[idx - 1] if idx > 0 else ""
        next_ch = out_chars[idx + 1] if idx + 1 < total else ""
        if ch == '"':
            # Decide opening vs closing double quote using immediate neighbors across inline tags.
            if prev_ch in ("", "\u2018", "'") or (
                prev_ch in _OPENING_PREDECESSORS
                and next_ch != ""
                and next_ch not in _CLOSING_FOLLOWERS
            ):
                # Special case: opening double quote immediately before ellipsis ("...and then")
                # or nested single quote ("'Hello'") is opening; after em-dash/ellipsis before
                # space/end ("Wait—") is closing.
                out_chars[idx] = "\u201c"
            elif prev_ch in ("", " ", "\t", "\r", "\n", "(", "[", "{", "\u2018") and next_ch != "":
                out_chars[idx] = "\u201c"
            else:
                out_chars[idx] = "\u201d"
        elif ch == "'":
            # Contraction / intra-word apostrophe: don't, it's, rock'n'roll.
            if _is_word_char(prev_ch) and _is_word_char(next_ch):
                out_chars[idx] = "\u2019"
                continue
            # Leading elision: 'em, '90s, 'tis, 'cause. Preserve an existing explicit opener (‘).
            if (
                raw_joined[idx] != "\u2018"
                and (
                    prev_ch == "" or prev_ch in _OPENING_PREDECESSORS or prev_ch in ("\u201c", '"')
                )
                and _is_leading_elision(joined_plain[idx + 1 :])
            ):
                out_chars[idx] = "\u2019"
                continue
            # Opening single quote when preceded by start/space/opening punct/opening double quote
            # and followed by content (not whitespace or closing punctuation).
            if (
                (
                    prev_ch in ("", "\u201c")
                    or (
                        prev_ch in _OPENING_PREDECESSORS
                        and next_ch != ""
                        and next_ch not in _CLOSING_FOLLOWERS
                    )
                )
                and next_ch != ""
                and next_ch not in (" ", "\t", "\r", "\n")
            ):
                out_chars[idx] = "\u2018"
            else:
                out_chars[idx] = "\u2019"

    cursor = 0
    for seg_pos, token_index in enumerate(plain_indices):
        length = len(normalized_plain[seg_pos])
        tokens[token_index] = "".join(out_chars[cursor : cursor + length])
        cursor += length


def _normalize_typographic(text: str) -> str:
    """Convert straight/curly quotes to unified typographic “ ” ‘ ’ while preserving markup."""
    # Split into alternating (plain_text, markup_token) segments so attributes inside HTML tags
    # and protected-object markers are never altered. Inline tags stay transparent to neighbor
    # lookup while block-level tags reset paragraph boundaries.
    tokens = _MARKUP_TOKEN_RE.split(text)
    block_indices: list[int] = []
    for idx, token in enumerate(tokens):
        if idx % 2 == 0:
            block_indices.append(idx)
        elif _BLOCK_TAG_RE.match(token):
            if block_indices:
                _normalize_typographic_block(tokens, block_indices)
                block_indices = []
    if block_indices:
        _normalize_typographic_block(tokens, block_indices)
    return "".join(tokens)


def _normalize_straight(text: str) -> str:
    """Convert typographic quotes to straight ASCII quotes while preserving markup tokens."""
    tokens = _MARKUP_TOKEN_RE.split(text)
    for i in range(0, len(tokens), 2):
        tokens[i] = tokens[i].translate(_STRAIGHT_QUOTES)
    return "".join(tokens)


def normalize_quotes(
    text: str,
    style: QuoteStyle = "preserve",
    *,
    target_lang: str = "en",
) -> str:
    """Normalize quotes in ``text`` for export according to ``style`` and ``target_lang``.

    ``straight`` applies to Latin-script targets; ``typographic`` produces English curly
    quotes and elisions, so it applies to English targets only. Other targets, and
    ``preserve``, return ``text`` unchanged. HTML tags and protected-object markers are kept
    verbatim.
    """
    if not text or style == "preserve" or not supports_quote_normalization(target_lang, style):
        return text
    if style == "straight":
        return _normalize_straight(text)
    if style == "typographic":
        return _normalize_typographic(text)
    raise ValueError(f"Unsupported quote_style: {style!r}")
