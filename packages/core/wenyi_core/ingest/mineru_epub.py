"""Render MinerU Content List V2 and its local assets as a source EPUB."""

from __future__ import annotations

import io
import json
import logging
import re
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from html import escape, unescape
from pathlib import Path, PurePosixPath
from typing import Any
from unicodedata import name as unicode_name
from urllib.parse import quote, unquote, urlsplit
from xml.etree.ElementTree import Element, SubElement, tostring
from zipfile import ZipFile

from bs4 import BeautifulSoup, NavigableString, Tag
from latex2mathml.converter import Converter
from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin

from wenyi_core.assemble.epub_package import EpubChapter, EpubResource, EpubTocEntry, write_epub
from wenyi_core.models.errors import MinerUError

LOGGER = logging.getLogger(__name__)
XHTML = "http://www.w3.org/1999/xhtml"
EPUB = "http://www.idpf.org/2007/ops"
MATHML = "http://www.w3.org/1998/Math/MathML"
CHAPTER_HREF = "source.xhtml"
IMAGE_TYPES = {
    ".gif": "image/gif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
}
NUMERIC_ENTITY = re.compile(r"&#(?:[0-9]+|x[0-9a-f]+);", re.IGNORECASE)
INVALID_XML_CHAR = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
ALIGNMENT = re.compile(r"\\begin\s*\{\s*(?:aligned|align|split|gathered)\*?\s*\}")
EQUATION_NUMBER = re.compile(r"\\tag\s*\{([^{}]*)\}\s*$")
INLINE_TAGS = {"b", "br", "del", "em", "i", "s", "strong", "sub", "sup", "u"}
LEGACY_TAG = re.compile(r"</?(?:" + "|".join(sorted(INLINE_TAGS)) + r")\s*/?>", re.I)
INLINE_STYLE = {
    "bold": "strong",
    "italic": "em",
    "emphasis": "em",
    "strikethrough": "s",
    "underline": "u",
    "superscript": "sup",
    "subscript": "sub",
}
TABLE_TAGS = INLINE_TAGS | {
    "a",
    "caption",
    "code",
    "col",
    "colgroup",
    "img",
    "p",
    "pre",
    "span",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
}
PAGE_FURNITURE = {"page_header", "page_footer", "page_number", "page_aside_text", "aside_text"}
STYLES = """
img { max-width: 100%; height: auto; }
table { border-collapse: collapse; margin: 1em 0; }
th, td { border: 1px solid; padding: .35em; vertical-align: top; }
.algorithm { white-space: pre-wrap; }
.math.block { text-align: center; overflow: auto; margin: 1em 0; }
p.inline-math { text-align: left; text-align: start; }
caption p { margin: 0; }
.page-footnote, .media-note { font-size: .9em; }
.source-labels { list-style: none; }
"""


def _mathml(latex: str, *, display: str, crop: str = "") -> str:
    try:
        math = Converter(display=display).convert_to_element(latex)
        if ALIGNMENT.search(latex) and math.find(".//mtable") is None:
            raise ValueError("Unrendered alignment")
        # The converter stores symbols as character references. Decode only those
        # references before XML serialization so a literal ampersand stays escaped.
        for node in math.iter():
            if node.text:
                node.text = NUMERIC_ENTITY.sub(lambda match: unescape(match[0]), node.text)
                if node.tag == "mi" and re.match(r"\\[A-Za-z]", node.text):
                    raise ValueError("Unsupported LaTeX command")
    except Exception:
        fallback = "equation crop" if crop else "LaTeX text fallback"
        LOGGER.warning("Could not render a MinerU formula; using %s.", fallback)
        if crop:
            number = EQUATION_NUMBER.search(latex)
            return crop + (f" ({escape(number[1])})" if number else "")
        # Keep unrenderable inline formulas protected rather than sending them as prose.
        math = Element("math", {"xmlns": MATHML, "display": display})
        SubElement(math, "mtext").text = latex

    presentation = list(math)
    math[:] = []
    semantics = SubElement(math, "semantics")
    semantics.extend(presentation)
    SubElement(semantics, "annotation", {"encoding": "application/x-tex"}).text = latex
    return tostring(math, encoding="unicode")


def _image_path(src: str) -> PurePosixPath:
    url = urlsplit(src)
    path = PurePosixPath(unquote(url.path))
    if (
        url.scheme
        or url.netloc
        or url.query
        or url.fragment
        or path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or "\\" in str(path)
    ):
        raise ValueError(f"Image must be local to the MinerU export: {src!r}")
    return path


