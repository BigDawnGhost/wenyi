"""Exact DOM and source-model proof helpers."""

from __future__ import annotations

import hashlib
import re
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from bs4.element import Tag
from lxml import etree

from trans_novel.assemble.epub.verification import archive_model, structure
from trans_novel.epub.package import HTML_MEDIA, read_package

MAX_MEMBER_BYTES = archive_model.MAX_MEMBER_BYTES
BLOCK_TAGS = structure.BLOCK_TAGS
HEADING_TAGS = structure.HEADING_TAGS


def load_doc_segments(path: Path) -> dict[str, list[tuple[str, str]]]:
    """Use production EPUB parsing to obtain per-resource merged source blocks."""
    try:
        from trans_novel.ingest import load_document

        doc = load_document(str(path), "en", "zh")
    except Exception:
        return {}
    grouped: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for chapter in doc.chapters:
        for segment in chapter.segments:
            resource = segment.resource_href or ""
            if not resource:
                continue
            if segment.cont and grouped[resource]:
                old_kind, old_text = grouped[resource][-1]
                grouped[resource][-1] = (old_kind, old_text + segment.source)
            else:
                grouped[resource].append((segment.kind, segment.source))
    return dict(grouped)


def leaf_line_texts(element: Tag) -> list[str]:
    if not element.find("br", recursive=False):
        return [norm_text(element.get_text("", strip=False))]
    lines: list[str] = []
    current: list[str] = []
    for child in element.children:
        if isinstance(child, Tag) and child.name == "br":
            lines.append(norm_text("".join(current)))
            current = []
        elif isinstance(child, Tag):
            current.append(child.get_text("", strip=False))
        else:
            current.append(str(child))
    lines.append(norm_text("".join(current)))
    return [line for line in lines if line]


def dom_segments(path: Path) -> dict[str, list[tuple[str, str]]]:
    """Extract ordered leaf structural blocks directly from EPUB resources."""
    result: dict[str, list[tuple[str, str]]] = defaultdict(list)
    try:
        with zipfile.ZipFile(path, "r") as zf:
            info = read_package(zf, [])
            for item in info["model"]["resolved"]:
                if item["media"] not in HTML_MEDIA or "nav" in item["properties"].split():
                    continue
                data = archive_model.model_read(zf, item["path"], [])
                if data is None:
                    continue
                soup, _ = structure.html_soup(data, item["media"])
                candidates = BLOCK_TAGS | {"div"}
                for element in soup.find_all(list(candidates)):
                    text = norm_text(element.get_text("", strip=False))
                    if not text:
                        continue
                    has_descendant = any(
                        isinstance(descendant, Tag)
                        and descendant.name in candidates
                        and norm_text(descendant.get_text("", strip=False))
                        for descendant in element.find_all(True)
                    )
                    if has_descendant:
                        continue
                    kind = "heading" if element.name in HEADING_TAGS else "text"
                    for line in leaf_line_texts(element):
                        result[item["path"]].append((kind, line))
    except (OSError, zipfile.BadZipFile):
        return {}
    return dict(result)


def norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def source_span_target_candidates(node: Tag) -> list[str]:
    parent = node.parent
    if node.name != "span" or not isinstance(parent, Tag) or parent.name not in BLOCK_TAGS:
        return []
    candidates: list[str] = []
    for sibling, direction in (
        (node.previous_sibling, "previous"),
        (node.next_sibling, "next"),
    ):
        crossed_separator = False
        parts: list[str] = []
        while sibling is not None:
            if isinstance(sibling, Tag):
                if sibling.name == "br":
                    if crossed_separator:
                        break
                    crossed_separator = True
                    sibling = getattr(sibling, f"{direction}_sibling", None)
                    continue
                if "tn-source" in sibling.get("class", []):
                    break
                text = norm_text(sibling.get_text("", strip=False))
            else:
                text = norm_text(str(sibling))
            if text:
                parts.append(text)
            sibling = getattr(sibling, f"{direction}_sibling", None)
        if parts:
            candidates.append(norm_text(" ".join(parts)))
    return candidates


def source_node_target_candidates(node: Tag) -> list[str]:
    candidates = source_span_target_candidates(node)
    if candidates:
        return candidates
    parent = node.parent
    if isinstance(parent, Tag) and parent.name in {"li", "blockquote", "td", "th"}:
        source_text = norm_text(node.get_text("", strip=False))
        return [norm_text(parent.get_text("", strip=False).replace(source_text, "", 1))]
    result: list[str] = []
    for sibling in (node.previous_sibling, node.next_sibling):
        if (
            isinstance(sibling, Tag)
            and (sibling.name in BLOCK_TAGS or sibling.name == "span")
            and "tn-source" not in sibling.get("class", [])
        ):
            result.append(norm_text(sibling.get_text("", strip=False)))
    return result


