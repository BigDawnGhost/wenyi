"""确定性识别 EPUB 注释关系。"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping
from typing import Literal, TypedDict
from urllib.parse import urlsplit

from lxml import etree

from trans_novel.epub.archive import safe_name
from trans_novel.epub.navigation import resolve_epub_href


class NoteMarker(TypedDict):
    resource_href: str
    path: list[int]
    kind: Literal["noteref", "backlink"]
    label: str
    target_resource: str
    target_path: list[int]


class NoteTarget(TypedDict):
    resource_href: str
    path: list[int]
    kind: Literal["footnote", "endnote"]


class NoteRelations(TypedDict):
    version: Literal[1]
    markers: list[NoteMarker]
    targets: list[NoteTarget]


_NOTE_BLOCK_TAGS = {"p", "li", "aside"}
_PROTECTED_TAGS = {"script", "style", "code", "math", "svg", "form", "nav"}
_SYMBOL_MARKER_RE = re.compile(r"^(?:\*{1,3}|†{1,3}|‡{1,3}|§{1,3}|¶{1,3})$")
_NUMBER_MARKER_RE = re.compile(r"^(?:[0-9]{1,4}|\[[0-9]{1,4}\]|\([0-9]{1,4}\))$")
_WS_RE = re.compile(r"[ \t\r\n\f\v]+")


def _local_name(element: etree._Element) -> str:
    return element.tag.rsplit("}", 1)[-1].lower() if isinstance(element.tag, str) else ""


def _tokens(element: etree._Element) -> tuple[set[str], set[str]]:
    epub_type = element.get("{http://www.idpf.org/2007/ops}type", element.get("epub:type", ""))
    return set(epub_type.split()), set(element.get("role", "").split())


def _marker_kinds(
    element: etree._Element,
) -> set[Literal["noteref", "backlink"]]:
    epub_types, roles = _tokens(element)
    kinds: set[Literal["noteref", "backlink"]] = set()
    if "noteref" in epub_types or "doc-noteref" in roles:
        kinds.add("noteref")
    if "backlink" in epub_types or "doc-backlink" in roles:
        kinds.add("backlink")
    return kinds


def _marker_kind(element: etree._Element) -> Literal["noteref", "backlink"] | None:
    kinds = _marker_kinds(element)
    return next(iter(kinds)) if len(kinds) == 1 else None


def _target_kinds(
    element: etree._Element,
) -> set[Literal["footnote", "endnote"]]:
    epub_types, roles = _tokens(element)
    kinds: set[Literal["footnote", "endnote"]] = set()
    if "footnote" in epub_types or "doc-footnote" in roles:
        kinds.add("footnote")
    if "endnote" in epub_types or "doc-endnote" in roles:
        kinds.add("endnote")
    return kinds


def _target_kind(element: etree._Element) -> Literal["footnote", "endnote"] | None:
    kinds = _target_kinds(element)
    return next(iter(kinds)) if len(kinds) == 1 else None


def _element_children(element: etree._Element) -> list[etree._Element]:
    return [child for child in element if isinstance(child.tag, str)]


def _element_path(root: etree._Element, element: etree._Element) -> list[int]:
    path: list[int] = []
    current = element
    while current is not root:
        parent = current.getparent()
        if parent is None:
            raise ValueError("EPUB note element is detached from its resource root")
        path.append(_element_children(parent).index(current))
        current = parent
    return list(reversed(path))


def _is_protected(element: etree._Element) -> bool:
    return any(
        _local_name(candidate) in _PROTECTED_TAGS
        for candidate in (element, *element.iterancestors())
    )


def _anchor_label(anchor: etree._Element) -> str:
    return "".join(anchor.itertext())


def _normalized_label(anchor: etree._Element) -> str:
    return _WS_RE.sub(" ", _anchor_label(anchor)).strip()


def _short_marker_kind(label: str) -> Literal["symbol", "number"] | None:
    if _SYMBOL_MARKER_RE.fullmatch(label):
        return "symbol"
    if _NUMBER_MARKER_RE.fullmatch(label):
        return "number"
    return None


def _labels_correspond(left: str, right: str) -> bool:
    if _NUMBER_MARKER_RE.fullmatch(left) and _NUMBER_MARKER_RE.fullmatch(right):
        return left.strip("[]()") == right.strip("[]()")
    return left == right


def _identifier_indexes(
    resources: Mapping[str, etree._Element], excluded: set[str]
) -> dict[str, dict[str, etree._Element | None]]:
    indexes: dict[str, dict[str, etree._Element | None]] = {}
    for href, root in resources.items():
        if href in excluded:
            continue
        index: dict[str, etree._Element | None] = {}
        for element in root.iter():
            if not isinstance(element.tag, str):
                continue
            for identifier in dict.fromkeys((element.get("id"), element.get("name"))):
                if not identifier:
                    continue
                previous = index.get(identifier)
                index[identifier] = (
                    element if previous is None and identifier not in index else None
                )
        indexes[href] = index
    return indexes


def _resolve_target(
    resource_href: str,
    anchor: etree._Element,
    resources: Mapping[str, etree._Element],
    indexes: Mapping[str, Mapping[str, etree._Element | None]],
) -> tuple[str, etree._Element] | None:
    raw_href = anchor.get("href", "")
    if not raw_href:
        return None
    try:
        parsed = urlsplit(raw_href)
        resolved = resolve_epub_href(resource_href, raw_href)
    except ValueError:
        return None
    if parsed.query:
        return None
    if (
        resolved.external
        or not resolved.resource_href
        or not resolved.fragment
        or not safe_name(resolved.resource_href)
        or resolved.resource_href not in resources
    ):
        return None
    target = indexes.get(resolved.resource_href, {}).get(resolved.fragment)
    return (
        (resolved.resource_href, target)
        if target is not None and not _is_protected(target)
        else None
    )


def _resolved_anchors(
    resources: Mapping[str, etree._Element],
    excluded: set[str],
    indexes: Mapping[str, Mapping[str, etree._Element | None]],
) -> tuple[
    list[tuple[str, etree._Element]],
    dict[int, tuple[str, etree._Element] | None],
]:
    anchors = [
        (href, element)
        for href, root in resources.items()
        if href not in excluded
        for element in root.iter()
        if _local_name(element) == "a" and not _is_protected(element)
    ]
    return anchors, {
        id(anchor): _resolve_target(href, anchor, resources, indexes) for href, anchor in anchors
    }


def _note_block(element: etree._Element) -> etree._Element | None:
    fallback: etree._Element | None = None
    for candidate in (element, *element.iterancestors()):
        kinds = _target_kinds(candidate)
        if len(kinds) > 1:
            return None
        if len(kinds) == 1:
            return candidate
        if fallback is None and _local_name(candidate) in _NOTE_BLOCK_TAGS:
            fallback = candidate
    return fallback


def _has_note_semantics(element: etree._Element) -> bool:
    return any(
        _target_kind(candidate) is not None for candidate in (element, *element.iterancestors())
    )


def _text_before(root: etree._Element, target: etree._Element) -> str:
    parts: list[str] = []

    def walk(node: etree._Element) -> bool:
        if node is target:
            return True
        if node.text:
            parts.append(node.text)
        for child in node:
            if not isinstance(child.tag, str):
                continue
            if walk(child):
                return True
            if child.tail:
                parts.append(child.tail)
        return False

    walk(root)
    return "".join(parts)


def _at_note_start(block: etree._Element, anchor: etree._Element) -> bool:
    prefix = _WS_RE.sub("", _text_before(block, anchor))
    if not prefix:
        return True
    while prefix:
        match = re.match(
            r"(?:\*{1,3}|†{1,3}|‡{1,3}|§{1,3}|¶{1,3}|[0-9]{1,4}|\[[0-9]{1,4}\]|\([0-9]{1,4}\))",
            prefix,
        )
        if match is None:
            return False
        prefix = prefix[match.end() :]
    return True


def _preceding_empty_identifier(anchor: etree._Element) -> etree._Element | None:
    top = anchor
    while top.getparent() is not None and _local_name(top.getparent()) in {"sup", "span"}:
        parent = top.getparent()
        if (
            (parent.text or "").strip()
            or (top.tail or "").strip()
            or len(_element_children(parent)) != 1
        ):
            break
        top = parent
    previous = top.getprevious()
    if (
        previous is not None
        and _local_name(previous) == "a"
        and (previous.get("id") or previous.get("name"))
        and not _anchor_label(previous).strip()
        and not (previous.tail or "").strip()
    ):
        return previous
    return None


_InferredPair = tuple[
    str,
    etree._Element,
    str,
    etree._Element,
    etree._Element,
    str,
    etree._Element,
    etree._Element,
]


def _inferred_note_pairs(
    anchors: list[tuple[str, etree._Element]],
    resolved: Mapping[int, tuple[str, etree._Element] | None],
    resource_kinds: Mapping[str, Literal["footnote", "endnote"]],
) -> list[_InferredPair]:
    pairs: list[_InferredPair] = []
    for ref_href, ref_anchor in anchors:
        ref_kinds = _marker_kinds(ref_anchor)
        if ref_kinds and ref_kinds != {"noteref"}:
            continue
        ref_semantic = ref_kinds == {"noteref"}
        ref_label = _normalized_label(ref_anchor)
        marker_type = _short_marker_kind(ref_label)
        if not ref_semantic and marker_type is None:
            continue
        linked_note = resolved[id(ref_anchor)]
        if linked_note is None:
            continue
        note_href, note_target = linked_note
        note_block = _note_block(note_target)
        if note_block is None:
            continue
        accepted_target = _preceding_empty_identifier(ref_anchor)
        for backlink_href, backlink in anchors:
            if backlink_href != note_href or not (
                backlink is note_block
                or any(backlink is node for node in note_block.iterdescendants())
            ):
                continue
            backlink_kinds = _marker_kinds(backlink)
            if backlink_kinds and backlink_kinds != {"backlink"}:
                continue
            backlink_semantic = backlink_kinds == {"backlink"}
            backlink_label = _normalized_label(backlink)
            if not backlink_semantic and _short_marker_kind(backlink_label) is None:
                continue
            linked_ref = resolved[id(backlink)]
            if linked_ref is None or linked_ref[0] != ref_href:
                continue
            if linked_ref[1] is not ref_anchor and linked_ref[1] is not accepted_target:
                continue
            if not _at_note_start(note_block, backlink):
                continue
            if not (
                ref_semantic or backlink_semantic or _labels_correspond(ref_label, backlink_label)
            ):
                continue
            if (
                not ref_semantic
                and marker_type == "number"
                and not _has_note_semantics(note_block)
                and note_href not in resource_kinds
            ):
                continue
            pairs.append(
                (
                    ref_href,
                    ref_anchor,
                    note_href,
                    note_target,
                    note_block,
                    backlink_href,
                    backlink,
                    linked_ref[1],
                )
            )
    return pairs


def detect_note_relations(
    resources: Mapping[str, etree._Element],
    *,
    excluded_resources: Collection[str] = (),
    note_resources: Mapping[str, Literal["footnote", "endnote"]] | None = None,
) -> NoteRelations:
    """返回经源文验证的注释标记及正文目标位置，不修改 DOM。"""
    excluded = set(excluded_resources)
    indexes = _identifier_indexes(resources, excluded)
    resource_kinds = note_resources or {}
    anchors, resolved = _resolved_anchors(resources, excluded, indexes)
    marker_records: dict[tuple[str, tuple[int, ...], str], NoteMarker] = {}
    target_records: dict[tuple[str, tuple[int, ...]], NoteTarget] = {}

    def add_marker(
        href: str,
        anchor: etree._Element,
        kind: Literal["noteref", "backlink"],
        target_href: str,
        target: etree._Element,
    ) -> None:
        path = _element_path(resources[href], anchor)
        key = (href, tuple(path), kind)
        marker_records.setdefault(
            key,
            {
                "resource_href": href,
                "path": path,
                "kind": kind,
                "label": _anchor_label(anchor),
                "target_resource": target_href,
                "target_path": _element_path(resources[target_href], target),
            },
        )

    def add_target(href: str, block: etree._Element) -> None:
        path = _element_path(resources[href], block)
        key = (href, tuple(path))
        target_records.setdefault(
            key,
            {
                "resource_href": href,
                "path": path,
                "kind": _target_kind(block) or resource_kinds.get(href, "footnote"),
            },
        )

    for href, anchor in anchors:
        explicit_kind = _marker_kind(anchor)
        linked = resolved[id(anchor)]
        if explicit_kind is None or linked is None:
            continue
        target_href, target = linked
        add_marker(href, anchor, explicit_kind, target_href, target)
        note_block = _note_block(target) if explicit_kind == "noteref" else _note_block(anchor)
        if note_block is not None:
            add_target(target_href if explicit_kind == "noteref" else href, note_block)

    inferred_pairs = _inferred_note_pairs(anchors, resolved, resource_kinds)
    inferred_kinds: dict[tuple[str, tuple[int, ...]], set[Literal["noteref", "backlink"]]] = {}
    for ref_href, ref, _, _, _, backlink_href, backlink, _ in inferred_pairs:
        ref_key = (ref_href, tuple(_element_path(resources[ref_href], ref)))
        backlink_key = (
            backlink_href,
            tuple(_element_path(resources[backlink_href], backlink)),
        )
        inferred_kinds.setdefault(ref_key, set()).add("noteref")
        inferred_kinds.setdefault(backlink_key, set()).add("backlink")
    conflicts = {key for key, kinds in inferred_kinds.items() if len(kinds) > 1}
    for (
        ref_href,
        ref,
        note_href,
        note_target,
        note_block,
        backlink_href,
        backlink,
        linked_ref,
    ) in inferred_pairs:
        ref_key = (ref_href, tuple(_element_path(resources[ref_href], ref)))
        backlink_key = (
            backlink_href,
            tuple(_element_path(resources[backlink_href], backlink)),
        )
        if ref_key in conflicts or backlink_key in conflicts:
            continue
        add_marker(ref_href, ref, "noteref", note_href, note_target)
        add_marker(backlink_href, backlink, "backlink", ref_href, linked_ref)
        add_target(note_href, note_block)

    return {
        "version": 1,
        "markers": list(marker_records.values()),
        "targets": list(target_records.values()),
    }


__all__ = ["NoteMarker", "NoteRelations", "NoteTarget", "detect_note_relations"]