def _load_local_image(directory: Path, path: PurePosixPath) -> bytes:
    target = (directory / path).resolve()
    if not target.is_relative_to(directory):
        raise ValueError(f"Image must be local to the MinerU export: {str(path)!r}")
    return target.read_bytes()


def _collect_images(
    body: BeautifulSoup, read_image: Callable[[PurePosixPath], bytes], *, start: int
) -> list[EpubResource]:
    images: dict[PurePosixPath, EpubResource] = {}
    for image in body.select("img"):
        src = str(image.get("src", ""))
        path = _image_path(src)
        suffix = path.suffix.lower()
        if suffix not in IMAGE_TYPES:
            raise ValueError(f"Unsupported image format: {src!r}")
        if path not in images:
            try:
                data = read_image(path)
            except (FileNotFoundError, KeyError):
                raise ValueError(f"Missing image in the MinerU export: {src!r}") from None
            images[path] = EpubResource(
                f"images/image-{start + len(images) + 1}{suffix}", IMAGE_TYPES[suffix], data
            )
        image["src"] = images[path].href
    return list(images.values())


def _source_text(text: str) -> str:
    """Preserve legacy V2 inline formatting without treating prose as arbitrary HTML."""
    pieces: list[str] = []
    start = 0
    for match in LEGACY_TAG.finditer(text):
        pieces.extend((escape(unescape(text[start : match.start()])), match[0].lower()))
        start = match.end()
    pieces.append(escape(unescape(text[start:])))
    return "".join(pieces)


def _safe_href(url: str) -> str:
    if url.startswith("#") or urlsplit(url).scheme.lower() in {"http", "https", "mailto", "tel"}:
        return url
    return ""


def _inline(spans: list[dict[str, Any]], *, literal_text: bool = False) -> str:
    parts: list[str] = []
    render_text = escape if literal_text else _source_text
    for span in spans:
        kind, text = span["type"], span.get("content", "")
        if kind == "text":
            value = render_text(text)
            for style in span.get("styles", span.get("style", [])):
                if tag := INLINE_STYLE.get(style):
                    value = f"<{tag}>{value}</{tag}>"
        elif kind == "equation_inline":
            value = _mathml(text, display="inline")
        elif kind == "code_inline":
            value = f"<code>{escape(text)}</code>"
        elif kind == "hyperlink":
            children = span.get("children", text)
            value = (
                _inline(children, literal_text=literal_text)
                if isinstance(children, list)
                else render_text(children)
            )
            if url := _safe_href(span.get("url", "")):
                value = f'<a href="{escape(url)}">{value}</a>'
        else:
            raise ValueError(f"Unsupported inline content type: {kind!r}")
        parts.append(value)
    return "".join(parts)


def _table_html(html: str) -> Tag | None:
    """Keep static table structure and render formulas from legacy HTML cells."""
    # Legacy <eq> bodies can contain bare comparison operators; escape them
    # before parsing the surrounding HTML.
    html = re.sub(
        r"<eq>(.*?)</eq>", lambda m: f"<eq>{escape(unescape(m[1]))}</eq>", html, flags=re.S
    )
    block = BeautifulSoup(html, "html.parser")
    table = block.find("table")
    if table is None or not table.find("tr") or not table.find(["td", "th"]):
        return None
    for active in table.select("script,style,iframe,object,embed,template"):
        active.decompose()
    for tag in [table, *table.find_all()]:
        name = tag.name
        if name not in TABLE_TAGS and name != "eq":
            tag.unwrap()
            continue
        keep = {
            "a": ("href",),
            "img": ("alt", "src"),
            "td": ("rowspan", "colspan"),
            "th": ("rowspan", "colspan", "scope"),
            "col": ("span",),
            "colgroup": ("span",),
        }.get(name, ())
        tag.attrs = {key: tag[key] for key in keep if key in tag.attrs}
        if name == "a" and not _safe_href(_attribute(tag, "href")):
            tag.unwrap()
    for equation in table.find_all("eq"):
        equation.replace_with(
            BeautifulSoup(_mathml(equation.get_text(), display="inline"), "html.parser")
        )
    cell_math = MarkdownIt("zero").enable("escape")
    cell_math.use(
        dollarmath_plugin,
        allow_labels=False,
        renderer=lambda text, _: _mathml(text, display="inline"),
    )
    for text in table.find_all(string=True):
        if (
            type(text) is NavigableString
            and "$" in text
            and not text.find_parent(["code", "pre", "math"])
        ):
            rendered = BeautifulSoup(cell_math.renderInline(str(text)), "html.parser")
            text.replace_with(*rendered.contents)
    return table


