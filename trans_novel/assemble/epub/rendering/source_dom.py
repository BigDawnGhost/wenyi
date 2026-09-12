"""Shared DOM and source-markup helpers for source EPUB rendering."""

from __future__ import annotations

import re

from lxml import etree

from trans_novel.assemble.epub.metadata import translated_toc_title
from trans_novel.assemble.epub.rendering.bilingual import (
    japanese_ruby_source_copy,
    sanitized_source_copy,
)
from trans_novel.epub.markup import resource_parser
from trans_novel.epub.navigation import nav_toc_roots_lxml

_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"


def effective_language(element: etree._Element, fallback: str | None = None) -> str | None:
    """返回节点继承后的源语言；显式空值保持为空。"""
    for node in (element, *element.iterancestors()):
        if _XML_LANG in node.attrib:
            return node.attrib[_XML_LANG]
        if "lang" in node.attrib:
            return node.attrib["lang"]
    return fallback


def indexed_toc_entries(
    entries: list[dict[str, object]], toc_path: str
) -> dict[int, dict[str, object]]:
    indexed: dict[int, dict[str, object]] = {}
    for entry in entries:
        if entry.get("toc_path") != toc_path:
            continue
        node_index = entry.get("node_index")
        if isinstance(node_index, int) and node_index >= 0:
            indexed[node_index] = entry
    return indexed


def parse_source_markup(
    data: bytes, expected_mode: str | None = None
) -> tuple[etree._ElementTree, str]:
    tree, mode, _diagnostics = resource_parser(data)
    if expected_mode and mode != expected_mode:
        raise ValueError(f"EPUB parse mode mismatch: expected {expected_mode}, got {mode}")
    return tree, mode


def element_children_lxml(element: etree._Element) -> list[etree._Element]:
    return [child for child in element if isinstance(child.tag, str)]


def resolve_element_path(root: etree._Element, path: tuple[int, ...]) -> etree._Element:
    current = root
    for index in path:
        children = element_children_lxml(current)
        if index < 0 or index >= len(children):
            raise ValueError("EPUB block/slot locator mismatch")
        current = children[index]
    return current


def serialize_source_tree(tree: etree._ElementTree, data: bytes, mode: str) -> bytes:
    probe = data
    for bom in (b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff"):
        if probe.startswith(bom):
            probe = probe[len(bom) :]
            break
    declaration = bool(re.match(rb"\s*<\?xml\b", probe))
    if mode == "recovered":
        return etree.tostring(tree, encoding="UTF-8", xml_declaration=declaration, method="xml")
    encoding = tree.docinfo.encoding or "UTF-8"
    return etree.tostring(
        tree,
        encoding=encoding,
        xml_declaration=declaration,
        doctype=tree.docinfo.doctype or None,
        method="xml",
    )


def rewrite_markup_languages(tree: etree._ElementTree | etree._Element, target_lang: str) -> None:
    xml_lang = "{http://www.w3.org/XML/1998/namespace}lang"
    root = tree.getroottree().getroot() if isinstance(tree, etree._Element) else tree.getroot()
    for key in ("lang", xml_lang):
        if key in root.attrib:
            root.attrib[key] = target_lang


def set_visible_label(node: etree._Element, title: str) -> None:
    node.text = title
    for descendant in node.iterdescendants():
        descendant.tail = None
        if isinstance(descendant.tag, str):
            descendant.text = None


def attr_local_lxml(element: etree._Element, name: str) -> str:
    for key, value in element.attrib.items():
        if key.rsplit("}", 1)[-1].split(":", 1)[-1] == name:
            return value
    return ""


def resolve_slot_owner(
    block: etree._Element, path: tuple[int, ...], field: str, source_value: str
) -> etree._Element:
    owner = resolve_element_path(block, path)
    if field != "tail" or owner.tail == source_value:
        return owner
    matches = [
        node for node in block.iter() if not isinstance(node.tag, str) and node.tail == source_value
    ]
    if len(matches) != 1:
        raise ValueError("EPUB slot locator mismatch")
    return matches[0]


def bilingual_source_copy(
    original: etree._Element, block: etree._Element, *, source_lang: str, source_tag: str
) -> etree._Element:
    copied = japanese_ruby_source_copy(original, source_lang, source_tag)
    return copied if copied is not None else sanitized_source_copy(original, source_tag)


def nav_labels(root: etree._Element) -> list[tuple[etree._Element, str]]:
    labels: list[tuple[etree._Element, str]] = []
    nav_roots = nav_toc_roots_lxml(root)

    def visit_li(li: etree._Element) -> None:
        label = next(
            (
                child
                for child in li
                if isinstance(child.tag, str)
                and child.tag.rsplit("}", 1)[-1].lower() in {"a", "span"}
            ),
            None,
        )
        if label is not None:
            labels.append((label, attr_local_lxml(label, "href")))
        child_ol = next(
            (
                child
                for child in li
                if isinstance(child.tag, str) and child.tag.rsplit("}", 1)[-1].lower() == "ol"
            ),
            None,
        )
        if child_ol is not None:
            for child in child_ol:
                if isinstance(child.tag, str) and child.tag.rsplit("}", 1)[-1].lower() == "li":
                    visit_li(child)

    for root_list in nav_roots:
        for li in root_list:
            if isinstance(li.tag, str) and li.tag.rsplit("}", 1)[-1].lower() == "li":
                visit_li(li)
    return labels


def rewrite_nav_labels(
    root: etree._Element,
    indexed: dict[int, dict[str, object]],
    toc_path: str,
    source_lang: str = "",
) -> None:
    for node_index, (label, raw_href) in enumerate(nav_labels(root)):
        entry = indexed.get(node_index)
        if entry is None:
            continue
        expected = entry.get("raw_href")
        if isinstance(expected, str) and expected != raw_href:
            raise ValueError(f"EPUB TOC href mismatch in {toc_path}")
        title = translated_toc_title(entry)
        if title:
            set_visible_label(label, title)
        if entry.get("preserve_source") is True and source_lang:
            label.set("lang", source_lang)
            label.set("{http://www.w3.org/XML/1998/namespace}lang", source_lang)


def toc_kind_at(toc_entries: list[dict[str, object]], name: str) -> str | None:
    for entry in toc_entries:
        if entry.get("toc_path") == name:
            kind = entry.get("kind")
            return kind if isinstance(kind, str) else None
    lowered = name.lower()
    if lowered.endswith(".ncx"):
        return "ncx"
    if lowered.endswith(("/nav.xhtml", "/nav.html", "nav.xhtml", "nav.html")):
        return "nav"
    return None
