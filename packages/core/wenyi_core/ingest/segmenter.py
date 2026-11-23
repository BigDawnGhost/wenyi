"""Dispatch document readers and split translation batches.
load_document selects a reader by extension and optionally splits long segments.
batch_segments groups chapter segments by a shared token budget and requires equally
sized model output for alignment. split_long_segments splits oversized segments at
sentences, marks continuations and lets the writer merge them into the original
paragraph/EPUB element.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from copy import deepcopy

from .epub_reader import read_epub
from .fb2_reader import read_fb2
from .html_reader import read_html
from .models import KIND_TEXT, Chapter, Document, Segment
from .pdf_reader import read_pdf
from .text_reader import read_text
from .tokens import count_tokens

# Common sentence-ending punctuation used for splitting long paragraphs.
_SENT_SPLIT = re.compile(r"(?<=[。．.!！？!?…\n])")


def _prefix_within_tokens(text: str, max_tokens: int) -> int:
    """Largest character end index with ``count_tokens(text[:end]) <= max_tokens``.

    Prefers a nearby whitespace cut when one exists inside the token-safe prefix so
    hard mid-word splits remain a last resort.
    """
    if not text:
        return 0
    if max_tokens <= 0:
        return 0
    if count_tokens(text) <= max_tokens:
        return len(text)

    low, high = 1, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if count_tokens(text[:mid]) <= max_tokens:
            low = mid
        else:
            high = mid - 1
    end = low
    # Prefer breaking on whitespace inside the safe prefix when the remainder is long.
    for sep in (" ", "\t", "\n"):
        cut = text.rfind(sep, 0, end + 1)
        if cut > 0:
            return cut
    return end


def _split_oversized_sentence(text: str, max_tokens: int) -> list[str]:
    """Split an oversized sentence within the token budget; hard-split only as a fallback."""
    chunks: list[str] = []
    rest = text
    while rest and count_tokens(rest) > max_tokens:
        cut = _prefix_within_tokens(rest, max_tokens)
        if cut <= 0:
            # A single token wider than the budget cannot shrink further; emit one codepoint.
            cut = max(1, min(len(rest), 1))
        chunks.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        chunks.append(rest)
    return chunks


def _split_text(text: str, max_tokens: int) -> list[str]:
    """Greedily group sentences by token length, falling back for oversized sentences."""
    chunks: list[str] = []
    cur = ""
    for part in _SENT_SPLIT.split(text):
        if not part:
            continue
        part_tokens = count_tokens(part)
        if part_tokens > max_tokens:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.extend(_split_oversized_sentence(part, max_tokens))
            continue
        if cur and count_tokens(cur + part) > max_tokens:
            chunks.append(cur)
            cur = ""
        cur += part
    if cur:
        chunks.append(cur)
    return chunks or [text]


def split_long_segments(chapters: list[Chapter], max_tokens: int) -> None:
    """Split oversized segments in place; mark continuations cont=True without independent
    anchors.
    """
    if not max_tokens or max_tokens <= 0:
        return
    for ch in chapters:
        new_segs: list[Segment] = []
        idx = 0
        for s in ch.segments:
            if count_tokens(s.source) <= max_tokens:
                s.index = idx
                new_segs.append(s)
                idx += 1
                continue
            for k, piece in enumerate(_split_text(s.source, max_tokens)):
                if k == 0:
                    new_segs.append(
                        Segment(
                            index=idx,
                            source=piece,
                            kind=s.kind,
                            anchor=s.anchor,
                            resource_href=s.resource_href,
                            cont=False,
                            meta=deepcopy(s.meta),
                        )
                    )
                else:  # Merge continuations back into the first segment; they have no independent anchor.
                    new_segs.append(
                        Segment(
                            index=idx,
                            source=piece,
                            kind=KIND_TEXT,
                            anchor=None,
                            resource_href=s.resource_href,
                            cont=True,
                        )
                    )
                idx += 1
        ch.segments = new_segs


def load_document(
    path: str,
    source_lang: str,
    target_lang: str,
    split_segments: int = 0,
    *,
    cache_dir: str | None = None,
    source_hash: str | None = None,
    pdf_backend: str = "mineru",
    babeldoc_bridge_url: str = "http://127.0.0.1:8765",
    babeldoc_pages: str | None = None,
    babeldoc_timeout: float = 600.0,
    mineru_token_resolver: Callable[[], str | None] | None = None,
) -> Document:
    """Dispatch by extension; resolve MinerU credentials only for uncached PDF conversion."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".epub":
        doc = read_epub(path, source_lang, target_lang)
    elif ext in (".md", ".markdown", ".txt", ".text"):
        doc = read_text(path, source_lang, target_lang)
    elif ext == ".fb2":
        doc = read_fb2(path, source_lang, target_lang)
    elif ext in (".html", ".htm", ".xhtml"):
        doc = read_html(path, source_lang, target_lang)
    elif ext == ".pdf":
        if cache_dir is None:
            raise ValueError("PDF input requires a run-state cache directory")
        if pdf_backend == "babeldoc":
            from .pdf_babeldoc import read_pdf_babeldoc

            doc = read_pdf_babeldoc(
                path,
                source_lang,
                target_lang,
                bridge_url=babeldoc_bridge_url,
                pages=babeldoc_pages,
                cache_dir=cache_dir,
                timeout=babeldoc_timeout,
            )
        else:
            doc = read_pdf(
                path,
                source_lang,
                target_lang,
                cache_dir=cache_dir,
                source_hash=source_hash,
                mineru_token_resolver=mineru_token_resolver,
            )
    elif ext == ".docx":
        from .docx_reader import read_docx

        doc = read_docx(path, source_lang, target_lang)
    else:
        raise ValueError(
            f"Unsupported format: {ext} (supported: .epub / .txt / .md / .fb2 / .html / .xhtml / .pdf / .docx)"
        )

    # BabelDOC IDs are tied to layout; never split those paragraphs by token budget.
    if split_segments and split_segments > 0 and not (doc.meta or {}).get("babeldoc"):
        split_long_segments(doc.chapters, split_segments)
    return doc


