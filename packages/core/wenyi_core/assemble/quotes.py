"""Pure-function quote-style normalization applied only during export."""

from __future__ import annotations

import re
from typing import Literal

QuoteStyle = Literal["preserve", "typographic", "straight"]

# Latin-script targets where English/typographic curly vs straight quotes apply.
LATIN_SCRIPT_TARGETS = frozenset({"en", "fr", "de", "es", "it", "pt", "vi"})

_MARKUP_TOKEN_RE = re.compile(r"(<[^>]+>|〘[^〙]*〙)")

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


def supports_quote_normalization(target_lang: str | None) -> bool:
    """Return True when the target language uses Latin script quote conventions."""
    if not target_lang:
        return False
    base = target_lang.strip().replace("_", "-").split("-")[0].lower()
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


def _normalize_typographic(text: str) -> str:
    """Convert straight/curly quotes to unified typographic “ ” ‘ ’ while preserving markup."""
    # Split into alternating (plain_text, markup_token) segments so attributes inside HTML tags
    # and protected-object markers are never altered, while neighbor lookup crosses tag boundaries.
    tokens = _MARKUP_TOKEN_RE.split(text)
    plain_indices = [i for i in range(0, len(tokens), 2)]
    # Normalize any pre-existing curly quotes in plain segments first so output is idempotent.
    normalized_plain: list[str] = []
    for i in plain_indices:
        chunk = (
            tokens[i]
            .replace("\u201c", '"')
            .replace("\u201d", '"')
            .replace("\u2018", "'")
            .replace("\u2019", "'")
        )
        normalized_plain.append(chunk)

    joined_plain = "".join(normalized_plain)
    if '"' not in joined_plain and "'" not in joined_plain:
        return text

    out_chars = list(joined_plain)
    total = len(out_chars)
    for idx, ch in enumerate(out_chars):
        prev_ch = out_chars[idx - 1] if idx > 0 else ""
        next_ch = out_chars[idx + 1] if idx + 1 < total else ""
        if ch == '"':
            # Decide opening vs closing double quote using immediate neighbors across tag boundaries.
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
            # Leading elision: 'em, '90s, 'tis, 'cause.
            if (
                prev_ch == "" or prev_ch in _OPENING_PREDECESSORS or prev_ch in ("\u201c", '"')
            ) and _is_leading_elision(joined_plain[idx + 1 :]):
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

    # Reassemble plain segments with untouched markup tokens.
    rebuilt = list(tokens)
    cursor = 0
    for seg_pos, token_index in enumerate(plain_indices):
        length = len(normalized_plain[seg_pos])
        rebuilt[token_index] = "".join(out_chars[cursor : cursor + length])
        cursor += length
    return "".join(rebuilt)


def _normalize_straight(text: str) -> str:
    """Convert typographic quotes to straight ASCII quotes while preserving markup tokens."""
    tokens = _MARKUP_TOKEN_RE.split(text)
    for i in range(0, len(tokens), 2):
        tokens[i] = (
            tokens[i]
            .replace("\u201c", '"')
            .replace("\u201d", '"')
            .replace("\u2018", "'")
            .replace("\u2019", "'")
        )
    return "".join(tokens)


def normalize_quotes(
    text: str,
    style: QuoteStyle = "preserve",
    *,
    target_lang: str = "en",
) -> str:
    """Normalize quotes in ``text`` for export according to ``style`` and ``target_lang``.

    Returns ``text`` unchanged when ``style == "preserve"`` or when ``target_lang`` is not a
    Latin-script language. Preserves HTML tags and protected-object markers verbatim.
    """
    if not text or style == "preserve" or not supports_quote_normalization(target_lang):
        return text
    if style == "straight":
        return _normalize_straight(text)
    if style == "typographic":
        return _normalize_typographic(text)
    raise ValueError(f"Unsupported quote_style: {style!r}")
