"""FB2 (FictionBook) reader.
FB2 is XML, commonly using namespace http://www.gribuser.ru/xml/fictionbook/2.0, with 2.1 or
no namespace also encountered. FictionBook contains description/title-info, body/section
chapters with title/p headings, subtitles, paragraphs, epigraphs, citations and
poem/stanza/v lines; skip body name="notes".
Recursively flatten nested sections without losing child text. Include p, subtitle,
epigraph, cite, poem and text-author content. Preserve image references for export. FB2 has
no backfill anchors, so assembly generates a new EPUB instead of modifying the original
file.
"""

from __future__ import annotations

import base64
import os
import re
import xml.etree.ElementTree as ET

from .models import KIND_HEADING, KIND_TEXT, Chapter, Document, Segment


def _local(el: ET.Element) -> str:
    """Return an FB2 element's local tag name without its XML namespace."""
    return el.tag.rsplit("}", 1)[-1]


def _strip_markup(el: ET.Element) -> str:
    """Extract plain text from an element while retaining basic whitespace."""
    parts: list[str] = []
    for text in el.itertext():
        if text:
            parts.append(text)
    return "".join(parts)


# Recurse through containers such as poem/stanza/title and cite/p to reach their text.
_CONTAINER_BLOCKS = {"epigraph", "cite", "poem", "stanza", "title", "annotation"}


def _image_id(el: ET.Element) -> str:
    """Return the referenced FB2 binary id from an xlink/plain href."""
    for name, value in el.attrib.items():
        if name.rsplit("}", 1)[-1] == "href":
            return value.removeprefix("#").strip()
    return ""


def _direct_segments(
    section: ET.Element,
    chapter_index: int,
) -> tuple[str, list[Segment], list[dict[str, int | str]]]:
    """Extract only a section's direct content, leaving child sections for recursion.
    Include paragraphs, subtitles, epigraphs, citations, poetry and attributions. Record
    image positions relative to paragraphs for restoration from original FB2 binaries.
    Return title, segments and images, with the title first as a heading.
    """
    segments: list[Segment] = []
    images: list[dict[str, int | str]] = []
    idx = 0
    title_text = ""

    def add(text: str, kind: str) -> None:
        """Normalize and append nonempty paragraphs with stable chapter-local indices and
        anchors.
        """
        nonlocal idx
        text = text.strip()
        if text:
            segments.append(
                Segment(index=idx, source=text, kind=kind, anchor=f"tn{chapter_index}_{idx}")
            )
            idx += 1

    def emit_block(el: ET.Element) -> None:
        """Recursively expand body containers and collect text blocks and relative image
        positions.
        """
        tag = _local(el)
        if tag == "image":
            image_id = _image_id(el)
            if image_id:
                images.append({"id": image_id, "position": len(segments)})
        elif tag == "subtitle":
            add(_strip_markup(el), KIND_HEADING)  # Subheadings within a section.
        elif tag in ("p", "v", "text-author"):  # Paragraphs, verse lines and attributions.
            for image in el.iter():
                if image is not el and _local(image) == "image":
                    emit_block(image)
            add(_strip_markup(el), KIND_TEXT)
        elif tag in _CONTAINER_BLOCKS:  # Recurse into containers.
            for sub in el:
                emit_block(sub)
        # Skip empty-line and unsupported elements.

    for child in section:
        tag = _local(child)
        if tag == "section":
            continue  # _walk_sections handles child sections recursively.
        if tag == "title":
            title_text = _strip_markup(child).strip()
            add(title_text, KIND_HEADING)
        else:
            emit_block(child)
    return title_text, segments, images


def _walk_sections(section: ET.Element, chapters: list[Chapter]) -> None:
    """Recursively flatten sections into chapters while preserving parent content and part
    titles.
    FB2 commonly nests chapters within parts. Extracting only direct sections would lose
    text, so retain each container's own content before descending to its children.
    """
    ci = len(chapters)
    title_text, segs, images = _direct_segments(section, ci)
    child_sections = [c for c in section if _local(c) == "section"]

    if child_sections:
        # Preserve a container section as a chapter if it has its own body paragraphs or even only a part title.
        has_body = any(s.kind == KIND_TEXT for s in segs)
        if has_body or title_text:
            chapters.append(_make_chapter(ci, title_text, segs, images))
        for cs in child_sections:
            _walk_sections(cs, chapters)
    elif segs:
        chapters.append(_make_chapter(ci, title_text, segs, images))


