"""Deterministic DOM fragment and translation-anchor lookup."""

from __future__ import annotations

import posixpath
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup
from bs4.element import Tag


def fragment_anchor_map(template: str) -> dict[str, str | None]:
    """Map XHTML id/name values to Segment anchors.
    None means the ID exists after the resource's final translatable block. Keep this
    distinct from a fragment that does not exist at all.
    """
    soup = BeautifulSoup(template, "html.parser")
    mapping: dict[str, str | None] = {}
    for node in soup.find_all(True):
        identifiers = [node.get("id"), node.get("name")]
        if not any(isinstance(value, str) and value for value in identifiers):
            continue
        block = (
            node if node.has_attr("data-tn-id") else node.find_parent(attrs={"data-tn-id": True})
        )
        if not isinstance(block, Tag):
            block = node.find_next(attrs={"data-tn-id": True})
        raw_anchor = block.get("data-tn-id") if isinstance(block, Tag) else None
        anchor = raw_anchor if isinstance(raw_anchor, str) and raw_anchor else None
        for value in identifiers:
            if isinstance(value, str) and value:
                mapping.setdefault(value, anchor)
    return mapping


def fragment_nodes(soup: BeautifulSoup, fragment: str) -> list[Tag]:
    """Return unique DOM nodes exactly matching a fragment, retaining duplicate IDs for
    ambiguity checks.
    """
    nodes: list[Tag] = []
    seen: set[int] = set()
    for node in soup.find_all(True):
        if node.get("id") != fragment and node.get("name") != fragment:
            continue
        if id(node) not in seen:
            seen.add(id(node))
            nodes.append(node)
    return nodes


def scope_segment_anchors(scope: Tag) -> list[str]:
    """Return translation-block anchors within the annotation scope in DOM order."""
    candidates = [scope] if scope.has_attr("data-tn-id") else []
    candidates.extend(scope.find_all(True, attrs={"data-tn-id": True}))
    anchors: list[str] = []
    for candidate in candidates:
        raw_anchor = candidate.get("data-tn-id")
        if isinstance(raw_anchor, str) and raw_anchor and raw_anchor not in anchors:
            anchors.append(raw_anchor)
    return anchors


@dataclass(frozen=True)
class ResolvedEpubHref:
    """Structured TOC href.
    raw_href preserves the original value. resource_href is the ZIP member resolved relative
    to the TOC file. fragment is the percent-decoded anchor.
    """

    raw_href: str
    resource_href: str
    fragment: str
    external: bool = False

    @property
    def target_key(self) -> str:
        """Return a stable content-destination key, not a unique TOC node ID."""
        if not self.resource_href:
            return ""
        return f"{self.resource_href}#{self.fragment}" if self.fragment else self.resource_href


def resolve_epub_href(base_path: str, raw_href: str) -> ResolvedEpubHref:
    """Resolve an internal EPUB link against base_path without rewriting raw_href.
    Decode percent escapes with urllib.parse.unquote, preserving literal plus signs in
    filenames. Mark URLs with schemes or hosts as external and exclude them from chapter
    splitting and backfill lookup.
    """
    raw = raw_href or ""
    parsed = urlsplit(raw)
    external = bool(parsed.scheme or parsed.netloc)
    fragment = unquote(parsed.fragment)
    if external:
        return ResolvedEpubHref(raw, "", fragment, True)

    decoded_path = unquote(parsed.path)
    if decoded_path.startswith("/"):
        resource = posixpath.normpath(decoded_path.lstrip("/"))
    elif decoded_path:
        base_dir = posixpath.dirname(base_path)
        resource = posixpath.normpath(posixpath.join(base_dir, decoded_path))
    else:
        # A fragment-only href targets the TOC file itself.
        resource = posixpath.normpath(base_path)
    if resource == ".":
        resource = ""
    return ResolvedEpubHref(raw, resource, fragment, False)
