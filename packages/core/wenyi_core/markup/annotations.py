"""Recognize point and range notes, backlinks and note-body scopes."""

from __future__ import annotations

import re
from typing import TypeGuard
from urllib.parse import urlsplit

from bs4.element import Comment, NavigableString, ProcessingInstruction, Tag

from wenyi_core.markup.anchors import resolve_epub_href
from wenyi_core.markup.contracts import ANNOTATION_ID_ATTR

_ANNOTATION_MARKER_ONLY = re.compile(r"^[\d\s*＊※†‡\[\]()〔〕（）{}↩↵←↑↓⤶.·:：\-]+$")


_ANNOTATION_HINT = re.compile(
    r"(?:^|[^a-z0-9])(?:note|noteref|footnote|endnote|fn|jpref|jpnote)"
    r"(?:[-_]?\d+)?(?:$|[^a-z0-9])",
    re.IGNORECASE,
)


_NOTE_TARGET_HINT = re.compile(
    r"(?:^|[^a-z0-9])(?:note|footnote|endnote|fn)(?:[-_]?\d+)?(?:$|[^a-z0-9])",
    re.IGNORECASE,
)


_SHORT_NOTE_IDENTITY = re.compile(r"^n[-_]?\d+$", re.IGNORECASE)


_NOTEREF_SEMANTICS = {"noteref", "doc-noteref"}


_BACKLINK_SEMANTICS = {"backlink", "doc-backlink"}


_NOTE_BODY_SEMANTICS = {"footnote", "endnote", "rearnote", "doc-footnote", "doc-endnote"}


def _is_internal_link(link: Tag) -> bool:
    """Determine whether a link targets an EPUB resource rather than web, email or script
    content.
    """
    raw_href = link.get("href")
    if not isinstance(raw_href, str) or not raw_href.strip():
        return False
    parsed = urlsplit(raw_href.strip())
    return not parsed.scheme and not parsed.netloc


def _semantic_tokens(node: Tag) -> set[str]:
    """Return lowercase EPUB/ARIA/HTML semantic tokens for a link."""
    tokens: set[str] = set()
    for key in ("epub:type", "role", "rel"):
        value = node.get(key)
        values = value if isinstance(value, list) else [value]
        for raw in values:
            if raw is None:
                continue
            for token in str(raw).split():
                normalized = token.strip().lower()
                if normalized:
                    tokens.add(normalized)
                    # Some generators prefix values, for example z3998:footnote.
                    tokens.add(normalized.rsplit(":", 1)[-1])
    return tokens


def _inside_note_body(node: Tag) -> bool:
    """Check whether a node is inside an explicit footnote/endnote semantic container."""
    for parent in (node, *node.parents):
        if isinstance(parent, Tag) and _semantic_tokens(parent) & _NOTE_BODY_SEMANTICS:
            return True
    return False


def _has_note_identity(node: Tag) -> bool:
    """Check whether id, name or class explicitly identifies footnote body content."""
    identity: list[str] = []
    for key in ("id", "name", "class"):
        value = node.get(key)
        if isinstance(value, list):
            identity.extend(str(item) for item in value)
        elif value is not None:
            identity.append(str(value))
    return bool(_NOTE_TARGET_HINT.search(" ".join(identity)))


def _has_short_note_identity(node: Tag) -> bool:
    """Detect n1-style annotation IDs/names that require strong supporting structure."""
    return any(
        isinstance(node.get(key), str) and bool(_SHORT_NOTE_IDENTITY.fullmatch(str(node.get(key))))
        for key in ("id", "name")
    )


def _implicit_note_body_scope(node: Tag) -> Tag | None:
    """Find the nearest named annotation container lacking explicit EPUB semantics."""
    for parent in (node, *node.parents):
        if (
            isinstance(parent, Tag)
            and parent.name in {"aside", "li", "dd", "p", "div", "section"}
            and _has_note_identity(parent)
        ):
            return parent
    return None


