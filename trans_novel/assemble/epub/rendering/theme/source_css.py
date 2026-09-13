from __future__ import annotations

import zipfile
from collections.abc import Iterator, Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit

import soupsieve
import tinycss2
from bs4 import BeautifulSoup
from bs4.element import Tag
from lxml import etree

from trans_novel.assemble.epub.rendering.theme.cascade import (
    normalize_inline,
    source_specificity_bound,
)
from trans_novel.assemble.epub.rendering.theme.contracts import ThemeError
from trans_novel.epub.archive import ZipSafetyError, read_member, safe_name
from trans_novel.epub.navigation import resolve_epub_href

_IGNORED_TOKENS = {"whitespace", "comment"}


def _fail(resource: str, detail: str = "source_import_failed") -> ThemeError:
    return ThemeError("theme_css", detail, resource=resource)


def _nested_tokens(token: object) -> list[object] | None:
    nested = getattr(token, "arguments", None)
    if nested is None:
        nested = getattr(token, "content", None)
    return nested


def _has_error(tokens: list[object]) -> bool:
    for token in tokens:
        if getattr(token, "type", None) == "error":
            return True
        nested = _nested_tokens(token)
        if nested is not None and _has_error(nested):
            return True
    return False


def _has_layer(tokens: list[object]) -> bool:
    for token in tokens:
        if (
            getattr(token, "type", None) == "ident"
            and getattr(token, "lower_value", None) == "layer"
        ) or (
            getattr(token, "type", None) == "function"
            and getattr(token, "lower_name", None) == "layer"
        ):
            return True
        nested = _nested_tokens(token)
        if nested is not None and _has_layer(nested):
            return True
    return False


def _import_href(prelude: list[object], resource: str) -> str:
    if _has_error(prelude):
        raise ValueError
    tokens = [token for token in prelude if getattr(token, "type", None) not in _IGNORED_TOKENS]
    if not tokens:
        raise ValueError
    source, conditions = tokens[0], tokens[1:]
    token_type = getattr(source, "type", None)
    if token_type in {"string", "url"}:
        href = source.value
    elif token_type == "function" and getattr(source, "lower_name", None) == "url":
        arguments = [
            token
            for token in source.arguments
            if getattr(token, "type", None) not in _IGNORED_TOKENS
        ]
        if len(arguments) != 1 or getattr(arguments[0], "type", None) != "string":
            raise ValueError
        href = arguments[0].value
    else:
        raise ValueError
    if not href:
        raise ValueError
    if _has_layer(conditions):
        raise _fail(resource, "source_layers")
    return href


def _resolve_stylesheet(base_path: str, raw_href: str) -> str:
    if not raw_href:
        raise ValueError
    parsed = urlsplit(raw_href)
    resolved = resolve_epub_href(base_path, raw_href)
    if (
        parsed.query
        or parsed.fragment
        or resolved.external
        or not safe_name(resolved.resource_href)
    ):
        raise ValueError
    return resolved.resource_href


def _is_css(node: etree._Element) -> bool:
    media_type = node.get("type")
    return media_type is None or not media_type.strip() or media_type.strip().lower() == "text/css"


def collect_source_stylesheets(
    archive: zipfile.ZipFile,
    root: etree._Element,
    resource_href: str,
) -> dict[str, bytes]:
    """收集资源引用的归档内样式表，并返回移除顶层导入后的 UTF-8 副本。"""
    collected: dict[str, bytes] = {}
    complete: set[str] = set()
    active: set[str] = set()

    def collect_bytes(
        data: bytes,
        *,
        key: str,
        base_path: str,
        protocol_encoding: str | None = None,
        environment_encoding: Any = None,
    ) -> None:
        rules, encoding = tinycss2.parse_stylesheet_bytes(
            data,
            protocol_encoding=protocol_encoding,
            environment_encoding=environment_encoding,
            skip_comments=False,
            skip_whitespace=False,
        )
        retained: list[object] = []
        for rule in rules:
            if rule.type == "error":
                raise _fail(resource_href)
            if getattr(rule, "type", None) == "at-rule":
                keyword = getattr(rule, "lower_at_keyword", None)
                if keyword == "charset":
                    continue
                if keyword == "import":
                    collect_file(
                        _resolve_stylesheet(base_path, _import_href(rule.prelude, resource_href)),
                        environment_encoding=encoding,
                    )
                    continue
            retained.append(rule)
        collected[key] = tinycss2.serialize(retained).encode("utf-8")

    def collect_file(member: str, *, environment_encoding: Any = None) -> None:
        if member in active:
            raise _fail(resource_href, "source_import_cycle")
        if member in complete:
            return
        active.add(member)
        try:
            info = archive.getinfo(member)
            collect_bytes(
                read_member(archive, info),
                key=f"file:{member}",
                base_path=member,
                environment_encoding=environment_encoding,
            )
            complete.add(member)
        finally:
            active.remove(member)

    try:
        if not safe_name(resource_href):
            raise ValueError
        style_ordinal = 0
        for node in root.iter():
            if not isinstance(node.tag, str):
                continue
            local_name = node.tag.rsplit("}", 1)[-1].lower()
            if "{http://www.w3.org/XML/1998/namespace}base" in node.attrib or (
                local_name == "base" and node.get("href") is not None
            ):
                raise _fail(resource_href, "source_base_unsupported")
            if local_name == "style":
                ordinal = style_ordinal
                style_ordinal += 1
                if _is_css(node):
                    collect_bytes(
                        "".join(node.itertext()).encode("utf-8"),
                        key=f"inline:{ordinal}",
                        base_path=resource_href,
                        protocol_encoding="utf-8",
                    )
            elif (
                local_name == "link"
                and "stylesheet" in {token.lower() for token in (node.get("rel") or "").split()}
                and _is_css(node)
            ):
                collect_file(_resolve_stylesheet(resource_href, node.get("href") or ""))
    except ThemeError:
        raise
    except (
        KeyError,
        OSError,
        RecursionError,
        TypeError,
        UnicodeError,
        ValueError,
        zipfile.BadZipFile,
        ZipSafetyError,
    ):
        raise _fail(resource_href) from None
    return collected


