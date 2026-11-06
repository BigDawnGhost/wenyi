"""Static HTML translation regions, protected subtrees and inline range metadata.

Keep prose together across inline elements. Only block boundaries, explicit line
breaks and protected subtrees split a region; no browser or stylesheet evaluation
is involved. The existing annotation aligner locates inline ranges after translation.
"""

from __future__ import annotations

from bs4 import BeautifulSoup
from bs4.element import Comment, Doctype, NavigableString, PageElement, ProcessingInstruction, Tag

from wenyi_core.markup.annotations import _annotation_roots, _is_text_string
from wenyi_core.markup.contracts import (
    _ATOMIC_INLINE_TAGS,
    ANNOTATION_ID_ATTR,
    HTML_ATTRIBUTE_ID_ATTR,
    HTML_ATTRIBUTE_META_KEY,
    LINE_WRAPPER_ATTR,
)
from wenyi_core.models.document import Segment

_BLOCK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "body",
    "br",
    "button",
    "caption",
    "dd",
    "details",
    "dialog",
    "div",
    "dl",
    "dt",
    "fieldset",
    "figcaption",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "hgroup",
    "hr",
    "input",
    "label",
    "legend",
    "li",
    "main",
    "nav",
    "ol",
    "optgroup",
    "option",
    "p",
    "section",
    "select",
    "summary",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "ul",
}
_PROTECTED_TAGS = {
    "code",
    "head",
    "math",
    "mjx-container",
    "noscript",
    "pre",
    "rp",
    "rt",
    "script",
    "style",
    "svg",
    "template",
    "textarea",
    "title",
}
_PROTECTED_CLASSES = {
    "notranslate",
    "sr-only",
    "visually-hidden",
    "katex",
    "katex-display",
    "mathjax",
    "mathjax_display",
    "material-icons",
    "material-symbols-outlined",
    "material-symbols-rounded",
    "material-symbols-sharp",
}
_ATTRIBUTE_NAMES = frozenset(
    {
        "abbr",
        "alt",
        "aria-description",
        "aria-label",
        "aria-placeholder",
        "aria-roledescription",
        "aria-valuetext",
        "label",
        "placeholder",
        "title",
    }
)


def html_document_soup(html: str) -> BeautifulSoup:
    """Recover optional document shells without changing the fragment parser's policy.

    ``html.parser`` does not infer an omitted head, unlike a browser. Saved webpages
    commonly place global styles directly before an explicit body. Keep those nodes
    in the head rather than dropping them or treating metadata as body prose.
    """
    soup = BeautifulSoup(html, "html.parser")
    root = soup.html
    if root is None:
        root = soup.new_tag("html")
        nodes = list(soup.contents)
        content = [
            index
            for index, node in enumerate(nodes)
            if isinstance(node, Tag)
            or (
                _is_text_string(node)
                and not isinstance(node, (Doctype, ProcessingInstruction))
                and str(node).strip()
            )
        ]
        if content:
            nodes[content[0]].insert_before(root)
            for node in nodes[content[0] : content[-1] + 1]:
                if not isinstance(node, Doctype):
                    root.append(node.extract())
        else:
            soup.append(root)

    head = root.find("head", recursive=False)
    if head is None:
        head = soup.new_tag("head")
        root.insert(0, head)
    body = root.find("body", recursive=False)
    head_tags = {"base", "link", "meta", "noscript", "script", "style", "template", "title"}
    in_head = True
    before_body: list[PageElement] = []
    after_body: list[PageElement] = []
    body_seen = False
    for node in list(root.contents):
        if node is head:
            continue
        if node is body:
            in_head = False
            body_seen = True
            continue
        if in_head and (
            isinstance(node, Comment)
            or (isinstance(node, NavigableString) and not str(node).strip())
            or (isinstance(node, Tag) and node.name in head_tags)
        ):
            head.append(node.extract())
        else:
            in_head = False
            (after_body if body_seen else before_body).append(node)
    if body is None:
        body = soup.new_tag("body")
        root.append(body)
    for index, node in enumerate(before_body):
        body.insert(index, node.extract())
    for node in after_body:
        body.append(node.extract())
    return soup


def _inline_style(element: Tag) -> dict[str, str]:
    """Read literal inline declarations, without pretending to compute CSS."""
    style = element.get("style", "")
    if not isinstance(style, str):
        return {}
    declarations = {}
    for declaration in style.split(";"):
        name, separator, value = declaration.partition(":")
        if separator:
            declarations[name.strip().lower()] = value.split("!")[0].strip().lower()
    return declarations


def html_is_protected(element: Tag, *, for_attributes: bool = False) -> bool:
    """Honor explicit file-level opt-outs and inert/non-prose content."""
    style = _inline_style(element)
    return (
        (element.name in _PROTECTED_TAGS and not (for_attributes and element.name == "textarea"))
        or element.has_attr("hidden")
        or str(element.get("translate", "")).lower() == "no"
        or str(element.get("aria-hidden", "")).lower() == "true"
        or (
            element.has_attr("contenteditable")
            and str(element.get("contenteditable", "")).lower() != "false"
        )
        or bool(
            {str(value).lower() for value in element.get_attribute_list("class")}
            & _PROTECTED_CLASSES
        )
        or style.get("display") == "none"
        or style.get("visibility") in {"hidden", "collapse"}
        or style.get("content-visibility") == "hidden"
    )