def _fragment(markup: str) -> BeautifulSoup:
    return BeautifulSoup(markup, "html.parser")


def _attribute(tag: Tag, key: str) -> str:
    value = tag.get(key)
    return value if isinstance(value, str) else ""


def _image(content: dict[str, Any], *, alt: str) -> str:
    path = (content.get("image_source") or {}).get("path")
    return f'<img src="{escape(path)}" alt="{escape(alt)}"/>' if path else ""


def _visual(kind: str, content: dict[str, Any]) -> str:
    caption = _inline(content.get(f"{kind}_caption", []))
    notes = _inline(content.get(f"{kind}_footnote", []))
    alt = _fragment(caption).get_text(" ", strip=True)
    raw_table = content.get("html", "") if kind == "table" else content.get("content", "")
    table = _table_html(raw_table) if raw_table and kind in {"chart", "table"} else None
    if kind == "table" and table is not None:
        if caption and not table.find("caption", recursive=False):
            node = _fragment(f"<caption>{caption}</caption>").caption
            assert node is not None, f"caption fragment lost its <caption> node: {caption!r}"
            table.insert(0, node)
        else:
            # Do not discard either caption if both are independently supplied.
            notes = " ".join(filter(None, (caption, notes)))
        # Extract the caption through existing EPUB paragraph anchors rather than
        # changing extraction identities in already initialized EPUB projects.
        caption_node = table.find("caption", recursive=False)
        if caption_node is not None and not caption_node.find(["p", "div"]):
            paragraph = _fragment("").new_tag("p")
            for child in list(caption_node.contents):
                paragraph.append(child.extract())
            caption_node.append(paragraph)
        return str(table) + (f'<p class="media-note">{notes}</p>' if notes else "")
    image = _image(content, alt=alt)
    details = str(table) if table is not None else ""
    if raw_table and table is None:
        details = f"<pre>{escape(raw_table)}</pre>"
    if not (image or details or caption or notes):
        raise ValueError(f"{kind} has neither readable content nor a local image")
    description = f"<figcaption>{caption}</figcaption>" if caption else ""
    return f"<figure>{image}{details}{description}</figure>" + (
        f'<p class="media-note">{notes}</p>' if notes else ""
    )


def _render_block(kind: str, content: dict[str, Any]) -> str:
    if kind == "title":
        level = content["level"]
        if type(level) is not int or level < 1:
            raise ValueError("Title level must be a positive integer")
        tag = f"h{min(level, 6)}"
        return f"<{tag}>{_inline(content['title_content'])}</{tag}>"
    if kind in ("image", "chart", "table"):
        return _visual(kind, content)
    if kind == "paragraph":
        return f"<p>{_inline(content['paragraph_content'])}</p>"
    if kind == "page_footnote":
        text = _inline(content["page_footnote_content"])
        return f'<aside epub:type="footnote" class="page-footnote"><p>{text}</p></aside>'
    if kind == "equation_interline":
        latex = content.get("math_content", "")
        crop = _image(content, alt=latex)
        if latex.strip() and content.get("math_type", "latex") == "latex":
            formula = _mathml(latex, display="block", crop=crop)
        else:
            formula = crop or escape(latex)
        if not formula:
            raise ValueError("Display equation has neither LaTeX nor a local image")
        return f'<div class="math block">{formula}</div>'
    if kind in {"list", "index"}:
        items: list[str] = []
        for item in content["list_items"]:
            prefix = item.get("prefix", "").strip()
            text = _inline(item["item_content"])
            if kind == "index" and (anchor := item.get("anchor")):
                text = f'<a href="#{quote(anchor, safe="")}">{text}</a>'
            items.append(f"<li>{escape(prefix) + ' ' if prefix else ''}{text}</li>")
        list_class = (
            ' class="source-labels"'
            if content.get("list_type") == "reference_list"
            or any(item.get("prefix") for item in content["list_items"])
            else ""
        )
        return f"<ul{list_class}>{''.join(items)}</ul>"
    if kind in {"code", "algorithm"}:
        spans = content[f"{kind}_content"]
        # Some producers supply a plain string, VLM supplies ordered inline
        # spans. Never interpret a program's text as markup or invent newlines.
        listing = (
            escape(spans) if isinstance(spans, str) else _inline(spans, literal_text=kind == "code")
        )
        if kind == "code":
            listing = f"<pre><code>{listing}</code></pre>"
        else:
            listing = f'<p class="algorithm">{listing}</p>'
        caption = _inline(content.get(f"{kind}_caption", []))
        if caption:
            return f"<figure>{listing}<figcaption>{caption}</figcaption></figure>"
        return listing
    raise ValueError(f"Unsupported Content List V2 block type: {kind!r}")