def source_span_target_text(node: Tag) -> str | None:
    candidates = source_span_target_candidates(node)
    return candidates[0] if candidates else None


def source_node_attached(node: Tag) -> bool:
    parent = node.parent
    if not isinstance(parent, Tag) or parent.name in HEADING_TAGS:
        return False
    if node.name == "span":
        return source_span_target_text(node) is not None
    block_adjacent = any(
        isinstance(sibling, Tag) and sibling.name in BLOCK_TAGS
        for sibling in list(node.previous_siblings) + list(node.next_siblings)
    )
    return parent.name in {"li", "blockquote", "td", "th"} or block_adjacent


def source_subset(
    path: Path,
    source_path: Path,
    soups: dict[str, BeautifulSoup],
    failures: list[dict[str, str]],
    checked: dict[str, int],
) -> None:
    source_segments = dom_segments(source_path)
    for resource, soup in soups.items():
        nodes = soup.select(".tn-source")
        if not nodes:
            continue
        blocks = source_segments.get(resource)
        if blocks is None:
            blocks = next(
                (value for key, value in source_segments.items() if key.endswith(resource)), []
            )
        allowed = Counter(
            hashlib.sha256(norm_text(text).encode("utf-8")).hexdigest()
            for kind, text in blocks
            if kind != "heading"
        )
        seen = Counter()
        for node in nodes:
            checked["bilingual_source"] += 1
            digest = hashlib.sha256(
                norm_text(node.get_text("", strip=False)).encode("utf-8")
            ).hexdigest()
            seen[digest] += 1
            if seen[digest] > allowed[digest]:
                failures.append(
                    archive_model.item(
                        "bilingual_source", "source_node_unexpected", resource, "not_source_segment"
                    )
                )
        if len(nodes) > sum(allowed.values()):
            failures.append(
                archive_model.item(
                    "bilingual_source", "source_node_count", resource, "count_mismatch"
                )
            )


def exact_bilingual_proof(
    source_path: Path,
    mono_path: Path,
    bilingual_soups: dict[str, BeautifulSoup],
    failures: list[dict[str, str]],
    checked: dict[str, int],
) -> None:
    source_segments = dom_segments(source_path)
    mono_segments = dom_segments(mono_path)
    resources = sorted(set(source_segments) | set(mono_segments) | set(bilingual_soups))
    for resource in resources:
        source_blocks = source_segments.get(resource, [])
        mono_blocks = mono_segments.get(resource, [])
        if len(source_blocks) != len(mono_blocks):
            failures.append(
                archive_model.item(
                    "bilingual_source", "segment_structure_mismatch", resource, "mono"
                )
            )
        expected: list[tuple[str, str]] = []
        for index, (kind, source_text) in enumerate(source_blocks):
            target_text = mono_blocks[index][1] if index < len(mono_blocks) else ""
            if (
                kind != "heading"
                and norm_text(source_text)
                and norm_text(source_text) != norm_text(target_text)
            ):
                expected.append(
                    (
                        hashlib.sha256(norm_text(source_text).encode("utf-8")).hexdigest(),
                        hashlib.sha256(norm_text(target_text).encode("utf-8")).hexdigest(),
                    )
                )
        soup = bilingual_soups.get(resource)
        if soup is None:
            soup = next(
                (value for key, value in bilingual_soups.items() if key.endswith(resource)), None
            )
        actual_nodes = soup.select(".tn-source") if soup is not None else []
        observed: list[tuple[str, str]] = []
        for node in actual_nodes:
            source_norm = norm_text(node.get_text("", strip=False))
            source_hash = hashlib.sha256(source_norm.encode("utf-8")).hexdigest()
            candidate_texts = source_node_target_candidates(node)
            expected_target_hashes = {
                target_hash
                for expected_source_hash, target_hash in expected
                if expected_source_hash == source_hash
            }
            candidate_hashes = [
                hashlib.sha256(candidate.encode("utf-8")).hexdigest()
                for candidate in candidate_texts
            ]
            target_hash = next(
                (
                    candidate
                    for candidate in candidate_hashes
                    if candidate in expected_target_hashes
                ),
                candidate_hashes[0] if candidate_hashes else None,
            )
            if target_hash is not None:
                observed.append((source_hash, target_hash))
        checked["bilingual_source"] += max(len(expected), len(observed), 1)
        if observed != expected:
            failures.append(
                archive_model.item(
                    "bilingual_source", "source_target_pair_mismatch", resource, "pair_mismatch"
                )
            )
        for node in actual_nodes:
            if not source_node_attached(node):
                failures.append(
                    archive_model.item(
                        "bilingual_source", "source_node_misplaced", resource, "unattached"
                    )
                )


