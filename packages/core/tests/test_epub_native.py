"""Verify the native EPUB parser against the legacy state contract."""

from __future__ import annotations

from contextlib import closing
from os import utime
from struct import pack_into, unpack_from
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest
from bs4 import BeautifulSoup, UnicodeDammit
from wenyi_core.assemble import epub_native
from wenyi_core.assemble.about import append_about_page
from wenyi_core.assemble.writer import assemble
from wenyi_core.ingest.epub_reader import read_epub
from wenyi_core.markup import epub_dom
from wenyi_core.markup.anchors import fragment_anchor_map
from wenyi_core.markup.contracts import INLINE_META_KEY
from wenyi_core.markup.segments import annotate_epub_resource
from wenyi_core.models.document import Chapter, Document
from wenyi_core.storage.file import FileStorage


def _write_book(path, markup):
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "META-INF/container.xml", '<container><rootfile full-path="book.opf"/></container>'
        )
        archive.writestr(
            "book.opf",
            '<package><manifest><item id="s" href="content.xhtml"/></manifest>'
            '<spine><itemref idref="s"/></spine></package>',
        )
        archive.writestr("content.xhtml", markup)


def _book(path, markup):
    _write_book(path, markup)
    return read_epub(path, "en", "zh")


def _legacy_resource(raw, monkeypatch):
    # Match the pre-native decode and annotation path, not the current native reader.
    markup = UnicodeDammit(raw).unicode_markup
    assert markup is not None
    with monkeypatch.context() as patch:
        patch.setattr(epub_dom, "xhtml_tokens", None)
        title, segments, _ = annotate_epub_resource(markup, 0, "content.xhtml")
    for segment in segments:
        segment.meta.pop(INLINE_META_KEY, None)
    return title, segments


CDATA_ONLY = "<![CDATA[Only &amp; text]]>"
CDATA_MIXED = "A<![CDATA[B & C]]>D"
ESCAPED = "&lt;![CDATA[Not markup]]&gt; &amp;amp;"
TOLERANT_CDATA = (
    '<BODY><P id=part-0 title="<![CDATA[attribute]]>"><![CDATA[Only text]]></P>'
    "<P id=part-1>A<![CDATA[B > C &amp; <!-- text -->]]>D&nbsp;E</P></BODY>"
)
RUSSIAN = "Привет, мир! Это старая книга, которую мы читаем каждый вечер."
WESTERN = "¡El pingüino bebió café! ¿Cuánto cuesta? “Sólo €5”, respondió el dueño."


def _paragraphs(*contents):
    body = "".join(f'<p id="part-{i}">{text}</p>' for i, text in enumerate(contents))
    return f'<html xmlns="http://www.w3.org/1999/xhtml"><body>{body}</body></html>'


@pytest.mark.parametrize(
    ("markup", "expected"),
    [
        (
            _paragraphs(CDATA_ONLY, CDATA_MIXED, ESCAPED),
            ["Only &amp; text", "AB & CD", "<![CDATA[Not markup]]> &amp;"],
        ),
        (TOLERANT_CDATA, ["Only text", "AB > C &amp; <!-- text -->D\xa0E"]),
    ],
    ids=["xhtml", "tolerant"],
)
def test_native_ingest_keeps_cdata_text_and_anchors(markup, expected, tmp_path, monkeypatch):
    raw = markup.encode()
    _, legacy = _legacy_resource(raw, monkeypatch)
    assert [s.source for s in legacy] == expected
    assert [s.anchor for s in legacy] == [f"tn0_{i}" for i in range(len(expected))]
    assert _book(tmp_path / "source.epub", raw).chapters[0].segments == legacy


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (_paragraphs(RUSSIAN).encode("cp1251"), RUSSIAN),
        (_paragraphs(WESTERN).encode("cp1252"), WESTERN),
        (
            ('<?xml version="1.0" encoding="windows-1251"?>' + _paragraphs(RUSSIAN)).encode(
                "cp1251"
            ),
            RUSSIAN,
        ),
        (
            ('<?xml version="1.0" encoding="windows-1252"?>' + _paragraphs(RUSSIAN)).encode(
                "utf-8-sig"
            ),
            RUSSIAN,
        ),
        (
            ('<?xml version="1.0" encoding="UTF-16"?>' + _paragraphs(RUSSIAN)).encode("utf-16"),
            RUSSIAN,
        ),
    ],
    ids=["undeclared-cp1251", "undeclared-cp1252", "xml-declaration", "utf8-bom", "utf16-bom"],
)
def test_native_ingest_sniffs_legacy_encoding(raw, expected, tmp_path, monkeypatch):
    _, legacy = _legacy_resource(raw, monkeypatch)
    assert [s.source for s in legacy] == [expected]
    assert _book(tmp_path / "source.epub", raw).chapters[0].segments == legacy


