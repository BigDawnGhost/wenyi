"""Package Wenyi-rendered chapters as EPUB 3 without a general-purpose EPUB library.

This module owns container, package and navigation serialization only. Source parsing,
translation, chapter layout and resource discovery remain with the existing adapters.
"""

from __future__ import annotations

import posixpath
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import lxml.etree as etree
import lxml.html as html

from wenyi_core.atomicio import publish_target

_XHTML = "http://www.w3.org/1999/xhtml"
_OPF = "http://www.idpf.org/2007/opf"
_DC = "http://purl.org/dc/elements/1.1/"
_NCX = "http://www.daisy.org/z3986/2005/ncx/"
_CONTAINER = "urn:oasis:names:tc:opendocument:xmlns:container"
_PREFIXES = {
    "xml": "http://www.w3.org/XML/1998/namespace",
    "epub": "http://www.idpf.org/2007/ops",
    "xlink": "http://www.w3.org/1999/xlink",
}
_FOREIGN = {
    "svg": "http://www.w3.org/2000/svg",
    "math": "http://www.w3.org/1998/Math/MathML",
}


@dataclass(frozen=True)
class EpubChapter:
    """A rendered chapter, with a path relative to the package document."""

    href: str
    title: str
    content: str


@dataclass(frozen=True)
class EpubResource:
    """An already-discovered binary resource; no filesystem or network lookup occurs."""

    href: str
    media_type: str
    content: bytes
    cover_image: bool = False


@dataclass
class EpubTocEntry:
    """One navigation node shared by the NAV and NCX serializers."""

    href: str
    title: str
    children: list[EpubTocEntry] = field(default_factory=list)


def _xml(element: etree._Element) -> bytes:
    return etree.tostring(element, encoding="utf-8", xml_declaration=True)


def _child(
    parent: etree._Element, tag: str, text: str | None = None, **attrs: str
) -> etree._Element:
    namespace = etree.QName(parent).namespace
    element = etree.SubElement(parent, f"{{{namespace}}}{tag}", attrs)
    element.text = text
    return element


def _copy_markup(
    source: etree._Element, namespace: str, prefixes: dict[str, str]
) -> etree._Element:
    """Convert HTML nodes to namespace-correct XML without dropping heads or body text."""
    if isinstance(source, etree._Comment):
        result = etree.Comment(source.text or "")
    elif isinstance(source.tag, str):
        prefixes = {**prefixes, **{k: v for k, v in source.nsmap.items() if k}}
        prefixes.update(
            (name[6:], value) for name, value in source.attrib.items() if name.startswith("xmlns:")
        )
        if ":" in source.tag and not source.tag.startswith("{"):
            prefix, tag_name = source.tag.split(":", 1)
            if prefix not in prefixes:
                raise ValueError(f"Undeclared EPUB markup namespace: {prefix}")
            current_ns = prefixes[prefix]
        else:
            name = etree.QName(source)
            tag_name = name.localname
            current_ns = _FOREIGN.get(tag_name) or (
                namespace if name.namespace in {None, _XHTML} else name.namespace
            )
        nsmap = {None: current_ns, **{k: v for k, v in prefixes.items() if k != "xml"}}
        result = etree.Element(f"{{{current_ns}}}{tag_name}", nsmap=nsmap)
        for name, value in source.attrib.items():
            if name == "xmlns" or name.startswith("xmlns:"):
                continue
            if ":" in name and not name.startswith("{"):
                prefix, local_name = name.split(":", 1)
                if prefix not in prefixes:
                    raise ValueError(f"Undeclared EPUB markup namespace: {prefix}")
                name = f"{{{prefixes[prefix]}}}{local_name}"
            result.set(name, value)
        result.text = source.text
        child_ns = _XHTML if tag_name.lower() == "foreignobject" else current_ns
        for child in source:
            result.append(_copy_markup(child, child_ns, prefixes))
    else:
        raise ValueError("EPUB chapters must not contain unresolved XML entities")
    result.tail = source.tail
    return result


