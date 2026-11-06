"""Read static HTML as prose regions with semantic inline ranges and local attribute slots.

Preserve the original layout template; translate complete inline runs rather than
individual DOM text nodes. Converted PDFs can retain the established EPUB block policy.
"""

from __future__ import annotations

import os

from bs4 import BeautifulSoup, Tag
from bs4.element import Comment, Doctype

from wenyi_core.markup.html import html_document_soup, html_is_protected, html_visible_text
from wenyi_core.markup.segments import annotate_epub_resource

from .models import Chapter, Document, Segment

_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}

# All heading levels start chapters by default, following the text reader's heading policy.
_DEFAULT_CHAPTER_TAGS: frozenset[str] = frozenset(_HEADING_TAGS)


# Internal helpers.


def _extract_chapter(
    html: str,
    chapter_index: int,
    *,
    chapter_title: str = "",
    preserve_inline_markup: bool = True,
) -> tuple[str, list[Segment], str]:
    """Extract a fragment using the selected markup policy and prefer its explicit title."""
    detected_title, segments, template = annotate_epub_resource(
        html,
        chapter_index,
        f"html-chapter-{chapter_index}.xhtml",
        html_mode=preserve_inline_markup,
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
    preserve_inline_markup: bool = True,
) -> Document:
    """Parse one HTML file into a Document.
    Headings (h1-h6 by default) begin chapters; consecutive headings stay together. Body
    blocks before the first heading form front matter. With chapter_tags=None, use one
    chapter for the entire document. Protect inert, hidden, editable and opted-out content.
    preserve_inline_markup=False retains the EPUB extraction policy for converted PDFs.
    path is the HTML file, source_lang and target_lang are language codes, chapter_tags
    selects boundary tags or disables splitting, and encoding defaults to UTF-8.
    """
    with open(path, "r", encoding=encoding, errors="replace") as f:
        document, _template = annotate_html_document(
            f.read(),
            path,
            source_lang,
            target_lang,
            chapter_tags=chapter_tags,
            encoding=encoding,
            preserve_inline_markup=preserve_inline_markup,
        )
    return document


def annotate_html_document(
    html: str,
    path: str,
    source_lang: str,
    target_lang: str,
    *,
    chapter_tags: frozenset[str] | None = _DEFAULT_CHAPTER_TAGS,
    encoding: str = "utf-8",
    preserve_inline_markup: bool = True,
) -> tuple[Document, str]:
    """Extract logical chapters and annotate the complete physical source for backfill."""
    # Match text-file reading when exports decode a frozen byte snapshot directly.
    soup = html_document_soup(html.replace("\r\n", "\n").replace("\r", "\n"))

    # Derive the book title from the filename, matching the text reader.
    book_title = os.path.splitext(os.path.basename(path))[0]

    # Chapter-based formats also need head contents; HTML exports reparse the full source.
    head_html = soup.head.decode_contents() if soup.head else ""

    body = soup.body or soup.html or soup
    protected_document = preserve_inline_markup and any(
        isinstance(element, Tag) and html_is_protected(element)
        for element in (soup.html, soup.body)
    )

    # Collect direct body children, including tags and text, in document order.
    children: list = [
        child
        for child in body.children
        if not isinstance(child, Doctype) and not (isinstance(child, Tag) and child.name == "head")
    ]

    # For consecutive headings, only the first starts a new chapter.
    boundaries: list[int] = []
    if chapter_tags and not protected_document:
        for i, child in enumerate(children):
            if (
                isinstance(child, Tag)
                and child.name in chapter_tags
                and (not preserve_inline_markup or not html_is_protected(child))
            ):
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
    pending_template = ""
    annotated_parts: list[str] = []
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
                    text = html_visible_text(c) if preserve_inline_markup else c.get_text()
                    t = " ".join(text.split())
                    if t:
                        titles.append(t)
                elif isinstance(c, Tag):
                    break  # Stop at a non-heading tag.
                elif hasattr(c, "strip") and c.strip():
                    break  # Stop at nonempty text.
                # Skip whitespace-only text and continue.
            chapter_title = " / ".join(titles)

        if protected_document:
            title, segments, template = "", [], fragment_html
        else:
            title, segments, template = _extract_chapter(
                fragment_html,
                ci,
                chapter_title=chapter_title,
                preserve_inline_markup=preserve_inline_markup,
            )

        annotated_parts.append(template)
        template_soup = BeautifulSoup(template, "html.parser")
        has_visible_media = template_soup.find(
            ["img", "picture", "svg", "object", "video", "audio", "canvas", "iframe"]
        )
        if not any(s.source.strip() for s in segments) and has_visible_media is None:
            if preserve_inline_markup:
                pending_template += template
            continue

        template = pending_template + template
        pending_template = ""
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

    if pending_template.strip():
        if chapters:
            chapters[-1].template = (chapters[-1].template or "") + pending_template
        else:
            chapters.append(Chapter(index=0, template=pending_template))

    # Logical chapter boundaries never become physical document boundaries. Restore every
    # annotated fragment, including non-translatable layout, into the original shell.
    body.clear()
    annotated_body = BeautifulSoup("".join(annotated_parts), "html.parser")
    for node in list(annotated_body.contents):
        body.append(node.extract())

    document = Document(
        title=book_title,
        source_lang=source_lang,
        target_lang=target_lang,
        fmt="html",
        source_path=os.path.abspath(path),
        chapters=chapters,
        meta={
            "chapter_tags": list(chapter_tags) if chapter_tags else None,
            "head_html": head_html,
            "html_attrs": dict(soup.html.attrs) if soup.html else {},
            "body_attrs": dict(soup.body.attrs) if soup.body else {},
            "html_parse": {
                "version": 1,
                "encoding": encoding,
                "preserve_inline_markup": preserve_inline_markup,
            },
        },
    )
    return document, str(soup)