def _make_chapter(
    ci: int,
    title_text: str,
    segments: list[Segment],
    images: list[dict[str, int | str]],
) -> Chapter:
    """Construct a chapter from extracted content and supply a display title when absent."""
    if not title_text and segments:
        title_text = segments[0].source[:80]
    elif not title_text:
        title_text = f"Chapter {ci + 1}"
    meta = {"fb2_images": images} if images else {}
    return Chapter(index=ci, title=title_text, segments=segments, meta=meta)


def _body_title_chapter(body: ET.Element) -> Chapter | None:
    """Parse a body/title element as a separate title-page chapter."""
    title_el = next((child for child in body if _local(child) == "title"), None)
    if title_el is None:
        return None

    lines = [
        _strip_markup(child).strip()
        for child in title_el
        if _local(child) == "p" and _strip_markup(child).strip()
    ]
    if not lines:
        return None

    segments = [
        Segment(
            index=idx,
            source=line,
            kind=KIND_HEADING,
            anchor=f"tn0_{idx}",
        )
        for idx, line in enumerate(lines)
    ]
    return Chapter(index=0, title=lines[-1], segments=segments)


def read_fb2(path: str, source_lang: str, target_lang: str) -> Document:
    """Read an FB2 file into a Document."""
    with open(path, "rb") as f:
        raw = f.read()

    # Remove encoding from the XML declaration; FB2 often declares windows-1251.
    enc = "utf-8"
    m = re.search(rb"<\?xml.*?encoding\s*=\s*['\"]([^'\"]+)['\"]", raw)
    if m:
        enc = m.group(1).decode("ascii", errors="replace")
    try:
        text = raw.decode(enc)
    except (UnicodeDecodeError, LookupError):
        text = raw.decode("utf-8", errors="replace")

    root = ET.fromstring(text)

    resources: list[dict[str, str]] = []
    for binary in root.iter():
        if _local(binary) != "binary":
            continue
        resource_id = binary.attrib.get("id", "").strip()
        if resource_id:
            resources.append(
                {
                    "id": resource_id,
                    "content_type": binary.attrib.get("content-type", "application/octet-stream"),
                }
            )

    cover_image = ""
    for coverpage in root.iter():
        if _local(coverpage) != "coverpage":
            continue
        image = next(
            (element for element in coverpage.iter() if _local(element) == "image"),
            None,
        )
        if image is not None:
            cover_image = _image_id(image)
        break

    # Book title.
    title = os.path.splitext(os.path.basename(path))[0]
    for desc in root.iter():
        if _local(desc) != "title-info":
            continue
        for child in desc:
            if _local(child) == "book-title":
                if child.text:
                    title = child.text.strip()
                break

    # Chapters.
    chapters: list[Chapter] = []
    # Use only the first main body; skip auxiliary bodies such as body[name="notes"].
    for body in root:
        if _local(body) != "body":
            continue
        body_name = body.attrib.get("name", "")
        if body_name:  # Auxiliary bodies such as notes and comments.
            continue
        title_chapter = _body_title_chapter(body)
        if title_chapter is not None:
            chapters.append(title_chapter)
        for section in body:
            if _local(section) == "section":
                _walk_sections(section, chapters)
        break  # Process only the first body.

    if not chapters:
        # Fallback: treat the entire document as one chapter.
        segments: list[Segment] = []
        idx = 0
        for p in root.iter():
            if _local(p) != "p":
                continue
            text = _strip_markup(p).strip()
            if text:
                segments.append(
                    Segment(
                        index=idx,
                        source=text,
                        kind=KIND_TEXT,
                        anchor=f"tn0_{idx}",
                    )
                )
                idx += 1
        if segments:
            chapters.append(Chapter(index=0, title=title, segments=segments))

    meta: dict[str, object] = {}
    if resources:
        meta["fb2_resources"] = resources
    if cover_image:
        meta["fb2_cover_image"] = cover_image

    return Document(
        title=title,
        source_lang=source_lang,
        target_lang=target_lang,
        fmt="fb2",
        source_path=os.path.abspath(path),
        chapters=chapters,
        meta=meta,
    )


def read_fb2_binaries(path: str) -> dict[str, tuple[str, bytes]]:
    """Read embedded FB2 binaries for export without persisting them in run state."""
    with open(path, "rb") as file:
        root = ET.fromstring(file.read())

    resources: dict[str, tuple[str, bytes]] = {}
    for binary in root.iter():
        if _local(binary) != "binary":
            continue
        resource_id = binary.attrib.get("id", "").strip()
        encoded = "".join((binary.text or "").split())
        if not resource_id or not encoded:
            continue
        try:
            payload = base64.b64decode(encoded, validate=False)
        except (ValueError, TypeError):
            continue
        resources[resource_id] = (
            binary.attrib.get("content-type", "application/octet-stream"),
            payload,
        )
    return resources
