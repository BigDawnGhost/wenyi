"""从原始 EPUB 或生成式章节构建完整布局证据清单。"""

from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Sequence
from html import escape
from pathlib import Path
from typing import Any

from lxml import etree

from trans_novel.assemble.epub.rendering.bilingual import local_name
from trans_novel.assemble.epub.rendering.source_dom import parse_source_markup
from trans_novel.assemble.epub.rendering.theme.projection import build_projection
from trans_novel.assemble.epub.rendering.theme.service import package_protected_resources
from trans_novel.assemble.epub.rendering.theme.source_css import (
    collect_source_stylesheets,
    source_style_evidence,
)
from trans_novel.assemble.text import merged_paragraphs
from trans_novel.epub.archive import preflight_zip, read_member
from trans_novel.epub.layout import (
    LAYOUT_POLICY_VERSION,
    LayoutInventory,
    LayoutNode,
    source_node_digest,
)
from trans_novel.epub.navigation import resolve_epub_href
from trans_novel.epub.package import read_package
from trans_novel.ingest import KIND_HEADING, Chapter

_EPUB_TYPE = "{http://www.idpf.org/2007/ops}type"

_CONTEXT_CHARS = 1_000


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _file_digest(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tokens(node: etree._Element, name: str, *, lowercase: bool = False) -> tuple[str, ...]:
    value = str(node.get(name, ""))
    return tuple(dict.fromkeys((value.lower() if lowercase else value).split()))


def _epub_types(node: etree._Element) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            (
                *_tokens(node, _EPUB_TYPE, lowercase=True),
                *_tokens(node, "epub:type", lowercase=True),
            )
        )
    )


def _features(node: etree._Element) -> dict[str, Any]:
    roles = _tokens(node, "role", lowercase=True)
    return {
        "tag": local_name(node.tag),
        "classes": sorted(set(_tokens(node, "class"))),
        "epub_types": sorted(set(_epub_types(node))),
        "aria_role": roles[0] if roles else None,
        "aria_level": node.get("aria-level"),
        "language": node.get("lang") or node.get("{http://www.w3.org/XML/1998/namespace}lang"),
    }


def _ancestor_features(node: etree._Element) -> list[dict[str, Any]]:
    return [_features(ancestor) for ancestor in reversed(tuple(node.iterancestors()))]


def _append_tail(parent: etree._Element, value: str | None) -> None:
    if not value:
        return
    if len(parent):
        parent[-1].tail = (parent[-1].tail or "") + value
    else:
        parent.text = (parent.text or "") + value


def _safe_copy(
    node: etree._Element,
    forbidden: frozenset[etree._Element],
    *,
    root: bool = True,
) -> etree._Element:
    copied = etree.Element(node.tag, attrib=dict(node.attrib), nsmap=node.nsmap if root else None)
    copied.text = node.text
    for child in node:
        if not isinstance(child.tag, str) or child in forbidden:
            _append_tail(copied, child.tail)
            continue
        child_copy = _safe_copy(child, forbidden, root=False)
        child_copy.tail = child.tail
        copied.append(child_copy)
    return copied


def _safe_markup(
    node: etree._Element,
    protected: frozenset[etree._Element],
    sources: frozenset[etree._Element],
) -> str:
    return etree.tostring(
        _safe_copy(node, protected | sources),
        encoding="unicode",
        with_tail=False,
    )


def _context(text: str) -> dict[str, Any]:
    return {"text": text[:_CONTEXT_CHARS], "truncated": len(text) > _CONTEXT_CHARS}


def _node_id(resource_href: str, path: tuple[int, ...]) -> str:
    return _digest({"resource_href": resource_href, "path": list(path)})