def _short_note_body_scope(node: Tag) -> Tag | None:
    """Find an n1-style container; callers must also supply superscript or backlink evidence."""
    for parent in (node, *node.parents):
        if (
            isinstance(parent, Tag)
            and parent.name in {"aside", "li", "dd", "p"}
            and _has_short_note_identity(parent)
        ):
            return parent
    return None


def _inside_implicit_note_body(node: Tag) -> bool:
    """Check whether a node is in a named annotation container lacking EPUB semantics."""
    if _implicit_note_body_scope(node) is not None:
        return True
    short_scope = _short_note_body_scope(node)
    if short_scope is None or not _ANNOTATION_MARKER_ONLY.fullmatch(node.get_text("", strip=True)):
        return False
    before, after = _surrounding_text(short_scope, node)
    return bool(before.strip()) != bool(after.strip())


def _is_text_string(node: object) -> TypeGuard[NavigableString]:
    """Recognize actual text nodes, excluding XML processing instructions such as page-break
    markers.
    """
    return isinstance(node, NavigableString) and not isinstance(
        node, (Comment, ProcessingInstruction)
    )


def _hoist_processing_instructions(el: Tag) -> None:
    """Move block processing instructions before the block so writer clear() cannot erase them."""
    if el.parent is None:
        return
    extracted = [
        node.extract() for node in list(el.descendants) if isinstance(node, ProcessingInstruction)
    ]
    for node in extracted:
        el.insert_before(node)


def _surrounding_text(block: Tag, node: Tag) -> tuple[str, str]:
    """Return visible text before and after a node to identify leading backlinks."""

    def text_of(value: object) -> str:
        if _is_text_string(value):
            return str(value)
        if isinstance(value, Tag):
            return value.get_text(" ", strip=False)
        return ""

    before: list[str] = []
    after: list[str] = []
    cursor: Tag = node
    while cursor is not block and isinstance(cursor.parent, Tag):
        before.extend(text_of(sibling) for sibling in cursor.previous_siblings)
        after.extend(text_of(sibling) for sibling in cursor.next_siblings)
        cursor = cursor.parent
    return "".join(before), "".join(after)


def _annotation_relation(
    link: Tag,
    block: Tag,
    *,
    marker_wrapper: Tag | None,
    range_marker: Tag | None,
    marker_only: bool,
) -> str:
    """Classify structure-preserving internal links as annotation links, backlinks or ordinary
    links.
    """
    semantics = _semantic_tokens(link)
    if semantics & _BACKLINK_SEMANTICS:
        return "backlink"
    if semantics & _NOTEREF_SEMANTICS:
        return "noteref"
    if _inside_note_body(link):
        return "backlink"
    if marker_only and _inside_implicit_note_body(link):
        return "backlink"
    if marker_only and re.fullmatch(r"[↩↵←↑↓⤶\s]+", link.get_text("", strip=True)):
        return "backlink"

    # Footnotes often begin with a bare backlink number followed by explanatory text, without sup/sub.
    # Treat that leading link conservatively as a backlink to avoid injecting body text as annotation.
    if marker_wrapper is None and marker_only and link is not block:
        before, after = _surrounding_text(block, link)
        if not before.strip() and after.strip():
            return "backlink"

    if marker_wrapper is not None or range_marker is not None or marker_only:
        return "noteref"
    return "internal_link"


def _nearest_marker_wrapper(link: Tag, block: Tag) -> Tag | None:
    """Find the nearest semantic superscript/subscript wrapper between a link and its block.
    Besides native sup/sub, accept spans with explicit classes or inline styles indicating
    super/subscript. Ordinary spans remain body text.
    """

    def is_marker_wrapper(node: Tag) -> bool:
        if node.name in {"sup", "sub"}:
            return True
        if node.name != "span":
            return False
        classes = {
            str(value).strip().lower()
            for value in node.get_attribute_list("class")
            if str(value).strip()
        }
        if classes & {"sup", "super", "superscript", "sub", "subscript"}:
            return True
        style = node.get("style")
        return isinstance(style, str) and bool(
            re.search(r"(?:^|;)\s*vertical-align\s*:\s*(?:super|sub)\b", style, re.IGNORECASE)
        )

    parent = link.parent
    while isinstance(parent, Tag):
        if parent is block:
            return parent if is_marker_wrapper(parent) else None
        if is_marker_wrapper(parent):
            return parent
        parent = parent.parent
    return None


