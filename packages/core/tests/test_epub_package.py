"""Validate generated EPUBs independently of Wenyi's permissive EPUB reader."""

from __future__ import annotations

import importlib.metadata
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET
from zipfile import ZIP_STORED, ZipFile

import pytest
from wenyi_core.assemble.epub_package import (
    EpubChapter,
    EpubResource,
    EpubTocEntry,
    write_epub,
)
from wenyi_core.assemble.writer import assemble
from wenyi_core.ingest.segmenter import load_document
from wenyi_core.storage.file import FileStorage

NS = {
    "container": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
    "html": "http://www.w3.org/1999/xhtml",
    "ncx": "http://www.daisy.org/z3986/2005/ncx/",
    "svg": "http://www.w3.org/2000/svg",
    "math": "http://www.w3.org/1998/Math/MathML",
}
CONTENT = "<html><head><title>Old</title></head><body><p>中文 &amp; text.</p></body></html>"


def _find(element: ET.Element, path: str) -> ET.Element:
    result = element.find(path, NS)
    assert result is not None, f"Missing XML element: {path}"
    return result


def _write(path: Path, **overrides) -> Path:
    options = {
        "title": '书名 <A> & "B"',
        "lang": "zh-Hans",
        "chapters": [EpubChapter("ch0.xhtml", "章节 & <1>", CONTENT)],
        "toc": [EpubTocEntry("ch0.xhtml", "章节 & <1>")],
        "resources": [],
    }
    write_epub(path, **{**options, **overrides})
    return path


def test_core_distribution_does_not_require_ebooklib():
    requirements = importlib.metadata.requires("wenyi-core") or []
    assert not any(requirement.lower().startswith("ebooklib") for requirement in requirements)


def test_container_metadata_manifest_and_spine(tmp_path):
    output = _write(tmp_path / "book.epub")
    with ZipFile(output) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names))
        mimetype = archive.infolist()[0]
        assert mimetype.filename == "mimetype"
        assert mimetype.header_offset == 0
        assert mimetype.compress_type == ZIP_STORED
        assert mimetype.extra == b""
        assert archive.read("mimetype") == b"application/epub+zip"
        assert archive.testzip() is None
        container = ET.fromstring(archive.read("META-INF/container.xml"))
        rootfile = container.find("container:rootfiles/container:rootfile", NS)
        assert rootfile is not None
        assert rootfile.get("full-path") == "EPUB/content.opf"
        package = ET.fromstring(archive.read(rootfile.attrib["full-path"]))
        assert package.tag == f"{{{NS['opf']}}}package"
        assert package.get("version") == "3.0"
        identifier = package.find("opf:metadata/dc:identifier", NS)
        assert identifier is not None
        assert identifier.get("id") == package.get("unique-identifier")
        assert package.findtext("opf:metadata/dc:title", namespaces=NS) == '书名 <A> & "B"'
        assert package.findtext("opf:metadata/dc:language", namespaces=NS) == "zh-Hans"
        modified = package.find("opf:metadata/opf:meta[@property='dcterms:modified']", NS)
        assert modified is not None
        assert modified.text is not None
        assert modified.text.endswith("Z")
        items = {item.attrib["id"]: item for item in package.findall("opf:manifest/opf:item", NS)}
        assert len(items) == 3
        for item in items.values():
            assert "EPUB/" + unquote(item.attrib["href"]) in names
        assert items["nav"].get("properties") == "nav"
        spine = package.find("opf:spine", NS)
        assert spine is not None
        assert spine.get("toc") == "ncx"
        assert [item.attrib["idref"] for item in spine] == ["nav", "chapter-0"]
        for item in spine:
            assert item.attrib["idref"] in items
        chapter = ET.fromstring(archive.read("EPUB/ch0.xhtml"))
        assert chapter.get("lang") == "zh-Hans"
        assert chapter.get("{http://www.w3.org/XML/1998/namespace}lang") == "zh-Hans"
        assert chapter.findtext("html:head/html:title", namespaces=NS) == "章节 & <1>"
        assert chapter.findtext("html:body/html:p", namespaces=NS) == "中文 & text."


def test_nav_and_ncx_share_hierarchy_and_escaped_labels(tmp_path):
    chapters = [EpubChapter(f"ch{i}.xhtml", f"Chapter {i}", CONTENT) for i in range(3)]
    toc = [
        EpubTocEntry("ch0.xhtml", "Part <1>", [EpubTocEntry("ch1.xhtml", "Child & two")]),
        EpubTocEntry("ch2.xhtml", "Part 2"),
    ]
    output = _write(tmp_path / "book.epub", chapters=chapters, toc=toc)
    with ZipFile(output) as archive:
        nav = ET.fromstring(archive.read("EPUB/nav.xhtml"))
        ncx = ET.fromstring(archive.read("EPUB/toc.ncx"))
    nav_nodes = nav.findall(".//html:li/html:a", NS)
    ncx_nodes = ncx.findall(".//ncx:navPoint/ncx:content", NS)
    assert [node.get("href") for node in nav_nodes] == [node.get("src") for node in ncx_nodes]
    assert [node.text for node in nav_nodes] == ["Part <1>", "Child & two", "Part 2"]
    assert _find(nav, ".//html:li/html:ol/html:li/html:a").get("href") == "ch1.xhtml"
    assert _find(ncx, ".//ncx:navPoint/ncx:navPoint/ncx:content").get("src") == "ch1.xhtml"
    assert [node.get("playOrder") for node in ncx.findall(".//ncx:navPoint", NS)] == ["1", "2", "3"]
    assert _find(ncx, "ncx:head/ncx:meta[@name='dtb:depth']").get("content") == "2"
    assert all(ordered.find("html:li", NS) is not None for ordered in nav.findall(".//html:ol", NS))


