"""Public EPUB markup parsing primitives."""

from __future__ import annotations

import io

from lxml import etree


def is_noteref(epub_type: str, role: str) -> bool:
    """仅识别明确声明的脚注引用语义。"""
    return "noteref" in epub_type.split() or "doc-noteref" in role.split()


def is_backlink(epub_type: str, role: str) -> bool:
    """仅识别已明确声明的脚注返回链接语义。"""
    return "backlink" in epub_type.split() or "doc-backlink" in role.split()


def resource_parser(data: bytes) -> tuple[etree._ElementTree, str, list[dict[str, object]]]:
    if len(data) > 512 * 1024 * 1024:
        raise ValueError("EPUB XHTML resource exceeds 512 MiB limit")
    diagnostics: list[dict[str, object]] = []
    strict = etree.XMLParser(
        no_network=True,
        recover=False,
        resolve_entities=False,
        remove_comments=False,
        remove_pis=False,
        strip_cdata=False,
    )
    try:
        return etree.fromstring(data, strict).getroottree(), "xml", diagnostics
    except etree.XMLSyntaxError as strict_error:
        first = strict_error.error_log[0] if strict_error.error_log else None
        if first is not None:
            diagnostics = [
                {
                    "level": first.level_name,
                    "domain": first.domain_name,
                    "type": first.type_name,
                    "line": first.line,
                    "column": first.column,
                }
            ]
        recovered = etree.HTMLParser(
            recover=True,
            no_network=True,
            remove_comments=False,
            remove_pis=False,
        )
        tree = etree.parse(io.BytesIO(data), recovered)
        root = tree.getroot()
        if root is None or root.find(".//body") is None:
            raise ValueError(
                "EPUB malformed XHTML recovery did not produce a document body"
            ) from strict_error
        return tree, "recovered", diagnostics[:20]


__all__ = ["is_backlink", "is_noteref", "resource_parser"]