def _has_annotation_hint(
    link: Tag,
    marker: Tag,
    marker_text: str,
    *,
    allow_short_n: bool = False,
) -> bool:
    """Use explicit annotation evidence to distinguish note numbers from ordinary internal
    links.
    """
    decorated = bool(re.search(r"[^\d\s.·:\-]", marker_text))
    parsed = urlsplit(str(link.get("href", "")))
    attrs: list[str] = [parsed.fragment]
    for node in (link, marker):
        for key in ("id", "class", "role", "rel", "epub:type"):
            value = node.get(key)
            if isinstance(value, list):
                attrs.extend(str(item) for item in value)
            elif value is not None:
                attrs.append(str(value))
    hint = " ".join(attrs)
    numbered_note_fragment = bool(
        re.search(
            r"(?:notes?|footnotes?|endnotes?|fn)[\-_]?\d+$",
            parsed.fragment,
            re.IGNORECASE,
        )
    )
    short_n_fragment = allow_short_n and bool(_SHORT_NOTE_IDENTITY.fullmatch(parsed.fragment))
    return (
        decorated
        or bool(_ANNOTATION_HINT.search(hint))
        or numbered_note_fragment
        or short_n_fragment
    )


def _range_marker_node(link: Tag) -> Tag | None:
    """Recognize high-confidence trailing note markers without deleting semantic
    super/subscripts.
    Chemical subscripts and formula exponents are body content, not removable merely because
    they use sup/sub. Accept only trailing number-like markers supported by href, id, class,
    semantic attributes or decorations.
    """
    significant = [
        child for child in link.children if not (_is_text_string(child) and not str(child).strip())
    ]
    if not significant:
        return None
    candidate = significant[-1]
    if not isinstance(candidate, Tag) or candidate.name not in {"sup", "sub"}:
        return None
    marker_text = candidate.get_text("", strip=True)
    if not marker_text or not _ANNOTATION_MARKER_ONLY.fullmatch(marker_text):
        return None

    # Numeric subscripts are usually chemical or mathematical content. Treat sub as a note marker
    # only when clear decorations such as brackets, asterisks or arrows support that interpretation.
    decorated = bool(re.search(r"[^\d\s.·:\-]", marker_text))
    if candidate.name == "sub" and not decorated:
        return None
    return (
        candidate
        if _has_annotation_hint(link, candidate, marker_text, allow_short_n=True)
        else None
    )


def _semantic_link_text(link: Tag, marker_node: Tag | None = None) -> str:
    """Return link body text, excluding only confirmed trailing annotation numbers."""
    parts: list[str] = []

    def collect(parent: Tag) -> None:
        for child in parent.children:
            if isinstance(child, Tag):
                if child is marker_node or child.name in {"rt", "rp"}:
                    continue
                collect(child)
            elif _is_text_string(child):
                parts.append(str(child))

    collect(link)
    return re.sub(r"[ \t\r\n\f\v]+", " ", "".join(parts)).strip()