def _group_key(
    node: etree._Element,
    parent: etree._Element | None,
    stylesheet_digest: str,
    matched_css: Sequence[str],
) -> str:
    features = _features(node)
    parent_features = _features(parent) if parent is not None else None
    return _digest(
        {
            "tag": features["tag"],
            "classes": features["classes"],
            "epub_types": features["epub_types"],
            "aria_role": features["aria_role"],
            "parent": (
                {
                    "tag": parent_features["tag"],
                    "classes": parent_features["classes"],
                }
                if parent_features is not None
                else None
            ),
            "stylesheet_family": stylesheet_digest,
            "matched_css": list(matched_css),
        }
    )


def _inventory_digest(source_sha256: str, nodes: Sequence[LayoutNode]) -> str:
    return _digest(
        {
            "source_sha256": source_sha256,
            "policy_version": LAYOUT_POLICY_VERSION,
            "nodes": [
                {
                    "node_id": node.node_id,
                    "resource_href": node.resource_href,
                    "path": list(node.path),
                    "source_sha256": node.source_sha256,
                    "group_key": node.group_key,
                    "sample": node.sample,
                }
                for node in nodes
            ],
        }
    )


def _generated_inventory(chapters: Sequence[Chapter]) -> LayoutInventory:
    rows: list[tuple[str, tuple[int, ...], str, str]] = []
    for chapter in chapters:
        resource_href = f"ch{chapter.index}.xhtml"
        for index, (kind, _target, source) in enumerate(merged_paragraphs(chapter)):
            if not source.strip():
                continue
            rows.append((resource_href, (index,), "h1" if kind == KIND_HEADING else "p", source))
    source_sha256 = _digest(
        [
            {"resource_href": href, "path": list(path), "tag": tag, "source": source}
            for href, path, tag, source in rows
        ]
    )
    nodes: list[LayoutNode] = []
    for position, (href, path, tag, source) in enumerate(rows):
        sample = {
            "source_markup": f"<{tag}>{escape(source)}</{tag}>",
            "features": {
                "tag": tag,
                "classes": [],
                "epub_types": [],
                "aria_role": None,
                "aria_level": int(tag[1:]) if tag.startswith("h") else None,
                "language": None,
            },
            "ancestors": [{"tag": "body", "classes": []}],
            "adjacent": {
                "before": _context(rows[position - 1][3]) if position else None,
                "after": _context(rows[position + 1][3]) if position + 1 < len(rows) else None,
            },
            "matched_css": [],
            "references": {"outgoing": [], "referenced_by": []},
        }
        node_sha = source_node_digest(tag, {}, source)
        nodes.append(
            LayoutNode(
                node_id=_node_id(href, path),
                resource_href=href,
                path=path,
                source_sha256=node_sha,
                group_key=_digest(
                    {
                        "tag": tag,
                        "classes": [],
                        "epub_types": [],
                        "aria_role": None,
                        "parent": {"tag": "body", "classes": []},
                        "stylesheet_family": _digest({}),
                    }
                ),
                sample=sample,
            )
        )
    return LayoutInventory(
        source_sha256=source_sha256,
        nodes=tuple(nodes),
        digest=_inventory_digest(source_sha256, nodes),
        policy_version=LAYOUT_POLICY_VERSION,
    )


def _outgoing(node: etree._Element) -> list[str]:
    return [
        href
        for descendant in node.iter()
        if isinstance(descendant.tag, str)
        and isinstance((href := descendant.get("href")), str)
        and href
    ]


def _add_reverse_references(nodes: list[LayoutNode], source_nodes: list[etree._Element]) -> None:
    targets: dict[tuple[str, str], list[str]] = {}

    def append(mapping: dict[Any, list[str]], key: Any, node_id: str) -> None:
        values = mapping.setdefault(key, [])
        if node_id not in values:
            values.append(node_id)

    for layout_node, source_node in zip(nodes, source_nodes, strict=True):
        for candidate in (source_node, *source_node.iterancestors()):
            anchor = candidate.get("id")
            if anchor:
                append(
                    targets,
                    (layout_node.resource_href, anchor),
                    layout_node.node_id,
                )
        for descendant in source_node.iterdescendants():
            anchor = descendant.get("id") if isinstance(descendant.tag, str) else None
            if anchor:
                append(
                    targets,
                    (layout_node.resource_href, anchor),
                    layout_node.node_id,
                )
    reverse: dict[str, list[str]] = {}
    for layout_node, source_node in zip(nodes, source_nodes, strict=True):
        for href in _outgoing(source_node):
            try:
                resolved = resolve_epub_href(layout_node.resource_href, href)
            except ValueError:
                continue
            target_ids = (
                targets.get((resolved.resource_href, resolved.fragment), [])
                if not resolved.external and resolved.fragment
                else []
            )
            for target_id in target_ids:
                append(reverse, target_id, layout_node.node_id)
    for node in nodes:
        node.sample["references"]["referenced_by"] = reverse.get(node.node_id, [])


