"""Preserve bilingual source markup and allocate collision-free source anchors."""

from __future__ import annotations

from bs4 import BeautifulSoup
from bs4.element import Comment, Tag

from wenyi_core.assemble.writer_common import _bilingual_source
from wenyi_core.ingest.models import KIND_HEADING
from wenyi_core.markup.anchors import resolve_epub_href
from wenyi_core.markup.contracts import (
    ANNOTATION_ID_ATTR,
    INLINE_ID_ATTR,
    LINE_WRAPPER_ATTR,
)

_BILINGUAL_STYLE_ID = "tn-bilingual-style"


_BILINGUAL_CSS = """\
.tn-source {
  font-size: 0.88em;
  line-height: 1.55;
  color: #6b6b6b;
  background-color: #f4f3f0;
  padding: 0.5em 0.8em;
  border-radius: 5px;
  margin: 0.2em 0 1em;
}
@media (prefers-color-scheme: dark) {
  .tn-source {
    color: #a8a8a8;
    background-color: #2a2a2a;
    box-shadow: inset 0 0 0 1px rgba(255,255,255,0.14);
  }
}
"""


_SOURCE_ANCHOR_PREFIX = "tn-source-"


def _bilingual_source_markup(
    element: Tag,
    source_lang: str,
    *,
    resource_href: str,
    source_link_targets: dict[tuple[str, str], str],
) -> str:
    """Preserve annotation links and Japanese ruby in bilingual source text.
    Source links already have accurate positions in the original EPUB, so target placements
    are unnecessary. Keep annotation roots and descendants while flattening other inline
    tags to clean text. Remove cloned id/name attributes to avoid duplicate anchors on the
    target side.
    """
    normalized_lang = source_lang.strip().replace("_", "-").lower()
    keep_ruby = normalized_lang == "ja" or normalized_lang.startswith("ja-")
    has_annotation = (
        element.has_attr(ANNOTATION_ID_ATTR)
        or element.find(True, attrs={ANNOTATION_ID_ATTR: True}) is not None
    )
    if not has_annotation and (not keep_ruby or element.find("ruby") is None):
        return ""

    fragment = BeautifulSoup(str(element), "html.parser")
    root = fragment.find(element.name)
    if not isinstance(root, Tag):
        return ""

    root_is_annotation = root.has_attr(ANNOTATION_ID_ATTR)
    retained: set[int] = set()
    for annotation in root.find_all(True, attrs={ANNOTATION_ID_ATTR: True}):
        retained.add(id(annotation))
        retained.update(id(descendant) for descendant in annotation.find_all(True))
    if root_is_annotation:
        retained.add(id(root))
        retained.update(id(descendant) for descendant in root.find_all(True))
    if keep_ruby:
        for ruby in root.find_all("ruby"):
            retained.add(id(ruby))
            retained.update(id(descendant) for descendant in ruby.find_all(True))

    for comment in list(root.find_all(string=lambda node: isinstance(node, Comment))):
        comment.extract()
    for tag in list(
        root.find_all(
            [
                "audio",
                "canvas",
                "embed",
                "hr",
                "iframe",
                "img",
                "math",
                "object",
                "script",
                "source",
                "style",
                "svg",
                "video",
            ]
        )
    ):
        tag.decompose()

    if not keep_ruby:
        for tag in list(root.find_all(["rt", "rp"])):
            tag.decompose()

    for tag in list(root.find_all(True)):
        if id(tag) not in retained:
            tag.unwrap()
            continue
        for attr in (
            "id",
            "name",
            "data-tn-id",
            INLINE_ID_ATTR,
            ANNOTATION_ID_ATTR,
            LINE_WRAPPER_ATTR,
        ):
            tag.attrs.pop(attr, None)
    for attr in (
        "id",
        "name",
        "data-tn-id",
        INLINE_ID_ATTR,
        ANNOTATION_ID_ATTR,
        LINE_WRAPPER_ATTR,
    ):
        root.attrs.pop(attr, None)

    # Translations retain original fragments. Rewrite source mirrors to synthetic anchors only when
    # the destination also has a source block. Preserve paths and queries so cross-XHTML links
    # keep their original relative resolution; preserve unmapped links to avoid dangling anchors.
    links = [root] if root.name == "a" else []
    links.extend(root.find_all("a", href=True))
    for link in links:
        raw_href = link.get("href")
        if not isinstance(raw_href, str):
            continue
        resolved = resolve_epub_href(resource_href, raw_href)
        source_anchor = source_link_targets.get((resolved.resource_href, resolved.fragment))
        if resolved.external or not resolved.fragment or not source_anchor:
            continue
        path_and_query, separator, _fragment = raw_href.partition("#")
        if separator:
            link["href"] = f"{path_and_query}#{source_anchor}"
    return str(root) if root_is_annotation else root.decode_contents()


def _append_source(soup: BeautifulSoup, element: Tag, source: str, markup: str) -> None:
    """Write plain text or sanitized annotation/ruby markup into a bilingual source block."""
    if not markup:
        element.append(source)
        return
    fragment = BeautifulSoup(markup, "html.parser")
    for child in list(fragment.contents):
        element.append(child.extract())


def _index_soup_ids(soup: BeautifulSoup) -> tuple[set[str], dict[str, Tag]]:
    """Build occupied id/name sets and the data-tn-id index in one find_all traversal.
    Repeated soup.find calls make backfill scale with paragraph count times DOM size. Index
    the page once so subsequent anchor lookup is O(1).
    """
    occupied: set[str] = set()
    tn_id_index: dict[str, Tag] = {}
    for node in soup.find_all(True):
        for attr in ("id", "name"):
            value = node.get(attr)
            if isinstance(value, str) and value:
                occupied.add(value)
        tn_id = node.get("data-tn-id")
        if isinstance(tn_id, str) and tn_id:
            tn_id_index[tn_id] = node
    return occupied, tn_id_index


def _build_source_anchor_ids(
    by_anchor: dict[str, str],
    src_by_anchor: dict[str, str],
    kind_by_anchor: dict[str, str],
    tn_id_index: dict[str, Tag],
    occupied: set[str],
) -> dict[str, str]:
    """Assign stable synthetic IDs to actual source blocks without colliding with original IDs."""
    occupied = set(
        occupied
    )  # Copy the caller's set because this function adds every newly allocated ID.
    assigned: dict[str, str] = {}
    for anchor, target in by_anchor.items():
        if kind_by_anchor.get(anchor) == KIND_HEADING:
            continue
        source = _bilingual_source(src_by_anchor.get(anchor, ""), target)
        if not source or anchor not in tn_id_index:
            continue
        base = f"{_SOURCE_ANCHOR_PREFIX}{anchor}"
        candidate = base
        suffix = 2
        while candidate in occupied:
            candidate = f"{base}-{suffix}"
            suffix += 1
        assigned[anchor] = candidate
        occupied.add(candidate)
    return assigned