_CJK_COUPLET_RE = re.compile(
    r"^([\u3400-\u9fff\uf900-\ufaff]{3,10})[，、；]([\u3400-\u9fff\uf900-\ufaff]{3,10})[。！？；]?$"
)
_VERSE_DIALOGUE_MARKS = frozenset('“”"「」『』')


def is_verse_segment(text: str) -> bool:
    """Detect standalone verse/couplet lines so batching avoids cleaving a poem mid-stanza."""
    stripped = text.strip()
    if not stripped or len(stripped) > 48:
        return False
    # Allow a single trailing closing quote on the final couplet of a recited ballad.
    core = stripped.rstrip('」』”"')
    if any(ch in _VERSE_DIALOGUE_MARKS for ch in core):
        return False
    match = _CJK_COUPLET_RE.match(core)
    if match is not None:
        # Require symmetrical couplet hemistich lengths (e.g. 5+5, 7+7, 4+4, or off-by-1) so
        # asymmetric short narrative sentences (e.g. "他站起身，仔细打量着眼前的书生。") are
        # not misclassified as poetry.
        left, right = match.group(1), match.group(2)
        if abs(len(left) - len(right)) <= 1:
            return True
    lines = [line.strip() for line in core.splitlines() if line.strip()]
    return len(lines) >= 2 and all(len(line) <= 28 for line in lines)


_is_verse_segment = is_verse_segment


def batch_segments(segments: list[Segment], max_tokens: int) -> list[list[Segment]]:
    """Group segments into batches by shared token budget (tiktoken cl100k_base).

    Avoid splitting contiguous multi-segment verse blocks (e.g., classical couplets) across
    batch boundaries: if a poem is about to begin late in a prose batch and fits cleanly in a
    fresh batch, start the batch before the poem; if already inside a poem, allow bounded
    overflow (+35% of ``max_tokens``) to finish the remaining verse run together.
    """
    if not segments:
        return []
    lengths = [count_tokens(s.source) for s in segments]
    is_verse = [_is_verse_segment(s.source) for s in segments]
    overflow_limit = max(max_tokens, int(max_tokens * 1.35))
    early_flush_threshold = int(max_tokens * 0.65)

    def _verse_run_end(start: int) -> int:
        end = start
        while end < len(segments) and is_verse[end]:
            end += 1
        return end

    batches: list[list[Segment]] = []
    cur: list[Segment] = []
    cur_tokens = 0
    in_verse_run = False
    i = 0
    while i < len(segments):
        s = segments[i]
        slen = lengths[i]
        if is_verse[i] and not in_verse_run:
            run_end = _verse_run_end(i)
            run_len = run_end - i
            if run_len >= 2:
                run_tokens = sum(lengths[i:run_end])
                # Start a fresh batch before a multi-segment poem if it won't fit here
                # but does fit cleanly in its own batch.
                if (
                    cur
                    and cur_tokens >= early_flush_threshold
                    and cur_tokens + run_tokens > max_tokens
                    and run_tokens <= max_tokens
                ):
                    batches.append(cur)
                    cur, cur_tokens = [], 0
                in_verse_run = True
        elif not is_verse[i]:
            in_verse_run = False

        if cur and cur_tokens + slen > max_tokens:
            # If we are inside a multi-segment verse run and finishing the run fits within
            # the bounded overflow slack, keep the poem unbroken in this batch.
            if in_verse_run:
                run_end = _verse_run_end(i)
                rem_tokens = sum(lengths[i:run_end])
                if cur_tokens + rem_tokens <= overflow_limit:
                    cur.append(s)
                    cur_tokens += slen
                    i += 1
                    continue
            batches.append(cur)
            cur, cur_tokens = [], 0
        cur.append(s)
        cur_tokens += slen
        i += 1
    if cur:
        batches.append(cur)
    return batches
