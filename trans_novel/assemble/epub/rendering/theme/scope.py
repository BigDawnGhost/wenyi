from __future__ import annotations

from lxml import etree

from trans_novel.assemble.epub.rendering.source_dom import parse_source_markup
from trans_novel.assemble.epub.rendering.theme.contracts import (
    LayoutBinding,
    ResourceThemeScope,
    ThemeError,
)
from trans_novel.assemble.epub.rendering.theme.projection import build_projection
from trans_novel.epub.layout import LayoutAssignment, source_node_digest


def identity_theme_scope(
    data: bytes,
    assignments: tuple[LayoutAssignment, ...],
    *,
    resource: str,
) -> ResourceThemeScope:
    if not assignments:
        return ResourceThemeScope()
    try:
        tree, _ = parse_source_markup(data)
        projection = build_projection(tree.getroot())
    except (ValueError, etree.LxmlError, ThemeError):
        raise ThemeError("theme_layout", "invalid_binding", resource=resource) from None
    eligible = {
        path: node
        for index, (node, path) in enumerate(zip(projection.nodes, projection.paths, strict=True))
        if projection.snapshot["nodes"][index]["isTextBlock"]
    }
    bindings: list[LayoutBinding] = []
    for assignment in assignments:
        node = eligible.get(assignment.path)
        if node is None:
            raise ThemeError("theme_layout", "invalid_binding", resource=resource)
        bindings.append(
            LayoutBinding(
                source_path=assignment.path,
                target_paths=(assignment.path,),
                source_sha256=source_node_digest(
                    node.tag.rsplit("}", 1)[-1].lower(),
                    dict(node.attrib),
                    "".join(node.itertext()),
                ),
            )
        )
    return ResourceThemeScope(layout_bindings=tuple(bindings))