def _render_document(pages: list[list[dict[str, Any]]]) -> BeautifulSoup:
    parts: list[str] = []
    for page_index, page in enumerate(pages, 1):
        for block_index, block in enumerate(page, 1):
            where = f"page {page_index}, block {block_index}"
            try:
                kind = block["type"]
                if kind in PAGE_FURNITURE:
                    continue
                rendered = _fragment(_render_block(kind, block["content"]))
                anchor = block.get("anchor") or block["content"].get("anchor")
                if not anchor and kind == "page_footnote":
                    anchor = f"footnote-p{page_index}-{block_index}"
                if anchor:
                    node = rendered.find(True)
                    if node is None:
                        raise ValueError("An anchored block must render an element")
                    node["id"] = anchor
                parts.append(rendered.decode())
            except (AttributeError, KeyError, TypeError, ValueError) as error:
                raise ValueError(f"MinerU {where}: {error}") from error
    body = _fragment("".join(parts))
    anchored = body.select("[id]")
    ids = Counter(_attribute(node, "id") for node in anchored)
    notes = {_attribute(node, "id") for node in anchored if node.get("epub:type") == "footnote"}
    for link in body.select('a[href^="#"]'):
        target = unquote(_attribute(link, "href")[1:])
        if ids[target] != 1:
            link.unwrap()
        elif target in notes:
            link["epub:type"] = "noteref"
    return body


def _math_prose_tokens(parent: Tag) -> Iterator[NavigableString | bool]:
    """Yield text, inline formula boundaries (True) and other boundaries (False)."""
    for node in parent.children:
        if type(node) is NavigableString and node:
            yield node
        elif isinstance(node, Tag):
            if node.name == "math":
                yield node.get("display") == "inline"
            elif node.name in {"br", "code", "img", "pre", "sub", "sup"}:
                yield False
            else:
                yield from _math_prose_tokens(node)


def _needs_math_gap(char: str) -> bool:
    return char.isdecimal() or (char.isalpha() and unicode_name(char, "").startswith("LATIN "))


def _format_math_paragraphs(body: Tag) -> None:
    """Separate word characters from neighboring MathML without changing LaTeX."""
    for paragraph in body.select("p"):
        if paragraph.find("math", attrs={"display": "inline"}) is None:
            continue
        tokens = [False, *_math_prose_tokens(paragraph), False]
        if not any(isinstance(token, NavigableString) and token.strip() for token in tokens):
            continue
        value = paragraph.get("class")
        classes = value.split() if isinstance(value, str) else list(value or ())
        if "inline-math" not in classes:
            paragraph["class"] = " ".join([*classes, "inline-math"])
        for index, token in enumerate(tokens[1:-1], 1):
            if not isinstance(token, NavigableString):
                continue
            before = tokens[index - 1] is True and _needs_math_gap(token[0])
            after = tokens[index + 1] is True and _needs_math_gap(token[-1])
            if before or after:
                token.replace_with((" " if before else "") + token + (" " if after else ""))


def _build_toc(body: Tag) -> list[EpubTocEntry]:
    toc: list[EpubTocEntry] = []
    stack: list[tuple[int, EpubTocEntry]] = []
    used_ids = {_attribute(tag, "id") for tag in body.select("[id]")}
    for heading in body.select("h1, h2, h3, h4, h5, h6"):
        title = " ".join(
            text.strip()
            for text in heading.find_all(string=True)
            if text.strip() and not text.find_parent("annotation")
        )
        if not title:
            continue
        anchor = _attribute(heading, "id")
        if not anchor:
            index = len(used_ids) + 1
            while (anchor := f"section-{index}") in used_ids:
                index += 1
            used_ids.add(anchor)
            heading["id"] = anchor
        level = int(heading.name[1])
        entry = EpubTocEntry(f"{CHAPTER_HREF}#{anchor}", title)
        while stack and stack[-1][0] >= level:
            stack.pop()
        (stack[-1][1].children if stack else toc).append(entry)
        stack.append((level, entry))
    return toc


