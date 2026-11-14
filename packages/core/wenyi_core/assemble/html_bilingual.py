"""Preserve bilingual source markup and allocate collision-free source anchors."""

from __future__ import annotations

import re
from copy import deepcopy

from bs4 import BeautifulSoup
from bs4.element import AttributeValueList, Tag

from wenyi_core.assemble.writer_common import _bilingual_source
from wenyi_core.markup.anchors import resolve_epub_href
from wenyi_core.markup.contracts import (
    ANNOTATION_ID_ATTR,
    HTML_ATTRIBUTE_ID_ATTR,
    INLINE_ID_ATTR,
    LINE_WRAPPER_ATTR,
)
from wenyi_core.models.document import KIND_HEADING

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
_TEMPORARY_ATTRS = (
    "data-tn-id",
    INLINE_ID_ATTR,
    ANNOTATION_ID_ATTR,
    LINE_WRAPPER_ATTR,
    HTML_ATTRIBUTE_ID_ATTR,
)
_IDREF_ATTRS = {
    "aria-activedescendant",
    "aria-controls",
    "aria-describedby",
    "aria-details",
    "aria-errormessage",
    "aria-flowto",
    "aria-labelledby",
    "aria-owns",
    "for",
    "form",
    "headers",
    "itemref",
    "list",
    "xref",
}
_LOCAL_URL = re.compile(r"""url\(\s*(['"]?)#([^)'"\s]+)\1\s*\)""")


def _bilingual_source_markup(
    element: Tag,
    *,
    resource_href: str,
    source_link_targets: dict[tuple[str, str], str],
    source_fragment_ids: dict[str, str],
) -> Tag:
    """Copy the complete original subtree, changing only temporary attributes and identities.

    Source content already has exact positions: never rebuild it from Segment.source,
    apply translation placements, flatten tags, or filter content by language/type.
    Cloned IDs and their references belong to the source namespace, not the translation.
    """
    root = deepcopy(element)

    def local_id(identifier: str) -> str:
        return source_fragment_ids.get(identifier) or source_link_targets.get(
            (resource_href, identifier), identifier
        )

    for tag in (root, *root.find_all(True)):
        for attr in _TEMPORARY_ATTRS:
            tag.attrs.pop(attr, None)
        for attr in ("id", "name"):
            value = tag.get(attr)
            if isinstance(value, str) and value in source_fragment_ids:
                tag[attr] = source_fragment_ids[value]
        for attr in ("href", "xlink:href"):
            raw_href = tag.get(attr)
            if not isinstance(raw_href, str):
                continue
            resolved = resolve_epub_href(resource_href, raw_href)
            destination = source_link_targets.get((resolved.resource_href, resolved.fragment))
            if resolved.resource_href == resource_href:
                destination = source_fragment_ids.get(resolved.fragment, destination)
            if not resolved.external and resolved.fragment and destination:
                path_and_query, separator, _fragment = raw_href.partition("#")
                if separator:
                    tag[attr] = f"{path_and_query}#{destination}"
        for attr, value in list(tag.attrs.items()):
            if attr in _IDREF_ATTRS:
                identifiers = value if isinstance(value, list) else str(value).split()
                rewritten = [local_id(identifier) for identifier in identifiers]
                tag[attr] = (
                    AttributeValueList(rewritten)
                    if isinstance(value, list)
                    else " ".join(rewritten)
                )
            elif isinstance(value, str):
                tag[attr] = _LOCAL_URL.sub(
                    lambda match: (
                        f"url({match[1]}#{local_id(match[2])}{match[1]})"
                        if local_id(match[2]) != match[2]
                        else match[0]
                    ),
                    value,
                )
    return root


def _append_source(element: Tag, source: str, snapshot: Tag | None) -> None:
    """Write a complete source snapshot, or plain text when no source DOM is available."""
    if snapshot is None:
        element.append(source)
        return
    # A list item can use its <a> as the translation region. Keep that original shell;
    # block shells are supplied by the source wrapper so paragraphs never nest.
    if snapshot.name == "a":
        element.append(snapshot)
        return
    for child in list(snapshot.contents):
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


def _build_source_fragment_ids(
    source_ids: dict[str, str],
    tn_id_index: dict[str, Tag],
    occupied: set[str],
) -> dict[str, dict[str, str]]:
    """Allocate collision-free identities for every copied source descendant, in DOM order."""
    occupied = set(occupied) | set(source_ids.values())
    assigned: dict[str, dict[str, str]] = {}
    for anchor, source_id in source_ids.items():
        element = tn_id_index[anchor]
        identifiers = assigned[anchor] = {}
        for ordinal, node in enumerate((element, *element.find_all(True))):
            values = {
                value
                for attr in ("id", "name")
                if isinstance(value := node.get(attr), str) and value
            }
            if not values:
                continue
            candidate = source_id
            if node is not element or element.name == "a":
                base = f"{source_id}-fragment-{ordinal}"
                candidate = base
                suffix = 2
                while candidate in occupied:
                    candidate = f"{base}-{suffix}"
                    suffix += 1
                occupied.add(candidate)
            for value in values:
                identifiers.setdefault(value, candidate)
    return assigned
