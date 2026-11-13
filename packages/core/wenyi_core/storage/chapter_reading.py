"""Read projections for continuous chapter readers, separate from mutable domain models."""

from typing import Protocol

# Match Python str.strip(), including non-ASCII whitespace, on both SQL adapters.
SOURCE_WHITESPACE = "".join(chr(code) for code in range(0x3001) if chr(code).isspace())
MAX_WINDOW = 256


def validate_window(offset: int, limit: int) -> None:
    if offset < 0 or not 1 <= limit <= MAX_WINDOW:
        raise ValueError("Invalid chapter window")


class ChapterReader(Protocol):
    """Return bounded projections without constructing a full Chapter or Document."""

    def read_chapter_window(
        self,
        ci: int,
        *,
        offset: int,
        limit: int,
        around: int | None = None,
        parsed: dict | None = None,
    ) -> dict: ...

    def read_chapter_targets(self, ci: int, indices: list[int]) -> list[dict]: ...