def test_html_normalization_keeps_heads_entities_comments_and_text_tails(tmp_path):
    content = (
        "<html><head><style>p::before { content: '<'; }</style></head>"
        "<body>Leading &nbsp;<p>Copyright &copy; &amp; text<br>next</p>"
        "<!-- keep -->Trailing<script>if (1 < 2) { window.x = '&'; }</script></body></html>"
    )
    output = _write(tmp_path / "book.epub", chapters=[EpubChapter("ch0.xhtml", "Title", content)])
    with ZipFile(output) as archive:
        data = archive.read("EPUB/ch0.xhtml")
        root = ET.fromstring(data)
        package = ET.fromstring(archive.read("EPUB/content.opf"))
    assert root.findtext("html:head/html:style", namespaces=NS) == "p::before { content: '<'; }"
    body = _find(root, "html:body")
    assert body.text == "Leading \xa0"
    assert "".join(body.itertext()).startswith("Leading \xa0Copyright © & textnextTrailing")
    assert root.findtext(".//html:script", namespaces=NS) == "if (1 < 2) { window.x = '&'; }"
    assert b"<!-- keep -->Trailing" in data
    item = _find(package, "opf:manifest/opf:item[@id='chapter-0']")
    assert item.get("properties") == "scripted"


def test_foreign_content_namespaces_and_manifest_properties(tmp_path):
    content = (
        '<html xmlns="http://www.w3.org/1999/xhtml" '
        'xmlns:epub="http://www.idpf.org/2007/ops">'
        '<head/><body><p epub:type="footnote">Note</p>'
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
        '<rect width="10" height="10"/></svg>'
        '<math xmlns="http://www.w3.org/1998/Math/MathML"><mi>x</mi></math>'
        '<img src="https://example.invalid/image.png" alt="Remote"/>'
        "</body></html>"
    )
    output = _write(tmp_path / "book.epub", chapters=[EpubChapter("ch0.xhtml", "Title", content)])
    with ZipFile(output) as archive:
        chapter = ET.fromstring(archive.read("EPUB/ch0.xhtml"))
        package = ET.fromstring(archive.read("EPUB/content.opf"))
    assert _find(chapter, ".//svg:svg").get("viewBox") == "0 0 10 10"
    assert chapter.find(".//svg:rect", NS) is not None
    assert chapter.findtext(".//math:mi", namespaces=NS) == "x"
    assert _find(chapter, ".//html:p").get("{http://www.idpf.org/2007/ops}type") == "footnote"
    item = _find(package, "opf:manifest/opf:item[@id='chapter-0']")
    assert set(item.attrib["properties"].split()) == {"svg", "mathml", "remote-resources"}


@pytest.mark.parametrize("bilingual", [False, True])
@pytest.mark.parametrize("preserve_source_style", [False, True])
def test_html_export_preserves_head_styles_and_bilingual_presentation(
    tmp_path, bilingual, preserve_source_style
):
    source = tmp_path / "sample.html"
    source.write_text(
        "<html><head><title>Sample</title>"
        '<link rel="stylesheet" href="unbundled.css"/><script src="unbundled.js"></script>'
        "<style>.body-text { font-weight: bold; }</style></head>"
        '<body><h1>Chapter</h1><p class="body-text">Original text.</p></body></html>',
        encoding="utf-8",
    )
    document = load_document(str(source), "en", "zh")
    for chapter in document.chapters:
        for segment in chapter.segments:
            segment.target = "翻译章节" if segment.kind == "heading" else "翻译正文。"
    store = FileStorage(str(tmp_path / "state"))
    store.init_from_document(document)
    output = assemble(
        store,
        str(source),
        out_format="epub",
        bilingual=bilingual,
        preserve_source_style=preserve_source_style,
        about_page=False,
    )
    with ZipFile(output) as archive:
        chapter = ET.fromstring(archive.read("EPUB/ch0.xhtml"))
    head = _find(chapter, "html:head")
    assert head.findtext("html:style", namespaces=NS) == ".body-text { font-weight: bold; }"
    assert head.find("html:link", NS) is None
    assert head.find("html:script", NS) is None
    bilingual_style = head.find("html:style[@id='tn-bilingual-style']", NS)
    assert (bilingual_style is not None) == (bilingual and not preserve_source_style)
    paragraphs = chapter.findall(".//html:p", NS)
    assert any(paragraph.text == "翻译正文。" for paragraph in paragraphs)
    assert any(paragraph.text == "Original text." for paragraph in paragraphs) == bilingual