def _bundle_data(archive: ZipFile) -> tuple[bytes, Callable[[PurePosixPath], bytes]]:
    members = [name for name in archive.namelist() if name.endswith("_content_list_v2.json")]
    if len(members) != 1:
        raise ValueError("Expected one *_content_list_v2.json in the MinerU ZIP.")
    member = PurePosixPath(members[0])
    return archive.read(members[0]), lambda path: archive.read(str(member.parent / path))


def _write_source_epub(
    chunks: list[tuple[bytes, Callable[[PurePosixPath], bytes]]],
    output: Path,
    *,
    title: str | None,
    lang: str,
) -> None:
    body = _fragment("")
    resources: list[EpubResource] = []
    for index, (data, read_image) in enumerate(chunks, 1):
        pages = json.loads(data)
        if not isinstance(pages, list) or not all(isinstance(page, list) for page in pages):
            raise ValueError("Expected Content List V2: a list of pages containing block arrays.")
        fragment = _render_document(pages)
        if len(chunks) > 1:
            # Separately parsed chunks can contain the same local IDs and image paths.
            prefix = f"chunk-{index}-"
            for node in fragment.select("[id]"):
                node["id"] = prefix + _attribute(node, "id")
            for node in fragment.select('a[href^="#"]'):
                node["href"] = "#" + prefix + _attribute(node, "href")[1:]
        resources.extend(_collect_images(fragment, read_image, start=len(resources)))
        for node in list(fragment.contents):
            body.append(node.extract())
    _format_math_paragraphs(body)
    if not body.find(True):
        raise ValueError("Content List V2 did not contain readable blocks.")
    if not body.get_text(strip=True):
        raise MinerUError(
            "MinerU extracted no text from the PDF: the pages look like scanned images "
            "without a text layer. Check the scan quality, or add a searchable text layer "
            "with OCR, then retry."
        )
    source_markup, replaced = INVALID_XML_CHAR.subn("\N{REPLACEMENT CHARACTER}", body.decode())
    if replaced:
        LOGGER.warning("Replaced %d characters that XHTML cannot represent.", replaced)
        body = _fragment(source_markup)
    toc = _build_toc(body)
    title = title or (toc[0].title if toc else "MinerU document")
    title = INVALID_XML_CHAR.sub("\N{REPLACEMENT CHARACTER}", title)
    xhtml = (
        f'<html xmlns="{XHTML}" xmlns:epub="{EPUB}"><head><title>{escape(title)}</title>'
        f"<style>{STYLES}</style></head><body>{body.decode()}</body></html>"
    )
    write_epub(
        output,
        title=title,
        lang=lang,
        chapters=[EpubChapter(CHAPTER_HREF, title, xhtml)],
        toc=toc or [EpubTocEntry(CHAPTER_HREF, title)],
        resources=resources,
        split_nested_toc=True,
    )


def render_mineru_results(results: list[bytes], output: Path, *, title: str, lang: str) -> None:
    """Render result ZIPs in PDF chunk order without extracting their files."""
    with ExitStack() as stack:
        archives = [stack.enter_context(ZipFile(io.BytesIO(result))) for result in results]
        _write_source_epub(
            [_bundle_data(archive) for archive in archives], output, title=title, lang=lang
        )


def convert_json(source: Path, output: Path, *, title: str | None = None, lang: str = "en") -> None:
    """Render only Content List V2 and its local images, not a Markdown or HTML export."""
    if source.resolve() == output.resolve():
        raise ValueError("The EPUB output must not overwrite the MinerU source.")
    if output.suffix.lower() != ".epub":
        raise ValueError("Output must have an .epub extension.")
    if source.suffix.lower() == ".zip":
        with ZipFile(source) as archive:
            _write_source_epub([_bundle_data(archive)], output, title=title, lang=lang)
    else:
        directory = source.parent.resolve()
        _write_source_epub(
            [(source.read_bytes(), lambda path: _load_local_image(directory, path))],
            output,
            title=title,
            lang=lang,
        )
