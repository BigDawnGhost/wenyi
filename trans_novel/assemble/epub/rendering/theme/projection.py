from __future__ import annotations

import json
import re
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from lxml import etree

from trans_novel.assemble.epub.rendering.bilingual import (
    BILINGUAL_DIRECT_TARGET_CLASS,
    XHTML_NS,
    local_name,
)
from trans_novel.assemble.epub.rendering.source_dom import element_children_lxml
from trans_novel.assemble.epub.rendering.theme.contracts import ThemeError

_MAX_NODES = 50_000
_MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
_MAX_TEXT = 2_048
_XML_SPACE = re.compile(r"[\t\n\r ]+")
_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"
_EPUB_TYPE = "{http://www.idpf.org/2007/ops}type"
_TEXT_BLOCKS = frozenset(
    {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote", "td", "th", "dt", "dd"}
)
_PROTECTED_TAGS = frozenset(
    {
        "nav",
        "script",
        "style",
        "form",
        "button",
        "datalist",
        "fieldset",
        "input",
        "meter",
        "optgroup",
        "option",
        "output",
        "progress",
        "select",
        "textarea",
        "code",
        "pre",
        "rt",
        "rp",
        "rtc",
    }
)
_PROTECTED_EPUB_TYPES = frozenset(
    {
        "toc",
        "landmarks",
        "page-list",
        "cover",
        "cover-image",
        "titlepage",
    }
)
_PROTECTED_ARIA_ROLES = frozenset(
    {
        "navigation",
        "doc-toc",
        "doc-landmarks",
        "doc-pagelist",
        "doc-cover",
        "doc-titlepage",
    }
)
_TABLE_ROLES = frozenset({"table", "row", "cell", "columnheader", "rowheader"})
_NAVIGATION_ROLES = frozenset({"navigation", "doc-toc", "doc-landmarks", "doc-pagelist"})
_QUOTE_ROLES = frozenset({"blockquote", "doc-pullquote", "doc-qna"})
_LIST_ROLES = frozenset({"list", "listitem", "doc-bibliography"})
_TABLE_EPUB_TYPES = frozenset({"table"})
_NAVIGATION_EPUB_TYPES = frozenset({"toc", "landmarks", "page-list"})
_QUOTE_EPUB_TYPES = frozenset({"epigraph", "pullquote", "qna"})
_LIST_EPUB_TYPES = frozenset({"list", "list-item", "bibliography"})
_EXPLICIT_EPUB_TYPES = frozenset(
    {
        "caption",
        "subtitle",
        "footnote",
        "footnotes",
        "endnote",
        "endnotes",
        "noteref",
    }
)
_EXPLICIT_ARIA_ROLES = frozenset(
    {"doc-footnote", "doc-endnote", "doc-endnotes", "doc-noteref", "doc-subtitle"}
)
_EXPLICIT_SEMANTIC_TAGS = frozenset({"caption", "figcaption"})


@dataclass(frozen=True, slots=True)
class ResourceProjection:
    snapshot: dict[str, Any]
    nodes: tuple[etree._Element, ...]
    paths: tuple[tuple[int, ...], ...]
    protected: frozenset[etree._Element]
    source_nodes: frozenset[etree._Element]


@dataclass(slots=True)
class _TextSummary:
    prefix: str = ""
    length: int = 0
    has_content: bool = False
    pending_space: bool = False
    seen_raw: bool = False
    starts_space: bool = False

    def append_text(self, value: str | None) -> None:
        if not value:
            return
        if not self.seen_raw:
            self.starts_space = value[0] in "\t\n\r "
            self.seen_raw = True
        position = 0
        for match in _XML_SPACE.finditer(value):
            self._append_word(value[position : match.start()])
            if self.has_content:
                self.pending_space = True
            position = match.end()
        self._append_word(value[position:])

    def append_summary(self, other: _TextSummary) -> None:
        if not other.seen_raw:
            return
        if not self.seen_raw:
            self.starts_space = other.starts_space
            self.seen_raw = True
        if not other.has_content:
            if self.has_content:
                self.pending_space = self.pending_space or other.starts_space
            return
        if self.has_content and (self.pending_space or other.starts_space):
            self._append_normalized(" ", 1)
        self._append_normalized(other.prefix, other.length)
        self.has_content = True
        self.pending_space = other.pending_space

    def _append_word(self, value: str) -> None:
        if not value:
            return
        if self.has_content and self.pending_space:
            self._append_normalized(" ", 1)
        self._append_normalized(value, len(value))
        self.has_content = True
        self.pending_space = False

    def _append_normalized(self, prefix: str, length: int) -> None:
        remaining = _MAX_TEXT - len(self.prefix)
        if remaining > 0:
            self.prefix += prefix[:remaining]
        self.length += length


def _namespace(node: etree._Element) -> str:
    tag = node.tag
    return tag[1:].split("}", 1)[0] if isinstance(tag, str) and tag.startswith("{") else ""


def _tokens(node: etree._Element, name: str) -> tuple[str, ...]:
    return tuple(str(node.get(name, "")).lower().split())


def _epub_types(node: etree._Element) -> tuple[str, ...]:
    values = (*_tokens(node, _EPUB_TYPE), *_tokens(node, "epub:type"))
    return tuple(dict.fromkeys(values))


def _aria_roles(node: etree._Element) -> tuple[str, ...]:
    return _tokens(node, "role")


def _is_source(node: etree._Element) -> bool:
    return "tn-source" in str(node.get("class", "")).split()


def _is_transparent(node: etree._Element) -> bool:
    return BILINGUAL_DIRECT_TARGET_CLASS in str(node.get("class", "")).split()


def _is_protected_root(node: etree._Element) -> bool:
    name = local_name(node.tag)
    namespace = _namespace(node)
    return (
        namespace in {"http://www.w3.org/2000/svg", "http://www.w3.org/1998/Math/MathML"}
        or name in _PROTECTED_TAGS
        or bool(set(_epub_types(node)) & _PROTECTED_EPUB_TYPES)
        or bool(set(_aria_roles(node)) & _PROTECTED_ARIA_ROLES)
    )


def _is_explicit_semantic(node: etree._Element) -> bool:
    return (
        local_name(node.tag) in _EXPLICIT_SEMANTIC_TAGS
        or bool(set(_epub_types(node)) & _EXPLICIT_EPUB_TYPES)
        or bool(set(_aria_roles(node)) & _EXPLICIT_ARIA_ROLES)
    )


def _descendant_sets(
    root: etree._Element,
    excluded: Collection[etree._Element],
    skip_resource: bool,
) -> tuple[frozenset[etree._Element], frozenset[etree._Element]]:
    excluded_ids = {id(node) for node in excluded}
    protected: set[etree._Element] = set()
    sources: set[etree._Element] = set()
    stack = [(root, skip_resource, False)]
    while stack:
        node, inherited_protection, inherited_source = stack.pop()
        if not isinstance(node.tag, str):
            continue
        node_protected = (
            inherited_protection or id(node) in excluded_ids or _is_protected_root(node)
        )
        node_source = inherited_source or _is_source(node)
        if node_protected:
            protected.add(node)
        if node_source:
            sources.add(node)
        for child in reversed(node):
            if isinstance(child.tag, str):
                stack.append((child, node_protected, node_source))
    return frozenset(protected), frozenset(sources)


def _body_and_path(root: etree._Element) -> tuple[etree._Element, tuple[int, ...]]:
    bodies = [
        node
        for node in root.iter()
        if isinstance(node.tag, str)
        and local_name(node.tag) == "body"
        and _namespace(node) in {"", XHTML_NS}
    ]
    if len(bodies) != 1:
        raise ThemeError("theme_config", "invalid_body")
    body = bodies[0]
    path: list[int] = []
    node = body
    while node is not root:
        parent = node.getparent()
        if parent is None:
            raise ThemeError("theme_config", "invalid_body")
        path.append(element_children_lxml(parent).index(node))
        node = parent
    return body, tuple(reversed(path))


def _semantic_context(node: etree._Element) -> tuple[bool, bool, bool, bool]:
    name = local_name(node.tag)
    roles = set(_aria_roles(node))
    epub_types = set(_epub_types(node))
    return (
        name in {"table", "thead", "tbody", "tfoot", "tr", "td", "th"}
        or bool(roles & _TABLE_ROLES)
        or bool(epub_types & _TABLE_EPUB_TYPES),
        name == "nav"
        or bool(roles & _NAVIGATION_ROLES)
        or bool(epub_types & _NAVIGATION_EPUB_TYPES),
        name in {"blockquote", "q"}
        or bool(roles & _QUOTE_ROLES)
        or bool(epub_types & _QUOTE_EPUB_TYPES),
        name in {"ul", "ol", "dl", "li"}
        or bool(roles & _LIST_ROLES)
        or bool(epub_types & _LIST_EPUB_TYPES),
    )


def _language(node: etree._Element, inherited: str | None) -> str | None:
    if _XML_LANG in node.attrib:
        return node.attrib[_XML_LANG]
    if "lang" in node.attrib:
        return node.attrib["lang"]
    return inherited


def _summaries(
    body: etree._Element,
    protected: frozenset[etree._Element],
    sources: frozenset[etree._Element],
) -> tuple[dict[etree._Element, _TextSummary], dict[etree._Element, bool]]:
    order: list[etree._Element] = []
    eligible_count = 0
    stack = [body]
    while stack:
        node = stack.pop()
        order.append(node)
        if node in protected or node in sources:
            continue
        if node is body or not _is_transparent(node):
            eligible_count += 1
            if eligible_count > _MAX_NODES:
                raise ThemeError("theme_limit", "snapshot_nodes")
        stack.extend(child for child in node if isinstance(child.tag, str))
    summaries: dict[etree._Element, _TextSummary] = {}
    block_subtrees: dict[etree._Element, bool] = {}
    for node in reversed(order):
        unavailable = node in protected or node in sources
        summary = _TextSummary()
        if not unavailable:
            summary.append_text(node.text)
            for child in node:
                if isinstance(child.tag, str) and child not in protected and child not in sources:
                    summary.append_summary(summaries[child])
                summary.append_text(child.tail)
        summaries[node] = summary
        descendant_block = any(
            block_subtrees.get(child, False)
            for child in node
            if isinstance(child.tag, str) and child not in protected and child not in sources
        )
        name = local_name(node.tag)
        has_direct_text = bool((node.text or "").strip()) or any(
            bool((child.tail or "").strip()) for child in element_children_lxml(node)
        )
        own_block = (
            not unavailable
            and not _is_transparent(node)
            and (
                name in _TEXT_BLOCKS
                or (_is_explicit_semantic(node) and has_direct_text)
                or (name == "div" and summary.length > 0 and not descendant_block)
            )
        )
        block_subtrees[node] = own_block or descendant_block
    return summaries, block_subtrees


def build_projection(
    root: etree._Element,
    *,
    excluded: Collection[etree._Element] = (),
    skip_resource: bool = False,
) -> ResourceProjection:
    body, body_path = _body_and_path(root)
    protected, sources = _descendant_sets(root, excluded, skip_resource)
    summaries, block_subtrees = _summaries(body, protected, sources)

    inherited_context = [False, False, False, False]
    inherited_language: str | None = None
    for ancestor in reversed(tuple(body.iterancestors())):
        inherited_context = [
            current or added
            for current, added in zip(inherited_context, _semantic_context(ancestor), strict=True)
        ]
        inherited_language = _language(ancestor, inherited_language)

    records: list[dict[str, Any]] = []
    nodes: list[etree._Element] = []
    paths: list[tuple[int, ...]] = []
    sibling_groups: dict[int | None, list[int]] = {}
    stack = [(body, body_path, None, tuple(inherited_context), inherited_language)]
    while stack:
        node, path, parent_id, context, language = stack.pop()
        if node in protected or node in sources:
            continue
        current_language = _language(node, language)
        transparent = node is not body and _is_transparent(node)
        projected_parent = parent_id
        if not transparent:
            node_id = len(records)
            if node_id >= _MAX_NODES:
                raise ThemeError("theme_limit", "snapshot_nodes")
            roles = _aria_roles(node)
            level_value = node.get("aria-level")
            aria_level = int(level_value) if level_value in {"1", "2", "3", "4", "5", "6"} else None
            summary = summaries[node]
            name = local_name(node.tag)
            records.append(
                {
                    "id": node_id,
                    "parentId": parent_id,
                    "previousSiblingId": None,
                    "nextSiblingId": None,
                    "tag": name,
                    "namespace": _namespace(node),
                    "classes": str(node.get("class", "")).split(),
                    "epubTypes": list(_epub_types(node)),
                    "ariaRole": roles[0] if roles else None,
                    "ariaLevel": aria_level,
                    "language": current_language,
                    "text": summary.prefix,
                    "textLength": summary.length,
                    "textTruncated": summary.length > _MAX_TEXT,
                    "isTextBlock": name in _TEXT_BLOCKS
                    or (_is_explicit_semantic(node) and bool(summary.length))
                    or (
                        name == "div"
                        and summary.length > 0
                        and not any(
                            block_subtrees.get(child, False)
                            for child in node
                            if isinstance(child.tag, str)
                            and child not in protected
                            and child not in sources
                        )
                    ),
                    "context": {
                        "inTable": context[0],
                        "inNavigation": context[1],
                        "inQuote": context[2],
                        "inList": context[3],
                    },
                }
            )
            nodes.append(node)
            paths.append(path)
            sibling_groups.setdefault(parent_id, []).append(node_id)
            projected_parent = node_id
        child_context = tuple(
            current or added
            for current, added in zip(context, _semantic_context(node), strict=True)
        )
        children = list(enumerate(element_children_lxml(node)))
        for index, child in reversed(children):
            if isinstance(child.tag, str):
                stack.append(
                    (child, (*path, index), projected_parent, child_context, current_language)
                )

    for siblings in sibling_groups.values():
        for position, node_id in enumerate(siblings):
            records[node_id]["previousSiblingId"] = siblings[position - 1] if position else None
            records[node_id]["nextSiblingId"] = (
                siblings[position + 1] if position + 1 < len(siblings) else None
            )
    snapshot: dict[str, Any] = {"apiVersion": 1, "nodes": records}
    encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode()
    if len(encoded) > _MAX_SNAPSHOT_BYTES:
        raise ThemeError("theme_limit", "snapshot_bytes")
    return ResourceProjection(snapshot, tuple(nodes), tuple(paths), protected, sources)