def _is_block(element: Tag) -> bool:
    display = _inline_style(element).get("display")
    if display:
        return not (display.startswith("inline") or display.startswith("ruby"))
    return element.name in _BLOCK_TAGS


def html_visible_text(element: Tag) -> str:
    """Collect title text with the same subtree exclusions as body extraction."""
    if html_is_protected(element):
        return ""
    parts = []
    for child in element.children:
        if isinstance(child, Tag):
            parts.append(html_visible_text(child))
        elif _is_text_string(child):
            parts.append(str(child))
    return "".join(parts)


def html_translation_targets(soup: BeautifulSoup) -> list[Tag]:
    """Group consecutive inline prose without stranding a container's own text."""

    def boundary(element: Tag) -> bool:
        return (
            html_is_protected(element) or _is_block(element) or element.name in _ATOMIC_INLINE_TAGS
        )

    has_barrier: set[int] = set()
    for element in reversed(soup.find_all(True)):
        if any(
            isinstance(child, Tag)
            and not (element.name == "ruby" and child.name in {"rt", "rp"})
            and (boundary(child) or id(child) in has_barrier)
            for child in element.children
        ):
            has_barrier.add(id(element))

    targets: list[Tag] = []

    def walk(parent: Tag) -> None:
        run: list[PageElement] = []

        def flush() -> None:
            if not run:
                return
            has_text = any(
                child.get_text(strip=True) if isinstance(child, Tag) else str(child).strip()
                for child in run
                if isinstance(child, Tag) or _is_text_string(child)
            )
            if has_text:
                if parent is not soup and len(run) == len(parent.contents):
                    targets.append(parent)
                else:
                    wrapper = soup.new_tag("span")
                    wrapper[LINE_WRAPPER_ATTR] = "true"
                    run[0].insert_before(wrapper)
                    for child in run:
                        wrapper.append(child.extract())
                    targets.append(wrapper)
            run.clear()

        for child in list(parent.children):
            if isinstance(child, Tag) and (boundary(child) or id(child) in has_barrier):
                flush()
                if not html_is_protected(child) and child.name not in _ATOMIC_INLINE_TAGS | {"br"}:
                    walk(child)
            else:
                run.append(child)
        flush()

    walk(soup)
    return targets


def html_inline_roots(block: Tag, anchor: str, href: str) -> dict[int, dict[str, object]]:
    """Extend note metadata with semantic ranges for ordinary HTML inline shells."""
    roots = _annotation_roots(block, anchor, href)
    for node in block.find_all(True):
        if not node.get_text(strip=True) or node.name in {"ruby", "rt", "rp"}:
            continue
        if any(
            isinstance(parent, Tag)
            and (
                parent.name == "ruby"
                or roots.get(id(parent), {}).get("mode") == "point"
                or (
                    isinstance(marker_ids := roots.get(id(parent), {}).get("marker_node_ids"), set)
                    and id(node) in marker_ids
                )
            )
            for parent in node.parents
            if parent is not block
        ):
            continue
        record = roots.get(id(node))
        if record is None:
            annotation_id = f"{anchor}_html_{len(roots)}"
            node[ANNOTATION_ID_ATTR] = annotation_id
            record = {
                "id": annotation_id,
                "mode": "range",
                "marker_text": "",
                "raw_href": str(node.get("href", "")),
                "target_key": "",
                "relation": "html_inline",
                "root": node,
            }
            roots[id(node)] = record
        if record["relation"] in {"html_inline", "internal_link"}:
            record["html_tag"] = node.name

    roots = {
        id(node): roots[id(node)] for node in (block, *block.find_all(True)) if id(node) in roots
    }
    for record in roots.values():
        node = record["root"]
        if isinstance(node, Tag):
            record["html_parent"] = next(
                (
                    roots[id(parent)]["id"]
                    for parent in node.parents
                    if parent is not block and id(parent) in roots
                ),
                None,
            )
    return roots


def html_attribute_names(element: Tag) -> frozenset[str]:
    """Only human-readable attributes can be replaced, never identity or behavior."""
    names = _ATTRIBUTE_NAMES
    if element.name == "input" and str(element.get("type", "")).lower() in {
        "button",
        "reset",
        "submit",
    }:
        names |= {"value"}
    return names


def html_attribute_segments(
    soup: BeautifulSoup, resource_index: int, start_index: int
) -> list[Segment]:
    """Expose attribute values as plain segments with local-only DOM destinations."""
    segments = []
    for element in soup.find_all(True):
        if any(
            isinstance(parent, Tag) and html_is_protected(parent, for_attributes=parent is element)
            for parent in (element, *element.parents)
        ):
            continue
        for name in sorted(html_attribute_names(element)):
            value = element.get(name)
            if not isinstance(value, str) or not value.strip():
                continue
            node_id = element.get(HTML_ATTRIBUTE_ID_ATTR)
            if not isinstance(node_id, str):
                node_id = f"tn{resource_index}_attributes_{len(segments)}"
                element[HTML_ATTRIBUTE_ID_ATTR] = node_id
            segments.append(
                Segment(
                    index=start_index + len(segments),
                    anchor=f"tn{resource_index}_attribute_{len(segments)}",
                    source=value,
                    meta={HTML_ATTRIBUTE_META_KEY: {"node_id": node_id, "name": name}},
                )
            )
    return segments
