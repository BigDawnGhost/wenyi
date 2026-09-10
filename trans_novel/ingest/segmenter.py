"""Dispatch document readers and split translation batches.
load_document selects a reader by extension and optionally splits long segments.
batch_segments groups chapter segments by character budget as a rough token bound and
requires equally sized model output for alignment. split_long_segments splits oversized
segments at sentences, marks continuations and lets the writer merge them into the original
paragraph/EPUB element.
"""

from __future__ import annotations

import os
import re
from copy import deepcopy

from .epub_reader import read_epub
from .fb2_reader import read_fb2
from .html_reader import read_html
from .models import KIND_TEXT, Chapter, Document, Segment
from .pdf_reader import read_pdf
from .text_reader import read_text

# Common sentence-ending punctuation used for splitting long paragraphs.
_SENT_SPLIT = re.compile(r"(?<=[。．.!！？!?…\n])")


def _split_oversized_sentence(text: str, max_chars: int) -> list[str]:
    """Split an oversized sentence at whitespace where possible; hard-split only as a fallback."""
    chunks: list[str] = []
    rest = text
    while len(rest) > max_chars:
        cut = rest.rfind(" ", 0, max_chars + 1)
        if cut <= 0:
            cut = rest.rfind("\t", 0, max_chars + 1)
        if cut <= 0:
            cut = rest.rfind("\n", 0, max_chars + 1)
        if cut <= 0:
            cut = max_chars
        chunks.append(rest[:cut])
        rest = rest[cut:]
    if rest:
        chunks.append(rest)
    return chunks


def _split_text(text: str, max_chars: int) -> list[str]:
    """Greedily group sentences by length, falling back to whitespace splits for oversized
    sentences.
    """
    chunks: list[str] = []
    cur = ""
    for p in _SENT_SPLIT.split(text):
        if not p:
            continue
        if len(p) > max_chars:  # The sentence itself exceeds the limit; use the fallback splitter.
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.extend(_split_oversized_sentence(p, max_chars))
            continue
        if cur and len(cur) + len(p) > max_chars:
            chunks.append(cur)
            cur = ""
        cur += p
    if cur:
        chunks.append(cur)
    return chunks or [text]


def split_long_segments(chapters: list[Chapter], max_chars: int) -> None:
    """Split oversized segments in place; mark continuations cont=True without independent
    anchors.
    """
    if not max_chars or max_chars <= 0:
        return
    for ch in chapters:
        new_segs: list[Segment] = []
        idx = 0
        for s in ch.segments:
            if len(s.source) <= max_chars:
                s.index = idx
                new_segs.append(s)
                idx += 1
                continue
            for k, piece in enumerate(_split_text(s.source, max_chars)):
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
) -> Document:
    """Dispatch by file extension and optionally split oversized translation segments."""
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
            )
    elif ext == ".docx":
        from .docx_reader import read_docx

        doc = read_docx(path, source_lang, target_lang)
    else:
        raise ValueError(
            f"Unsupported format: {ext} (supported: .epub / .txt / .md / .fb2 / .html / .xhtml / .pdf / .docx)"
        )

    # BabelDOC IDs are tied to layout; never split those paragraphs by character count.
    if split_segments and split_segments > 0 and not (doc.meta or {}).get("babeldoc"):
        split_long_segments(doc.chapters, split_segments)
    return doc


def batch_segments(segments: list[Segment], max_chars: int) -> list[list[Segment]]:
    """Group segments into batches by character budget."""
    batches: list[list[Segment]] = []
    cur: list[Segment] = []
    cur_len = 0
    for s in segments:
        slen = len(s.source)
        if cur and cur_len + slen > max_chars:
            batches.append(cur)
            cur, cur_len = [], 0
        cur.append(s)
        cur_len += slen
    if cur:
        batches.append(cur)
    return batches
