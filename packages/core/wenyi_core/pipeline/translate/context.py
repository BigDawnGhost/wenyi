"""Select a token-bounded recent-translation suffix without a separate context cache."""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from ...ingest.tokens import count_tokens
from ...models.document import Chapter, Segment
from ...storage.protocol import Storage
from .babeldoc_context import BabeldocContext


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


def neighboring_chapters(store: Storage, chapter: int, *, following: bool) -> Iterator[Chapter]:
    """Load neighboring chapters lazily in stable source order."""
    indices = sorted(
        row["index"]
        for row in store.load_manifest().get("chapters", [])
        if isinstance(row.get("index"), int)
        and (row["index"] > chapter if following else row["index"] < chapter)
    )
    for index in indices if following else reversed(indices):
        yield store.load_chapter(index)


def recent_chapter_context(
    store: Storage,
    chapter: Chapter,
    start: int,
    policy: BabeldocContext | None,
    *,
    max_tokens: int,
    prefer_drafts: bool = False,
) -> str:
    """Read formal translated prefix backward across chapters within ``max_tokens``."""

    def target(segment: Segment) -> str | None:
        if prefer_drafts and segment.target_before_polish is not None:
            return segment.target_before_polish
        return segment.target

    def targets() -> Iterator[str | None]:
        if policy is not None:
            yield from reversed(policy.recent_targets(chapter.text_segments, start, target))
            if start and policy.sections[start - 1] != 0:
                return
        else:
            yield from (target(segment) for segment in reversed(chapter.text_segments[:start]))
        for previous in neighboring_chapters(store, chapter.index, following=False):
            segments = previous.text_segments
            previous_policy = BabeldocContext.for_chapter(previous)
            if previous_policy is None:
                yield from (target(segment) for segment in reversed(segments))
            else:
                yield from reversed(previous_policy.recent_targets(segments, len(segments), target))
                if previous_policy.sections and previous_policy.sections[-1] != 0:
                    return

    return render_recent_translations(targets(), max_tokens)


def following_chapter_source(
    store: Storage,
    chapter: Chapter,
    end: int,
    policy: BabeldocContext | None,
) -> str:
    """Find one following source reference in book order without translating it early."""
    segments = chapter.text_segments
    if policy is None:
        if end < len(segments):
            return segments[end].source
    else:
        if end <= 0 or not policy.body[end - 1]:
            return ""
        reference = policy.next_source(segments, end)
        if reference:
            return reference
        if policy.sections[-1] != policy.sections[end - 1]:
            return ""
    for following in neighboring_chapters(store, chapter.index, following=True):
        segments = following.text_segments
        following_policy = BabeldocContext.for_chapter(following)
        if following_policy is None:
            if segments:
                return segments[0].source
        else:
            reference = following_policy.next_source(segments, 0)
            if reference:
                return reference
            if any(following_policy.sections):
                return ""
    return ""
