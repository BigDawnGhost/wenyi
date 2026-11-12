"""Decode raw source bytes with an explicit fallback ladder; never guess silently.

UTF-8 stays the primary contract. Legacy encodings are tried from a fixed whitelist,
and when several of them decode the same bytes into different texts the file is reported
as ambiguous instead of silently decoded with the wrong code page — a Shift-JIS novel
misread as GBK (or the reverse) corrupts every character while still "succeeding".
"""

from __future__ import annotations

import codecs

from ..models.errors import IngestError

# Order matters only for tie-breaking; ambiguity detection does not depend on it.
_CANDIDATES = ("shift_jis", "cp936", "cp1252")


def _has_fullwidth_kana(text: str) -> bool:
    return any("぀" <= character <= "ヿ" for character in text)


def _cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    cjk = sum(1 for character in text if "一" <= character <= "鿿" or "぀" <= character <= "ヿ")
    return cjk / len(text)


def _resolve_ambiguous(decoded: list[tuple[str, str]]) -> tuple[str, str]:
    """Break a legacy-encoding tie with textual signals, or reject the file.

    Full-width kana is the reliable Shift-JIS signal. Otherwise a clear CJK-ideograph
    majority separates a real code-page match from Windows-1252 mojibake. Without a
    decisive signal the file is reported as ambiguous instead of decoded with the wrong
    code page — a misread "succeeds" while corrupting every character.
    """
    kana = [encoding for text, encoding in decoded if _has_fullwidth_kana(text)]
    if len(kana) == 1:
        return next(item for item in decoded if item[1] == kana[0])
    scored = sorted(decoded, key=lambda item: _cjk_ratio(item[0]), reverse=True)
    if (
        scored
        and _cjk_ratio(scored[0][0]) >= 0.3
        and _cjk_ratio(scored[0][0]) - _cjk_ratio(scored[1][0]) >= 0.2
    ):
        return scored[0]
    raise IngestError(
        "Source file encoding is ambiguous: it decodes differently as "
        f"{', '.join(encoding for _text, encoding in decoded)}; convert the file to UTF-8 first"
    )


def decode_source(data: bytes) -> tuple[str, str]:
    """Return ``(text, encoding_name)`` for raw source bytes or raise :class:`IngestError`.

    UTF-8 (with or without BOM) is accepted as-is. Legacy candidates are tried strictly;
    multiple diverging decodes resolve only when exactly one candidate produced
    full-width kana (the reliable Shift-JIS signal), otherwise the ambiguity is raised.
    """
    if data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
        return data.decode("utf-16"), "utf-16"
    try:
        return data.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        pass

    decoded: list[tuple[str, str]] = []
    for encoding in _CANDIDATES:
        try:
            decoded.append((data.decode(encoding), encoding))
        except UnicodeDecodeError:
            continue
    if not decoded:
        raise IngestError(
            "Source file is not valid UTF-8 and no supported legacy encoding "
            "(Shift-JIS, GBK, Windows-1252) matches; convert the file to UTF-8 first"
        )
    if len(decoded) == 1:
        return decoded[0]
    distinct = {text for text, _encoding in decoded}
    if len(distinct) == 1:
        return decoded[0]
    return _resolve_ambiguous(decoded)
