"""Single-file HTML reader for converted books.
Split HTML by headings into Chapters, then extract block-level Segments with data-tn-id
anchors for translation backfill. Follow EPUB conventions for p, h1-h6, li and blockquote
blocks, avoiding duplicate nested extraction. Headings use kind=heading; other blocks use
kind=text. Anchors use tn{chapter}_{segment}, and each chapter retains its annotated HTML
template.
By default every heading level starts a chapter; chapter_tags can customize the boundary
policy.
"""

from __future__ import annotations

import os

from bs4 import BeautifulSoup, Tag
from bs4.element import Comment

from .epub_reader import annotate_epub_resource
from .models import Chapter, Document, Segment

# Block and heading tags shared with the EPUB reader.
_BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

# All heading levels start chapters by default, following the text reader's heading policy.
_DEFAULT_CHAPTER_TAGS: frozenset[str] = frozenset(_HEADING_TAGS)


# Internal helpers.


def _extract_chapter(
    html: str,
    chapter_index: int,
    *,
    chapter_title: str = "",
) -> tuple[str, list[Segment], str]:
    """Parse a chapter fragment into its title, segments and annotated HTML template.
    Follow EPUB block extraction: avoid duplicate nested blocks, assign data-tn-id to valid
    targets, and prefer explicit chapter_title over the first heading segment.
    """
    detected_title, segments, template = annotate_epub_resource(
        html,
        chapter_index,
        f"html-chapter-{chapter_index}.xhtml",
    )
    # ``resource_href`` only has meaning for EPUB's physical-resource rebuild.
    for segment in segments:
        segment.resource_href = None

    # Title priority: explicit value, first heading segment, then empty.
    title = chapter_title.strip()
    if not title:
        title = detected_title

    return title, segments, template


# Public API.


def read_html(
    path: str,
    source_lang: str,
    target_lang: str,
    *,
    chapter_tags: frozenset[str] | None = _DEFAULT_CHAPTER_TAGS,
    encoding: str = "utf-8",
) -> Document:
    """Parse one HTML file into a Document.
    Headings (h1-h6 by default) begin chapters; consecutive headings stay together. Body
    blocks before the first heading form front matter. With chapter_tags=None, use one
    chapter for the entire document. Extract block segments with the EPUB reader's
    nested-block rules.
    path is the HTML file, source_lang and target_lang are language codes, chapter_tags
    selects boundary tags or disables splitting, and encoding defaults to UTF-8.
    """
    with open(path, "r", encoding=encoding, errors="replace") as f:
        soup = BeautifulSoup(f, "html.parser")

    # Derive the book title from the filename, matching the text reader.
    book_title = os.path.splitext(os.path.basename(path))[0]

    # Store only head contents; the writer creates the enclosing head element during export.
    head_html = soup.head.decode_contents() if soup.head else ""

    body = soup.body if soup.body else soup

    # Collect direct body children, including tags and text, in document order.
    children: list = list(body.children)

    # For consecutive headings, only the first starts a new chapter.
    boundaries: list[int] = []
    if chapter_tags:
        for i, child in enumerate(children):
            if isinstance(child, Tag) and child.name in chapter_tags:
                # Check whether the previous non-whitespace element is also a heading.
                prev_is_heading = False
                for j in range(i - 1, -1, -1):
                    prev = children[j]
                    if isinstance(prev, Tag):
                        prev_is_heading = prev.name in chapter_tags
                        break
                    if hasattr(prev, "strip") and prev.strip():
                        break  # Nonempty text breaks heading continuity.
                if not prev_is_heading:
                    boundaries.append(i)

    # Build start/end intervals.
    if not boundaries:
        intervals: list[tuple[int, int]] = [(0, len(children))]
    else:
        intervals = []
        # Front matter before the first heading.
        if boundaries[0] > 0:
            intervals.append((0, boundaries[0]))
        # Start each chapter at its heading boundary.
        for bi, start in enumerate(boundaries):
            end = boundaries[bi + 1] if bi + 1 < len(boundaries) else len(children)
            intervals.append((start, end))

    chapters: list[Chapter] = []
    ci = 0
    for start, end in intervals:
        ch_children = children[start:end]
        fragment_html = "".join(
            f"<!--{c}-->" if isinstance(c, Comment) else str(c) for c in ch_children
        )

        # Join consecutive nonempty heading texts with /, normalizing embedded newlines.
        chapter_title = ""
        first = ch_children[0] if ch_children else None
        if isinstance(first, Tag) and first.name in _HEADING_TAGS:
            titles = []
            for c in ch_children:
                if isinstance(c, Tag) and c.name in _HEADING_TAGS:
                    t = " ".join(c.get_text().split())  # Normalize newlines.
                    titles.append(t)
                elif isinstance(c, Tag):
                    break  # Stop at a non-heading tag.
                elif hasattr(c, "strip") and c.strip():
                    break  # Stop at nonempty text.
                # Skip whitespace-only text and continue.
            chapter_title = " / ".join(titles)

        title, segments, template = _extract_chapter(
            fragment_html,
            ci,
            chapter_title=chapter_title,
        )

        template_soup = BeautifulSoup(template, "html.parser")
        has_visible_media = template_soup.find(
            ["img", "picture", "svg", "object", "video", "audio", "canvas", "iframe"]
        )
        if not any(s.source.strip() for s in segments) and has_visible_media is None:
            continue

        chapters.append(
            Chapter(
                index=ci,
                title=title,
                segments=segments,
                href=None,
                template=template,
            )
        )
        ci += 1

    return Document(
        title=book_title,
        source_lang=source_lang,
        target_lang=target_lang,
        fmt="html",
        source_path=os.path.abspath(path),
        chapters=chapters,
        meta={
            "chapter_tags": list(chapter_tags) if chapter_tags else None,
            "head_html": head_html,
        },
    )
