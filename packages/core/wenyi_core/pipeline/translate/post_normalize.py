"""Minimal deterministic post-translation hygiene strictly proven by the source paragraph.

Only one transform belongs here: turning a leaked literal ``\\n`` escape glued to prose
into a real newline when the source paragraph contains no literal ``\\n`` of its own,
skipping protected-object markers, fenced/inline code spans, and URLs.

Punctuation, quotation marks, dashes, dialogue tags, and names belong to the prompt,
glossary, sparse polish, and Review — never deterministic regex rewrites.
"""

from __future__ import annotations

import re

# Match leaked literal ``\n`` escapes (followed by non-lowercase, or preceded by prose
# punctuation) while leaving ``\nu``, ``\frac{1}{\nu}``, and ``dir\new`` untouched.
_ESCAPED_NL_RE = re.compile(r"(?<!\\)\\n(?![a-z])|(?<=[.!?;:,\u2014\u2026\"'”’）】」』》])\\n")
_PROTECTED_RE = re.compile(
    r"〘object:[^〙]+〙|⟦[^⟧]+⟧|```[\s\S]*?```|`[^`\n]+`|\$\$[\s\S]*?\$\$|\$[^$\n]+\$|"
    r"https?://\S+|[A-Za-z]:\\[^\s]+",
)


def repair_escaped_newlines(source: str, target: str) -> str:
    """Turn leaked literal ``\\n`` escapes into real newlines when the source has none."""
    if not target or "\\n" not in target or "\\n" in source:
        return target
    parts: list[str] = []
    cursor = 0
    for match in _PROTECTED_RE.finditer(target):
        if match.start() > cursor:
            parts.append(_ESCAPED_NL_RE.sub("\n", target[cursor : match.start()]))
        parts.append(match.group(0))
        cursor = match.end()
    if cursor < len(target):
        parts.append(_ESCAPED_NL_RE.sub("\n", target[cursor:]))
    return "".join(parts)