def _evidence_rules(
    rules: list[object],
    *,
    media: tuple[str, ...] = (),
) -> Iterator[tuple[str, str, tuple[str, ...]]]:
    for rule in rules:
        if getattr(rule, "type", None) == "qualified-rule":
            yield (
                tinycss2.serialize(rule.prelude).strip(),
                tinycss2.serialize(rule.content).strip(),
                media,
            )
            continue
        if (
            getattr(rule, "type", None) == "at-rule"
            and getattr(rule, "lower_at_keyword", None) in {"media", "supports"}
            and getattr(rule, "content", None) is not None
        ):
            keyword = str(rule.lower_at_keyword)
            condition = tinycss2.serialize(rule.prelude).strip()
            nested = tinycss2.parse_rule_list(
                rule.content,
                skip_whitespace=True,
                skip_comments=True,
            )
            yield from _evidence_rules(
                nested,
                media=(*media, f"@{keyword} {condition}"),
            )


def _wrap_evidence(selector: str, declarations: str, media: tuple[str, ...]) -> str:
    value = f"{selector}{{{declarations}}}"
    for condition in reversed(media):
        value = f"{condition}{{{value}}}"
    return value


def source_style_evidence(
    root: etree._Element,
    nodes: Sequence[etree._Element],
    stylesheets: Mapping[str, bytes],
    *,
    resource: str,
) -> tuple[tuple[str, ...], ...]:
    """返回每个节点及其祖先匹配的原始 CSS 证据，并保留条件规则上下文。"""
    source_specificity_bound(stylesheets, resource=resource)
    xml_nodes = [node for node in root.iter() if isinstance(node.tag, str)]
    try:
        soup = BeautifulSoup(etree.tostring(root), "xml")
    except (TypeError, ValueError, etree.LxmlError):
        raise _fail(resource, "invalid_markup") from None
    soup_nodes = [node for node in soup.find_all(True) if isinstance(node, Tag)]
    if len(xml_nodes) != len(soup_nodes) or any(
        xml.tag.rsplit("}", 1)[-1].lower() != str(parsed.name).split(":")[-1].lower()
        for xml, parsed in zip(xml_nodes, soup_nodes, strict=True)
    ):
        raise _fail(resource, "invalid_markup")
    reverse = {id(parsed): xml for xml, parsed in zip(xml_nodes, soup_nodes, strict=True)}

    matched: list[tuple[frozenset[etree._Element], str]] = []
    for data in stylesheets.values():
        try:
            rules, _encoding = tinycss2.parse_stylesheet_bytes(
                data,
                skip_whitespace=True,
                skip_comments=True,
            )
            for selector, declarations, media in _evidence_rules(rules):
                selected = frozenset(
                    reverse[id(tag)] for tag in soup.select(selector) if id(tag) in reverse
                )
                if selected:
                    matched.append((selected, _wrap_evidence(selector, declarations, media)))
        except (
            RecursionError,
            soupsieve.SelectorSyntaxError,
            NotImplementedError,
            TypeError,
            ValueError,
        ):
            raise _fail(resource, "unsupported_source_selector") from None

    node_set = set(xml_nodes)
    output: list[tuple[str, ...]] = []
    for node in nodes:
        if node not in node_set:
            raise _fail(resource, "invalid_markup")
        ancestry = (node, *node.iterancestors())
        evidence = [
            value
            for selected, value in matched
            if any(ancestor in selected for ancestor in ancestry)
        ]
        for ancestor in reversed(ancestry):
            inline = ancestor.get("style")
            if inline is None:
                continue
            normalize_inline(inline, (), resource=resource)
            name = ancestor.tag.rsplit("}", 1)[-1].lower()
            evidence.append(f"@inline {name}{{{inline}}}")
        output.append(tuple(evidence))
    return tuple(output)
