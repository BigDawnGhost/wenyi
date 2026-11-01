"""Select a token-bounded recent-translation suffix without a separate context cache."""

from __future__ import annotations

from collections.abc import Iterable

from ..ingest.tokens import count_tokens


def render_recent_translations(targets: Iterable[str | None], max_tokens: int) -> str:
    """Read formal targets newest first, then render a contiguous suffix in source order.

    Include complete paragraphs and their newline separators while they fit. If the newest
    nonempty paragraph alone is oversized, use a Unicode-safe tail of that paragraph. Empty
    completed targets are skipped; a pending target ends the contiguous translated history.
    The iterable is consumed lazily, so older chapters need not be loaded once the window fills.
    """
    if max_tokens <= 0:
        return ""
    rendered = ""
    for target in targets:
        if target is None:
            break
        if not target.strip():
            continue
        candidate = f"{target}\n{rendered}" if rendered else target
        tokens = count_tokens(candidate)
        if tokens <= max_tokens:
            rendered = candidate
            if tokens == max_tokens:
                break
            continue
        if not rendered:
            # Slice codepoints, not token bytes: a CJK character or emoji can span tokens.
            low, high = 0, len(target)
            while low < high:
                middle = (low + high + 1) // 2
                if count_tokens(target[-middle:]) <= max_tokens:
                    low = middle
                else:
                    high = middle - 1
            rendered = target[-low:] if low else ""
        break
    return rendered
