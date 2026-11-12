"""Shared token budget helper for segment packing and long-paragraph splits.

Uses a fixed tiktoken encoding (``cl100k_base``) as a universal estimator so batch
sizes track model context more closely than raw character counts, without binding
to any one provider's private tokenizer.
"""

from __future__ import annotations

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