@pytest.mark.parametrize(
    "raw",
    [
        _paragraphs(CDATA_ONLY, CDATA_MIXED, ESCAPED).encode(),
        TOLERANT_CDATA.encode(),
        _paragraphs(RUSSIAN).encode("cp1251"),
        _paragraphs(WESTERN).encode("cp1252"),
    ],
    ids=["xhtml-cdata", "tolerant-cdata", "undeclared-cp1251", "undeclared-cp1252"],
)
@pytest.mark.parametrize("out_format", ["epub", "html"])
def test_source_backed_export_reuses_legacy_saved_state(raw, out_format, tmp_path, monkeypatch):
    source, output = tmp_path / "source.epub", tmp_path / f"output.{out_format}"
    _write_book(source, raw)
    title, segments = _legacy_resource(raw, monkeypatch)
    targets = [f"译文 {i + 1}" for i in range(len(segments))]
    for segment, target in zip(segments, targets, strict=True):
        segment.target = target
    document = Document(
        title="Legacy book",
        source_lang="en",
        target_lang="zh",
        fmt="epub",
        source_path=str(source),
        chapters=[Chapter(index=0, title=title, href="content.xhtml", segments=segments)],
        meta={
            "epub_schema": 5,
            "epub_objects": 1,
            "opf_path": "book.opf",
            "epub_resources": [{"index": 0, "href": "content.xhtml"}],
        },
    )
    state_dir = str(tmp_path / "state")
    with closing(FileStorage(state_dir)) as store:
        store.init_from_document(document)
        store.set_chapter_status(0, "done")
        manifest = store.load_manifest()
    with closing(FileStorage(state_dir)) as store:
        assemble(store, str(source), str(output), out_format, bilingual=True, about_page=False)
        assert store.load_manifest() == manifest
        assert store.load_chapter(0).segments == segments

    if out_format == "epub":
        with ZipFile(output) as archive:
            rendered = archive.read("content.xhtml").decode()
    else:
        rendered = output.read_text(encoding="utf-8")
    soup = BeautifulSoup(rendered, "html.parser")
    for i, segment in enumerate(segments):
        target = soup.find(id=f"part-{i}")
        original = soup.find(id=f"tn-source-{segment.anchor}")
        assert target is not None and target.get_text() == segment.target
        assert original is not None and original.get_text() == segment.source
    with ZipFile(source) as archive:
        assert archive.read("content.xhtml") == raw