def _chapter_document(chapter: EpubChapter, lang: str) -> etree._Element:
    content = chapter.content.encode("utf-8")
    try:
        source = etree.fromstring(
            content, parser=etree.XMLParser(resolve_entities=False, no_network=True)
        )
    except etree.XMLSyntaxError:
        # HTML input may use named entities, void elements or raw script/style text.
        try:
            source = html.document_fromstring(
                content, parser=html.HTMLParser(encoding="utf-8", no_network=True)
            )
        except etree.ParserError as exc:
            raise ValueError(f"Cannot serialize EPUB chapter {chapter.href}: {exc}") from exc
    root = _copy_markup(source, _XHTML, _PREFIXES)
    if root.tag != f"{{{_XHTML}}}html":
        raise ValueError("EPUB chapters must be complete HTML documents")
    root.set("lang", lang)
    root.set(f"{{{_PREFIXES['xml']}}}lang", lang)
    head = root.find(f"{{{_XHTML}}}head")
    if head is None:
        head = etree.Element(f"{{{_XHTML}}}head")
        root.insert(0, head)
    for title in head.findall(f"{{{_XHTML}}}title"):
        head.remove(title)
    _child(head, "title", chapter.title)
    if root.find(f"{{{_XHTML}}}body") is None:
        raise ValueError("EPUB chapters must contain a body")
    return root


def _content_properties(root: etree._Element) -> str:
    properties: set[str] = set()
    for node in root.iter():
        if not isinstance(node.tag, str):
            continue
        name = etree.QName(node)
        if name.namespace == _FOREIGN["svg"]:
            properties.add("svg")
        if name.namespace == _FOREIGN["math"]:
            properties.add("mathml")
        if name.localname == "script" or any(key.startswith("on") for key in node.attrib):
            properties.add("scripted")
        if name.localname in {"img", "audio", "video", "source", "image", "link", "script"}:
            if any(
                value.startswith(("http://", "https://", "//"))
                for key, value in node.attrib.items()
                if key in {"src", "href", f"{{{_PREFIXES['xlink']}}}href", "poster"}
            ):
                properties.add("remote-resources")
    return " ".join(sorted(properties))


def _navigation(title: str, lang: str, toc: list[EpubTocEntry]) -> bytes:
    root = etree.Element(f"{{{_XHTML}}}html", nsmap={None: _XHTML, "epub": _PREFIXES["epub"]})
    root.set("lang", lang)
    root.set(f"{{{_PREFIXES['xml']}}}lang", lang)
    _child(_child(root, "head"), "title", title)
    nav = _child(_child(root, "body"), "nav", id="toc")
    nav.set(f"{{{_PREFIXES['epub']}}}type", "toc")
    _child(nav, "h1", title)

    def append(parent: etree._Element, entries: list[EpubTocEntry]) -> None:
        ordered = _child(parent, "ol")
        for entry in entries:
            item = _child(ordered, "li")
            _child(item, "a", entry.title, href=quote(entry.href, safe="/#"))
            if entry.children:
                append(item, entry.children)

    append(nav, toc)
    return _xml(root)


def _ncx(title: str, identifier: str, toc: list[EpubTocEntry]) -> bytes:
    root = etree.Element(f"{{{_NCX}}}ncx", nsmap={None: _NCX}, version="2005-1")
    head = _child(root, "head")
    _child(head, "meta", name="dtb:uid", content=identifier)
    depth = 1
    count = 0

    def append(parent: etree._Element, entries: list[EpubTocEntry], level: int) -> None:
        nonlocal count, depth
        depth = max(depth, level)
        for entry in entries:
            count += 1
            point = _child(parent, "navPoint", id=f"nav-{count}", playOrder=str(count))
            _child(_child(point, "navLabel"), "text", entry.title)
            _child(point, "content", src=quote(entry.href, safe="/#"))
            if entry.children:
                append(point, entry.children, level + 1)

    _child(_child(root, "docTitle"), "text", title)
    append(_child(root, "navMap"), toc, 1)
    for name, value in {"depth": str(depth), "totalPageCount": "0", "maxPageNumber": "0"}.items():
        _child(head, "meta", name=f"dtb:{name}", content=value)
    return _xml(root)