def element_children_lxml(node: etree._Element) -> list[etree._Element]:
    return [child for child in node if isinstance(child.tag, str)]


def resolve_path_lxml(root: etree._Element, path: tuple[int, ...]) -> etree._Element | None:
    current = root
    for index in path:
        children = element_children_lxml(current)
        if index < 0 or index >= len(children):
            return None
        current = children[index]
    return current


def element_path_lxml(root: etree._Element, target: etree._Element) -> tuple[int, ...] | None:
    """Return the element-only path used by the persisted schema4 locators."""
    if root is target:
        return ()
    parent = target.getparent()
    if parent is None:
        return None
    parent_path = element_path_lxml(root, parent)
    if parent_path is None:
        return None
    children = element_children_lxml(parent)
    try:
        return (*parent_path, children.index(target))
    except ValueError:
        return None


def diagnostic_codes(values: object) -> list[tuple[str, str]]:
    """Extract only stable, privacy-safe parser domain/type codes."""
    if not isinstance(values, list):
        return []
    result: list[tuple[str, str]] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        domain = re.sub(r"[^A-Za-z0-9_.-]", "_", str(value.get("domain", "")))[:64]
        kind = re.sub(r"[^A-Za-z0-9_.-]", "_", str(value.get("type", "")))[:64]
        result.append((domain, kind))
    return sorted(result)


def source_node_visible_text(node: etree._Element) -> str:
    """Match EPUB segment visibility while retaining ruby structure separately."""
    parts: list[str] = []

    def walk(current: etree._Element) -> None:
        name = current.tag.rsplit("}", 1)[-1].lower() if isinstance(current.tag, str) else ""
        if name in {"script", "style", "rt", "rp"}:
            return
        if current.text:
            parts.append(current.text)
        for child in current:
            if isinstance(child.tag, str):
                walk(child)
            if child.tail:
                parts.append(child.tail)

    walk(node)
    return norm_text("".join(parts))


def ruby_signatures(node: etree._Element) -> list[tuple[Any, ...]]:
    """Canonicalize ruby markup using the writer's sanitized-attribute contract."""
    result: list[tuple[Any, ...]] = []
    for ruby in node.iter():
        if not isinstance(ruby.tag, str) or archive_model.local_name(ruby.tag).lower() != "ruby":
            continue

        def signature(current: etree._Element) -> tuple[Any, ...]:
            attrs = tuple(
                sorted(
                    (key, value)
                    for key, value in current.attrib.items()
                    if key.rsplit("}", 1)[-1]
                    not in {"id", "name", "data-tn-id", "data-tn-inline-id", "data-tn-line"}
                )
            )
            children = tuple(signature(child) for child in current if isinstance(child.tag, str))
            tails = tuple(child.tail for child in current if isinstance(child.tag, str))
            return (current.tag, attrs, current.text, children, tails)

        result.append(signature(ruby))
    return result


def fragment_signature(markup: str) -> tuple[Any, ...]:
    soup = BeautifulSoup(f"<w>{markup}</w>", "xml")
    root = soup.find("w")
    if not isinstance(root, Tag):
        return ()

    def signature(node: Tag) -> tuple[Any, ...]:
        attrs = tuple(sorted((str(key), str(value)) for key, value in node.attrs.items()))
        children = tuple(
            signature(child) for child in node.find_all(recursive=False) if isinstance(child, Tag)
        )
        return (node.name, attrs, node.decode_contents(formatter="minimal"), children)

    return signature(root)


def source_subtree_signature(node: etree._Element, *, root: bool = True) -> tuple[Any, ...]:
    attrs = tuple(
        sorted((key, value) for key, value in node.attrib.items() if not (root and key == "class"))
    )
    children = tuple(
        (
            source_subtree_signature(child, root=False),
            child.tail,
        )
        for child in node
        if isinstance(child.tag, str)
    )
    return (archive_model.local_name(node.tag).lower(), attrs, node.text, children)