@pytest.mark.parametrize(
    "markup",
    [
        '<?xml version="1.0"?><!DOCTYPE html PUBLIC "test" "ignored.dtd">'
        '<html xmlns:epub="urn:epub"><body><!-- test -->\n'
        '<h1 id="chapter">字&amp;文</h1><p>One<?page 2?><br/>Two'
        '<a epub:type="noteref" href="notes.xhtml#n1"><sup>1</sup></a></p>'
        "<p>A <ruby>字<rt>じ</rt></ruby> B</p></body></html>",
        '<p class="x y" title="a > b &amp; c" id="first" id="last">'
        'Text <img src="cover.png"/> with <span id="dest">emphasis</span>.</p>',
        "<div><pre>  \r\n \t </pre><p /> <a/></div>",
        "<p>A<![CDATA[<literal> &amp;]]>B <![CDATA[  \n  ]]> C</p>",
    ],
)
def test_native_tokens_preserve_dom_and_state(markup, monkeypatch, tmp_path):
    assert epub_dom.xhtml_tokens is not None
    assert epub_dom.xhtml_tokens(markup) is not None
    assert str(epub_dom.parse_epub_dom(markup)) == str(BeautifulSoup(markup, "html.parser"))
    monkeypatch.setattr(epub_dom, "xhtml_tokens", None)
    title, segments, _ = annotate_epub_resource(markup, 0, "content.xhtml")
    for segment in segments:
        segment.meta.pop(INLINE_META_KEY, None)
    book = _book(tmp_path / "source.epub", markup)
    if segments:
        assert book.chapters[0].title == title
        assert book.chapters[0].segments == segments
    else:
        assert not book.chapters


@pytest.mark.parametrize(
    "markup",
    [
        "<p><em>Broken</p>",
        "<p>HTML &nbsp; entities</p>",
        '<p title="HTML &copy;">Text</p>',
        "<p>&#128; is HTML-specific.</p>",
        "<p>Break<br>here</p>",
        "<p title=unquoted>Text</p>",
        "<style>p::before { content: '<'; }</style><p>A</p>",
        "<P>Mixed case</P>",
    ],
)
def test_nonportable_xhtml_uses_native_tolerant_parser(markup, tmp_path):
    assert epub_dom.xhtml_tokens is not None
    assert epub_dom.xhtml_tokens(markup) is None
    book = _book(tmp_path / "source.epub", markup)
    _, segments, _ = annotate_epub_resource(markup, 0, "content.xhtml")
    assert book.chapters[0].segments == segments


def test_fragment_index_keeps_first_owner_and_distinguishes_trailing_markers():
    soup = epub_dom.parse_epub_dom(
        '<div id="before"><i id="duplicate"/></div>'
        '<p data-tn-id="tn0_0"><span id="within"/>Text</p>'
        '<span id="duplicate" name="next"/>'
        '<p id="on" data-tn-id="tn0_1">Text</p>'
        '<div id="trailing"/>'
    )
    assert list(fragment_anchor_map(soup).items()) == [
        ("before", "tn0_0"),
        ("duplicate", "tn0_0"),
        ("within", "tn0_0"),
        ("next", "tn0_1"),
        ("on", "tn0_1"),
        ("trailing", None),
    ]


def test_html_export_embeds_only_zip_media_and_keeps_image_only_spine_pages(tmp_path):
    book = tmp_path / "source.epub"
    out = tmp_path / "export.html"
    data_uri = "data:image/png;base64,AAAA"
    with ZipFile(book, "w") as archive:
        archive.writestr(
            "META-INF/container.xml", '<container><rootfile full-path="book.opf"/></container>'
        )
        archive.writestr(
            "book.opf",
            '<package><manifest><item id="a" href="Text/a.xhtml"/>'
            '<item id="b" href="Text/b.xhtml"/></manifest>'
            '<spine><itemref idref="a"/><itemref idref="b"/></spine></package>',
        )
        archive.writestr(
            "Text/a.xhtml",
            '<html><body><p>Body</p><img id="main" src="../Images/cover+1.png?rev=1#sample" '
            f'srcset="{data_uri} 1x, ../Images/cover%2B1.png 2x, https://example.invalid/c.png 3x"/>'
            '<object data="file:///missing.png"/></body></html>',
        )
        archive.writestr(
            "Text/b.xhtml", '<html><body><img id="plate" src="Images/cover+1.png"/></body></html>'
        )
        archive.writestr("Images/cover+1.png", b"cover")

    book_bytes = book.read_bytes()
    with closing(FileStorage(str(tmp_path / "state"))) as store:
        document = read_epub(book, "en", "zh")
        document.chapters[0].segments[0].target = "正文"
        store.save_manifest(store.stage_document(document))
        assemble(store, str(book), str(out), out_format="html")
    soup = BeautifulSoup(out.read_text(encoding="utf-8"), "html.parser")
    embedded = "data:image/png;base64,Y292ZXI="
    assert soup.p is not None and soup.p.string == "正文"
    main, plate = soup.find(id="main"), soup.find(id="plate")
    assert main is not None and plate is not None and main["src"] == plate["src"] == embedded
    assert main["srcset"] == (f"{data_uri} 1x, {embedded} 2x, https://example.invalid/c.png 3x")
    assert soup.object is not None and soup.object["data"] == "file:///missing.png"
    assert book.read_bytes() == book_bytes