def _epub_inventory(source_path: str) -> LayoutInventory:
    source_sha256 = _file_digest(source_path)
    nodes: list[LayoutNode] = []
    source_nodes: list[etree._Element] = []
    with zipfile.ZipFile(source_path) as archive:
        preflight_zip(archive)
        failures: list[dict[str, str]] = []
        package = read_package(archive, failures)
        if failures or not package["valid"]:
            raise ValueError("layout_source_invalid")
        model = package["model"]
        opf_path = package["opf_path"]
        opf_data = read_member(archive, archive.getinfo(opf_path))
        opf_tree, _opf_mode = parse_source_markup(opf_data)
        protected, fixed = package_protected_resources(opf_tree, model)
        excluded_resources = protected | fixed
        resources = [href for href in model["content_paths"] if href not in excluded_resources]
        for href in resources:
            data = read_member(archive, archive.getinfo(href))
            tree, _mode = parse_source_markup(data)
            root = tree.getroot()
            projection = build_projection(root)
            stylesheets = collect_source_stylesheets(archive, root, href)
            style_digest = _digest(
                {key: value.decode("utf-8") for key, value in sorted(stylesheets.items())}
            )
            evidence = source_style_evidence(
                root,
                projection.nodes,
                stylesheets,
                resource=href,
            )
            records = projection.snapshot["nodes"]
            eligible = [
                index
                for index in range(len(projection.nodes))
                if bool(records[index].get("isTextBlock"))
            ]
            eligible_texts = ["".join(projection.nodes[index].itertext()) for index in eligible]
            for position, index in enumerate(eligible):
                node = projection.nodes[index]
                path = projection.paths[index]
                text = "".join(node.itertext())
                features = _features(node)
                sample = {
                    "source_markup": _safe_markup(
                        node, projection.protected, projection.source_nodes
                    ),
                    "features": features,
                    "ancestors": _ancestor_features(node),
                    "adjacent": {
                        "before": _context(eligible_texts[position - 1]) if position else None,
                        "after": (
                            _context(eligible_texts[position + 1])
                            if position + 1 < len(eligible_texts)
                            else None
                        ),
                    },
                    "matched_css": list(evidence[index]),
                    "references": {"outgoing": _outgoing(node), "referenced_by": []},
                }
                nodes.append(
                    LayoutNode(
                        node_id=_node_id(href, path),
                        resource_href=href,
                        path=path,
                        source_sha256=source_node_digest(
                            local_name(node.tag), dict(node.attrib), text
                        ),
                        group_key=_group_key(
                            node,
                            node.getparent(),
                            style_digest,
                            evidence[index],
                        ),
                        sample=sample,
                    )
                )
                source_nodes.append(node)
    _add_reverse_references(nodes, source_nodes)
    return LayoutInventory(
        source_sha256=source_sha256,
        nodes=tuple(nodes),
        digest=_inventory_digest(source_sha256, nodes),
        policy_version=LAYOUT_POLICY_VERSION,
    )


def build_layout_inventory(source_path: str, chapters: Sequence[Chapter]) -> LayoutInventory:
    """只从原始源文、结构与样式构建确定性布局清单。"""
    if Path(source_path).suffix.lower() == ".epub":
        return _epub_inventory(source_path)
    return _generated_inventory(chapters)


__all__ = ["build_layout_inventory"]