def test_resources_cover_and_uri_encoding(tmp_path):
    output = _write(
        tmp_path / "book.epub",
        chapters=[EpubChapter("章 one.xhtml", "Title", CONTENT)],
        toc=[EpubTocEntry("章 one.xhtml", "Title")],
        resources=[
            EpubResource("images/封面 one.jpg", "image/jpeg", b"cover", cover_image=True),
            EpubResource("images/inside.png", "image/png", b"inside"),
        ],
    )
    with ZipFile(output) as archive:
        assert archive.read("EPUB/images/封面 one.jpg") == b"cover"
        assert archive.read("EPUB/images/inside.png") == b"inside"
        package = ET.fromstring(archive.read("EPUB/content.opf"))
        cover = package.find("opf:manifest/opf:item[@properties='cover-image']", NS)
        assert cover is not None
        assert unquote(cover.attrib["href"]) == "images/封面 one.jpg"
        assert " " not in cover.attrib["href"]
        legacy = _find(package, "opf:metadata/opf:meta[@name='cover']")
        assert legacy.get("content") == cover.get("id")
        cover_page = ET.fromstring(archive.read("EPUB/cover.xhtml"))
        assert _find(cover_page, ".//html:img").get("src") == cover.get("href")
        nav = ET.fromstring(archive.read("EPUB/nav.xhtml"))
        assert unquote(_find(nav, ".//html:a").attrib["href"]) == "章 one.xhtml"


@pytest.mark.parametrize(
    "href", ["", "../escape", "/absolute", "a/../b", "a//b", r"a\b", "a#b", "a?b", "C:drive"]
)
def test_invalid_resource_paths_do_not_touch_existing_export(tmp_path, href):
    output = tmp_path / "book.epub"
    output.write_bytes(b"previous export")
    with pytest.raises(ValueError, match="Invalid EPUB resource path"):
        _write(output, resources=[EpubResource(href, "image/png", b"image")])
    assert output.read_bytes() == b"previous export"
    assert list(tmp_path.iterdir()) == [output]


@pytest.mark.parametrize(
    "href", ["ch0.xhtml", "nav.xhtml", "toc.ncx", "content.opf", "cover.xhtml"]
)
def test_duplicate_and_reserved_resource_paths_are_rejected(tmp_path, href):
    with pytest.raises(ValueError, match="Duplicate EPUB resource path"):
        _write(tmp_path / "book.epub", resources=[EpubResource(href, "image/png", b"image")])


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"chapters": []}, "without chapters"),
        ({"toc": []}, "without navigation"),
        ({"toc": [EpubTocEntry("missing.xhtml", "Missing")]}, "missing chapter"),
        (
            {
                "resources": [
                    EpubResource("one.png", "image/png", b"1", True),
                    EpubResource("two.png", "image/png", b"2", True),
                ]
            },
            "only one cover",
        ),
    ],
)
def test_invalid_package_inputs_are_rejected(tmp_path, overrides, message):
    with pytest.raises(ValueError, match=message):
        _write(tmp_path / "book.epub", **overrides)


def test_xml_entities_are_not_loaded_from_the_filesystem(tmp_path):
    secret = tmp_path / "private.txt"
    secret.write_text("must not be read", encoding="utf-8")
    content = (
        f'<!DOCTYPE html [<!ENTITY external SYSTEM "{secret.as_uri()}">]>'
        "<html><head/><body><p>&external;</p></body></html>"
    )
    with pytest.raises(ValueError, match="unresolved XML entities"):
        _write(tmp_path / "book.epub", chapters=[EpubChapter("ch0.xhtml", "Title", content)])
    assert not (tmp_path / "book.epub").exists()
    assert list(tmp_path.iterdir()) == [secret]


def test_write_failure_preserves_existing_export_and_cleans_temporary_file(tmp_path, monkeypatch):
    output = tmp_path / "book.epub"
    output.write_bytes(b"previous export")
    original_write = ZipFile.writestr

    def failing_write(archive, name, *args, **kwargs):
        if name == "EPUB/ch0.xhtml":
            raise OSError("disk full")
        return original_write(archive, name, *args, **kwargs)

    monkeypatch.setattr(ZipFile, "writestr", failing_write)
    with pytest.raises(OSError, match="disk full"):
        _write(output)
    assert output.read_bytes() == b"previous export"
    assert list(tmp_path.iterdir()) == [output]


def test_replacement_failure_keeps_previous_export_and_closes_archive(tmp_path, monkeypatch):
    output = tmp_path / "book.epub"
    output.write_bytes(b"previous export")

    def failing_replace(source, target):
        # Replacement runs only after a complete ZIP has been closed.
        with ZipFile(source) as archive:
            assert archive.testzip() is None
            assert "EPUB/content.opf" in archive.namelist()
        raise OSError("replace failed")

    monkeypatch.setattr("wenyi_core.atomicio.os.replace", failing_replace)
    with pytest.raises(OSError, match="replace failed"):
        _write(output)
    assert output.read_bytes() == b"previous export"
    assert list(tmp_path.iterdir()) == [output]