def _annotation_roots(
    block: Tag,
    anchor: str,
    resource_href: str,
) -> dict[int, dict[str, object]]:
    """Identify inline links, number their DOM roots and return temporary extraction
    specifications.
    """
    # When block itself is an ordinary link, such as li > a, replacing its child text preserves href.
    # No model alignment is needed. Record the root only if it also contains sup/sub note markers
    # that clear() would otherwise erase.
    links: list[Tag] = []
    if block.name == "a" and block.has_attr("href") and block.find(["sup", "sub"]):
        links.append(block)
    links.extend(block.find_all("a", href=True))

    roots: dict[int, dict[str, object]] = {}
    ordinal = 0
    for link in links:
        if not _is_internal_link(link):
            continue

        marker_wrapper = _nearest_marker_wrapper(link, block)
        if marker_wrapper is not None:
            wrapper_text = marker_wrapper.get_text("", strip=True)
            if not _has_annotation_hint(
                link,
                marker_wrapper,
                wrapper_text,
                allow_short_n=True,
            ):
                marker_wrapper = None
        range_marker = None if marker_wrapper is not None else _range_marker_node(link)
        semantic_text = _semantic_link_text(link, range_marker)
        if not semantic_text and marker_wrapper is None:
            # Image-only links and empty anchors have no text needing cross-language alignment. Preserve
            # the entire link through atomic inline handling instead of misclassifying and clearing it as a note.
            continue
        marker_shaped = bool(_ANNOTATION_MARKER_ONLY.fullmatch(semantic_text))
        marker_only = bool(
            marker_shaped
            and (
                _has_annotation_hint(link, link, semantic_text) or _inside_implicit_note_body(link)
            )
        )
        mode = "point" if marker_wrapper is not None or marker_only else "range"
        root = marker_wrapper if mode == "point" and marker_wrapper is not None else link
        raw_href = str(link.get("href") or "")
        resolved = resolve_epub_href(resource_href, raw_href)
        relation = _annotation_relation(
            link,
            block,
            marker_wrapper=marker_wrapper,
            range_marker=range_marker,
            marker_only=marker_only,
        )

        # Record each structural root once. Valid XHTML does not nest links, but this guard prevents
        # malformed documents from assigning multiple IDs to the same sup/sub marker.
        if id(root) in roots:
            continue

        annotation_id = f"{anchor}_annotation_{ordinal}"
        ordinal += 1
        if mode == "point":
            marker_text = root.get_text("", strip=True)
        else:
            marker_text = range_marker.get_text("", strip=True) if range_marker is not None else ""
        root[ANNOTATION_ID_ATTR] = annotation_id
        roots[id(root)] = {
            "id": annotation_id,
            "mode": mode,
            "marker_text": marker_text,
            "raw_href": raw_href,
            "target_key": resolved.target_key,
            "relation": relation,
            "marker_node_ids": {id(range_marker)} if range_marker is not None else set(),
            "root": root,
        }
    return roots


def _semantic_note_scope(node: Tag) -> Tag | None:
    """Find the nearest explicit footnote/endnote semantic container enclosing the destination
    anchor.
    """
    for candidate in (node, *node.parents):
        if isinstance(candidate, Tag) and _semantic_tokens(candidate) & _NOTE_BODY_SEMANTICS:
            return candidate
    return None


def _note_context_scope(node: Tag, fragment: str) -> tuple[Tag, bool]:
    """Select an annotation body's DOM scope and report whether its note identity is explicit."""
    semantic = _semantic_note_scope(node)
    if semantic is not None:
        return semantic, True

    implicit = _implicit_note_body_scope(node)
    if implicit is not None and (
        implicit.has_attr("data-tn-id") or implicit.find(True, attrs={"data-tn-id": True})
    ):
        return implicit, True

    short_scope = _short_note_body_scope(node)
    if short_scope is not None and (
        short_scope.has_attr("data-tn-id") or short_scope.find(True, attrs={"data-tn-id": True})
    ):
        # An n1 ID alone cannot promote an ordinary link to a note, but a confirmed noteref can use it
        # to obtain the complete list annotation instead of just the numbered anchor.
        return short_scope, False

    # Older EPUBs often wrap multi-paragraph notes in li/dd/aside without semantic attributes.
    # A div/section with explicit note/fn identity can also safely supply all its body blocks.
    has_note_identity = bool(_NOTE_TARGET_HINT.search(fragment)) or _has_note_identity(node)
    if node.name in {"aside", "li", "dd"} or (
        node.name in {"div", "section"} and has_note_identity
    ):
        if node.has_attr("data-tn-id") or node.find(True, attrs={"data-tn-id": True}):
            return node, False

    if node.has_attr("data-tn-id"):
        return node, False
    parent = node.find_parent(attrs={"data-tn-id": True})
    if isinstance(parent, Tag):
        return parent, False
    following = node.find_next(attrs={"data-tn-id": True})
    return (following, False) if isinstance(following, Tag) else (node, False)