def _validate_paths(
    chapters: list[EpubChapter], resources: list[EpubResource], toc: list[EpubTocEntry]
) -> None:
    if not chapters:
        raise ValueError("Cannot generate an EPUB without chapters")
    seen = {"content.opf", "nav.xhtml", "toc.ncx", "cover.xhtml"}
    for item in [*chapters, *resources]:
        path = item.href
        if (
            not path
            or path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
            or any(char in path for char in ":?#")
        ):
            raise ValueError(f"Invalid EPUB resource path: {path!r}")
        if path in seen:
            raise ValueError(f"Duplicate EPUB resource path: {path}")
        seen.add(path)
    if sum(resource.cover_image for resource in resources) > 1:
        raise ValueError("An EPUB can have only one cover image")
    chapter_paths = {chapter.href for chapter in chapters}

    def validate(entries: list[EpubTocEntry]) -> None:
        for entry in entries:
            if entry.href.split("#", 1)[0] not in chapter_paths:
                raise ValueError(f"EPUB navigation refers to a missing chapter: {entry.href}")
            validate(entry.children)

    if not toc:
        raise ValueError("Cannot generate an EPUB without navigation entries")
    validate(toc)


def write_epub(
    out_path: str | Path,
    *,
    title: str,
    lang: str,
    chapters: list[EpubChapter],
    toc: list[EpubTocEntry],
    resources: list[EpubResource],
) -> None:
    """Atomically write an EPUB 3 package from explicit, already-rendered inputs."""
    _validate_paths(chapters, resources, toc)
    identifier = f"trans-novel-{title}"
    package = etree.Element(
        f"{{{_OPF}}}package",
        nsmap={None: _OPF},
        version="3.0",
        attrib={"unique-identifier": "book-id"},
    )
    metadata = etree.SubElement(package, f"{{{_OPF}}}metadata", nsmap={"dc": _DC})
    for name, value in {"identifier": identifier, "title": title, "language": lang}.items():
        item = etree.SubElement(metadata, f"{{{_DC}}}{name}")
        item.text = value
        if name == "identifier":
            item.set("id", "book-id")
    _child(
        metadata,
        "meta",
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        property="dcterms:modified",
    )
    manifest = _child(package, "manifest")
    spine = _child(package, "spine", toc="ncx")

    def manifest_item(item_id: str, href: str, media_type: str, properties: str = "") -> None:
        item = _child(
            manifest, "item", id=item_id, href=quote(href, safe="/"), **{"media-type": media_type}
        )
        if properties:
            item.set("properties", properties)

    manifest_item("nav", "nav.xhtml", "application/xhtml+xml", "nav")
    manifest_item("ncx", "toc.ncx", "application/x-dtbncx+xml")
    _child(spine, "itemref", idref="nav")
    container = etree.Element(f"{{{_CONTAINER}}}container", nsmap={None: _CONTAINER}, version="1.0")
    _child(
        _child(container, "rootfiles"),
        "rootfile",
        **{"full-path": "EPUB/content.opf", "media-type": "application/oebps-package+xml"},
    )
    with publish_target(out_path) as temporary:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
            archive.writestr("META-INF/container.xml", _xml(container))
            archive.writestr("EPUB/nav.xhtml", _navigation(title, lang, toc))
            archive.writestr("EPUB/toc.ncx", _ncx(title, identifier, toc))
            for index, chapter in enumerate(chapters):
                root = _chapter_document(chapter, lang)
                manifest_item(
                    f"chapter-{index}",
                    chapter.href,
                    "application/xhtml+xml",
                    _content_properties(root),
                )
                _child(spine, "itemref", idref=f"chapter-{index}")
                archive.writestr(posixpath.join("EPUB", chapter.href), _xml(root))
            for index, resource in enumerate(resources):
                item_id = f"resource-{index}"
                manifest_item(
                    item_id,
                    resource.href,
                    resource.media_type,
                    "cover-image" if resource.cover_image else "",
                )
                archive.writestr(posixpath.join("EPUB", resource.href), resource.content)
                if resource.cover_image:
                    _child(metadata, "meta", name="cover", content=item_id)
                    cover = EpubChapter(
                        "cover.xhtml",
                        title,
                        '<html><head></head><body><img alt="Cover"/></body></html>',
                    )
                    root = _chapter_document(cover, lang)
                    root.find(f".//{{{_XHTML}}}img").set("src", quote(resource.href, safe="/"))
                    manifest_item("cover", "cover.xhtml", "application/xhtml+xml")
                    archive.writestr("EPUB/cover.xhtml", _xml(root))
            archive.writestr("EPUB/content.opf", _xml(package))