def test_about_append_preserves_package_prefixes_existing_ids_and_unrelated_members(tmp_path):
    path = tmp_path / "source.epub"
    original_page = "<html><body>Original</body></html>"
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "META-INF/container.xml", '<container><rootfile full-path="OPS/book.xml"/></container>'
        )
        archive.writestr(
            "OPS/book.xml",
            '<opf:package xmlns:opf="http://www.idpf.org/2007/opf">'
            '<opf:manifest><opf:item id="trans-novel-about" href="trans-novel-about.xhtml"/>'
            "</opf:manifest><opf:spine /></opf:package>",
        )
        archive.writestr("OPS/trans-novel-about.xhtml", original_page)

    assert append_about_page(str(path), "zh-Hans")
    with ZipFile(path) as archive:
        assert archive.testzip() is None
        assert archive.read("OPS/trans-novel-about.xhtml").decode() == original_page
        assert archive.read("OPS/trans-novel-about-1.xhtml")
        opf = ET.fromstring(archive.read("OPS/book.xml"))
    ns = {"opf": "http://www.idpf.org/2007/opf"}
    items = opf.findall("opf:manifest/opf:item", ns)
    assert [(item.get("id"), item.get("href")) for item in items] == [
        ("trans-novel-about", "trans-novel-about.xhtml"),
        ("trans-novel-about-1", "trans-novel-about-1.xhtml"),
    ]
    itemref = opf.find("opf:spine/opf:itemref", ns)
    assert itemref is not None and itemref.get("idref") == "trans-novel-about-1"


def _zip_payload_offset(data, header):
    name_size, extra_size = unpack_from("<HH", data, header + 26)
    return header + 30 + name_size + extra_size


