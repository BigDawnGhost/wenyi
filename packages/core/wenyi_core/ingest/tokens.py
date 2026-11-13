"""Shared token budget helper for segment packing and long-paragraph splits.

Uses a fixed tiktoken encoding (``cl100k_base``) as a universal estimator so batch
sizes track model context more closely than raw character counts, without binding
to any one provider's private tokenizer.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import tiktoken

ENCODING_NAME = "cl100k_base"


@lru_cache(maxsize=1)
def _encoding() -> "tiktoken.Encoding":
    # Imported on first use: loading the vocabulary costs far more than a CLI start.
    import tiktoken

    return tiktoken.get_encoding(ENCODING_NAME)


def count_tokens(text: str) -> int:
    """Return the cl100k_base token count for ``text`` (0 for empty)."""
    if not text:
        return 0
    return len(_encoding().encode(text))


def split_balanced_tokens(text: str, max_tokens: int) -> list[str]:
    """Split losslessly into balanced, token-bounded chunks in source order.

    Start with ceil(tokens / budget) chunks, adjusting cuts to complete UTF-8 characters.
    Recheck each decoded chunk: tokenization can change at a new string boundary. If
    character boundaries prevent that partition, try one more chunk and rebalance.
    """
    if max_tokens <= 0:
        raise ValueError("Token budget must be positive")
    if not text:
        return []
    encoding = _encoding()
    tokens = encoding.encode(text)
    total = len(tokens)
    if total <= max_tokens:
        return [text]

    source = text.encode("utf-8")
    byte_offsets = [0]
    boundaries = []
    for index, token in enumerate(tokens, 1):
        end = byte_offsets[-1] + len(encoding.decode_single_token_bytes(token))
        byte_offsets.append(end)
        # A token can end inside a multibyte character; never decode such a slice.
        if end == len(source) or source[end] & 0xC0 != 0x80:
            boundaries.append(index)

    minimum = (total + max_tokens - 1) // max_tokens
    for parts in range(minimum, len(boundaries) + 1):
        chunks: list[str] = []
        start = 0
        for remaining in range(parts, 0, -1):
            target = start + (total - start) // remaining
            lower = max(start + 1, total - (remaining - 1) * max_tokens)
            upper = min(start + max_tokens, total - remaining + 1)
            candidates = boundaries[
                bisect_left(boundaries, lower) : bisect_right(boundaries, upper)
            ]
            for end in sorted(
                candidates,
                key=lambda end: (
                    abs(end - target),
                    abs((end - start) * remaining - (total - start)),
                    end,
                ),
            ):
                chunk = source[byte_offsets[start] : byte_offsets[end]].decode("utf-8")
                if count_tokens(chunk) <= max_tokens:
                    chunks.append(chunk)
                    start = end
                    break
            else:
                break
        if start == total:
            return chunks
    raise ValueError("Token budget is too small to split text at complete Unicode characters")