@pytest.mark.parametrize("about_only", [False, True], ids=["export", "about-copy"])
@pytest.mark.parametrize(
    ("compression", "damage", "error"),
    [
        (ZIP_STORED, "crc", "checksum"),
        (ZIP_DEFLATED, "crc", "checksum"),
        (ZIP_STORED, "short-size", "uncompressed size"),
        (ZIP_DEFLATED, "long-size", "uncompressed size"),
        (ZIP_DEFLATED, "short-stream", "incomplete deflate stream"),
        (ZIP_DEFLATED, "long-stream", "compressed size"),
    ],
    ids=["stored-crc", "deflated-crc", "stored-size", "deflated-size", "truncated", "overlong"],
)
def test_epub_copy_checks_unchanged_member_integrity(
    tmp_path, about_only, compression, damage, error
):
    source, output = tmp_path / "source.epub", tmp_path / "export.epub"
    _write_book(source, _paragraphs("Hello, world."))
    cover = b"synthetic cover data" * 5000
    with ZipFile(source, "a") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("cover.png", cover, compress_type=compression, compresslevel=0)
        member = archive.getinfo("cover.png")
    good_source = source.read_bytes()
    start = _zip_payload_offset(good_source, member.header_offset)

    with closing(FileStorage(str(tmp_path / "state"))) as store:
        document = read_epub(source, "en", "zh")
        document.chapters[0].segments[0].target = "你好。"
        store.save_manifest(store.stage_document(document))

        def publish():
            if about_only:
                assert append_about_page(str(source), "zh-Hans")
            else:
                assemble(store, str(source), str(output), out_format="epub", about_page=False)

        publish()
        target = source if about_only else output
        published = target.read_bytes()
        with ZipFile(target) as archive:
            assert archive.testzip() is None
            assert archive.infolist()[0].filename == "mimetype"
            assert archive.infolist()[0].compress_type == ZIP_STORED
            assert archive.read("cover.png") == cover
            if about_only:
                assert archive.read("trans-novel-about.xhtml")
            else:
                assert "你好。" in archive.read("content.xhtml").decode()
            copied = archive.getinfo("cover.png")

        copied_start = _zip_payload_offset(published, copied.header_offset)
        assert copied.compress_type == member.compress_type
        assert copied.compress_size == member.compress_size
        assert (
            published[copied_start : copied_start + copied.compress_size]
            == good_source[start : start + member.compress_size]
        )

        # Corrupt the same file without changing its size or mtime. For the CRC
        # case, keep the metadata intact and flip a late literal in the stream.
        if about_only:
            member, start = copied, copied_start
        damaged = bytearray(published if about_only else good_source)
        if damage == "crc":
            end = start + member.compress_size
            damaged[damaged.rindex(b"synthetic cover data", start, end)] ^= 1
        else:
            # The fixture has one central-directory entry for cover.png.
            name = damaged.index(b"cover.png", start + member.compress_size)
            central = name - 46
            assert damaged[central : central + 4] == b"PK\x01\x02"
            if damage in ("short-size", "long-size"):
                size = member.file_size + (-1 if damage == "short-size" else 1)
                pack_into("<I", damaged, member.header_offset + 22, size)
                pack_into("<I", damaged, central + 24, size)
            else:
                size = member.compress_size + (-1 if damage == "short-stream" else 1)
                pack_into("<I", damaged, member.header_offset + 18, size)
                pack_into("<I", damaged, central + 20, size)

        stat = source.stat()
        source.write_bytes(damaged)
        utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        with pytest.raises(OSError, match=rf"cover\.png.*{error}"):
            publish()
        assert source.read_bytes() == damaged
        assert target.read_bytes() == (damaged if about_only else published)
        assert not list(tmp_path.glob(".wenyi-*.tmp"))


def test_export_creates_missing_language_in_empty_metadata(tmp_path):
    source, output = tmp_path / "source.epub", tmp_path / "export.epub"
    with ZipFile(source, "w") as archive:
        archive.writestr(
            "META-INF/container.xml", '<container><rootfile full-path="OPS/book.xml"/></container>'
        )
        archive.writestr(
            "OPS/book.xml",
            '<package xmlns="http://www.idpf.org/2007/opf"><metadata/><manifest/><spine/></package>',
        )

    state = epub_native.options(
        [],
        {"opf_path": "OPS/book.xml"},
        book_title="Original",
        source_lang="en",
        target_lang="zh-Hans",
        export_title="Translated",
        bilingual=False,
        order="target_first",
        preserve_source_style=False,
    )
    epub_native.export(str(source), output, state)
    with ZipFile(output) as archive:
        opf = ET.fromstring(archive.read("OPS/book.xml"))
    ns = {"opf": "http://www.idpf.org/2007/opf", "dc": "http://purl.org/dc/elements/1.1/"}
    assert opf.findtext("opf:metadata/dc:language", namespaces=ns) == "zh-Hans"
    assert opf.find("opf:metadata/dc:title", ns) is None


def test_nav_title_repeating_book_title_uses_translated_book_title():
    from wenyi_core.assemble.epub_native import _nav_titles

    meta = {
        "epub_nav_titles": ["特拉福买家俱乐部", "目录"],
        "epub_nav_titles_translated": ["Trafford Buyer Club", "Contents"],
    }

    assert _nav_titles(
        meta, "特拉福买家俱乐部", "The Trafford Buyers Club", "特拉福买家俱乐部"
    ) == {
        "特拉福买家俱乐部": "The Trafford Buyers Club",
        "目录": "Contents",
    }
    # Without a translated book title the saved nav translation stands.
    assert (
        _nav_titles(meta, "特拉福买家俱乐部", "特拉福买家俱乐部-wenyi-en", None)["特拉福买家俱乐部"]
        == "Trafford Buyer Club"
    )
