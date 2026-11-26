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


BARE_BODY_WEBNOVEL = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<html xmlns="http://www.w3.org/1999/xhtml">'
    "<head><title>Chapter 1</title><style>body { color: #111; }</style></head>"
    "<body>"
    '<h1 class="hide720">第一章 陨石</h1>'
    "　　第一段正文。<br/><br/>"
    '　　第二段带有<em>强调</em>和<a id="n1"></a>锚点。<br/>'
    "　　第三段结尾。"
    "</body></html>"
)

MIXED_LOOSE_AND_BOUNDARIES = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<html xmlns="http://www.w3.org/1999/xhtml">'
    "<head><title>Mixed</title></head>"
    "<body>"
    "<script>var ignored = 'do not translate';</script>"
    '<svg xmlns="http://www.w3.org/2000/svg"><text>Cover SVG</text></svg>'
    '<div class="wrap">'
    "<p>Block paragraph</p>"
    "Loose in div line 1<br/>"
    'Loose with <img src="icon.png"/> inline<br/>'
    "</div>"
    "<section>"
    "Section loose line<br/>"
    "<span><section>Nested section line</section></span>"
    "</section>"
    "<pre>raw code block</pre>"
    "</body></html>"
)


def test_bare_body_br_epub_ingest_matches_python_and_bumps_schema(tmp_path, monkeypatch):
    raw = BARE_BODY_WEBNOVEL.encode("utf-8")
    _, legacy = _legacy_resource(raw, monkeypatch)
    document = _book(tmp_path / "bare.epub", raw)

    assert document.meta["epub_schema"] == 7
    segments = document.chapters[0].segments
    assert [s.source for s in segments] == [
        "第一章 陨石",
        "第一段正文。",
        "第二段带有强调和锚点。",
        "第三段结尾。",
    ]
    assert [s.kind for s in segments] == ["heading", "text", "text", "text"]
    assert segments == legacy


def test_mixed_loose_text_containers_and_non_prose_boundaries(tmp_path, monkeypatch):
    raw = MIXED_LOOSE_AND_BOUNDARIES.encode("utf-8")
    _, legacy = _legacy_resource(raw, monkeypatch)
    document = _book(tmp_path / "mixed.epub", raw)

    segments = document.chapters[0].segments
    assert [s.source for s in segments] == [
        "Block paragraph",
        "Loose in div line 1",
        "Loose with inline",
        "Section loose line",
        "Nested section line",
    ]
    assert segments == legacy


@pytest.mark.parametrize(
    ("bilingual", "order"),
    [
        (False, "target_first"),
        (True, "target_first"),
        (True, "source_first"),
    ],
    ids=["mono", "bilingual-target-first", "bilingual-source-first"],
)
def test_bare_body_br_epub_roundtrip_export(tmp_path, bilingual, order):
    from wenyi_core.alignment import target_digest
    from wenyi_core.markup.contracts import ANNOTATION_META_KEY

    source = tmp_path / "bare.epub"
    output = tmp_path / f"out-{bilingual}-{order}.epub"
    markup = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
        "<head><title>Chapter 1</title><style>body { color: #111; }</style></head>"
        "<body>"
        '<h1 class="hide720">第一章 陨石</h1>'
        "　　第一段正文。<br/><br/>"
        '　　第二段带有<em>强调</em>和<a id="n1"></a>锚点<a epub:type="noteref" href="#fn1">1</a>。<br/>'
        "　　第三段结尾。"
        '<aside id="fn1" epub:type="footnote"><p>脚注内容。</p></aside>'
        "</body></html>"
    )
    _write_book(source, markup.encode("utf-8"))

    document = read_epub(source, "zh", "en")
    translations = [
        "Chapter 1: The Meteorite",
        "First body paragraph.",
        "Second paragraph with emphasis and anchor.",
        "Third closing paragraph.",
        "Footnote body.",
    ]
    for seg, target in zip(document.chapters[0].segments, translations, strict=True):
        seg.target = target
    # Exercise real ExportViewStore protected-object marker resolution and aligned noteref placement.
    seg2 = document.chapters[0].segments[2]
    ann_meta = seg2.meta[ANNOTATION_META_KEY]
    obj_item = next(item for item in ann_meta["items"] if item["relation"] == "inline_object")
    note_item = next(item for item in ann_meta["items"] if item["relation"] == "noteref")
    seg2.target = f"Second paragraph with emphasis and {obj_item['marker']}anchor."
    clean_target = "Second paragraph with emphasis and anchor."
    note_pos = len("Second paragraph with emphasis and anchor")
    ann_meta["placements"] = [
        {
            "id": note_item["id"],
            "mode": "point",
            "target_start": note_pos,
            "target_end": note_pos,
            "status": "aligned",
            "method": "aligned",
        }
    ]
    ann_meta["target_digest"] = target_digest(clean_target)

    with closing(FileStorage(str(tmp_path / "state"))) as store:
        store.init_from_document(document)
        store.set_chapter_status(0, "done")
        assemble(
            store,
            str(source),
            str(output),
            "epub",
            bilingual=bilingual,
            order=order,
            about_page=False,
        )

    with ZipFile(output) as archive:
        assert archive.testzip() is None
        exported_xhtml = archive.read("content.xhtml").decode("utf-8")

    # Exported XHTML must remain well-formed XML with no leaked temporary attributes.
    ET.fromstring(exported_xhtml)
    soup = BeautifulSoup(exported_xhtml, "xml")
    assert soup.select("[data-tn-line], [data-tn-id], [data-tn-inline-id]") == []
    body = soup.find("body")
    assert body is not None
    assert body.find("h1").get_text(strip=True) == "Chapter 1: The Meteorite"
    assert len(body.find_all("br")) >= 3
    # Both the aligned interior anchor and footnote reference must survive backfill.
    assert body.find(id="n1") is not None
    assert body.find("a", href="#fn1") is not None
    body_text = body.get_text(" ", strip=True)
    for expected_text in (
        "Chapter 1: The Meteorite",
        "First body paragraph.",
        "Second paragraph with emphasis and anchor 1 .",
        "Third closing paragraph.",
        "Footnote body.",
    ):
        assert expected_text in body_text
    if bilingual:
        source_spans = body.select("span.tn-source")
        assert len(source_spans) == 3
        first_src_pos = exported_xhtml.index("第一段正文。")
        first_tgt_pos = exported_xhtml.index("First body paragraph.")
        if order == "source_first":
            assert first_src_pos < first_tgt_pos
        else:
            assert first_tgt_pos < first_src_pos
        # Bilingual source clone must remap its note link to the cloned footnote target.
        cloned_note_links = [
            a["href"] for span in source_spans for a in span.find_all("a", href=True)
        ]
        assert cloned_note_links
        for href in cloned_note_links:
            assert href.startswith("#")
            assert body.find(id=href[1:]) is not None
    else:
        assert "第一段正文。" not in body_text


@pytest.mark.parametrize("order", ["target_first", "source_first"])
def test_bare_body_br_html_export(tmp_path, order):
    source = tmp_path / "bare.epub"
    output = tmp_path / f"out-{order}.html"
    _write_book(source, BARE_BODY_WEBNOVEL.encode("utf-8"))

    document = read_epub(source, "zh", "en")
    for idx, seg in enumerate(document.chapters[0].segments):
        seg.target = f"Translated {idx}"

    with closing(FileStorage(str(tmp_path / "state"))) as store:
        store.init_from_document(document)
        store.set_chapter_status(0, "done")
        assemble(
            store,
            str(source),
            str(output),
            "html",
            bilingual=True,
            order=order,
            about_page=False,
        )

    rendered = output.read_text(encoding="utf-8")
    assert "data-tn-line" not in rendered
    assert "Translated 0" in rendered
    assert "Translated 3" in rendered
    assert "第一段正文。" in rendered
    src_pos = rendered.index("第一段正文。")
    tgt_pos = rendered.index("Translated 1")
    if order == "source_first":
        assert src_pos < tgt_pos
    else:
        assert tgt_pos < src_pos


@pytest.mark.parametrize("legacy_schema", [5, 6, None, -1])
@pytest.mark.parametrize("out_format", ["epub", "html"])
def test_legacy_schema_export_and_object_refresh_do_not_shift_anchors(
    tmp_path, monkeypatch, legacy_schema, out_format
):
    from wenyi_core.pipeline.prepare.epub_objects import refresh_epub_objects

    markup = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
        "Loose intro<br/>"
        "<h1>Heading</h1>"
        '<p>First <img src="a.png"/> para</p>'
        "* * *"
        "<p>Second para</p>"
        "</body></html>"
    )
    source = tmp_path / "legacy.epub"
    output = tmp_path / f"out-{legacy_schema}.{out_format}"
    _write_book(source, markup.encode("utf-8"))

    # Simulate state saved under pre-schema-7 rules (candidate blocks only, no loose text).
    with monkeypatch.context() as patch:
        patch.setattr(epub_dom, "xhtml_tokens", None)
        title, segments, _ = annotate_epub_resource(
            markup, 0, "content.xhtml", extract_loose_text=False
        )
    assert [s.source for s in segments] == ["Heading", "First para", "Second para"]
    for idx, seg in enumerate(segments):
        seg.target = f"Translated {idx}"
        seg.meta.pop(INLINE_META_KEY, None)

    meta: dict[str, object] = {
        "opf_path": "book.opf",
        "epub_resources": [{"index": 0, "href": "content.xhtml"}],
    }
    if legacy_schema is not None:
        meta["epub_schema"] = legacy_schema
    document = Document(
        title="Legacy",
        source_lang="en",
        target_lang="zh",
        fmt="epub",
        source_path=str(source),
        chapters=[Chapter(index=0, title=title, href="content.xhtml", segments=segments)],
        meta=meta,
    )
    state_dir = str(tmp_path / f"state-{legacy_schema}-{out_format}")
    with closing(FileStorage(state_dir)) as store:
        store.init_from_document(document)
        store.set_chapter_status(0, "done")
        # Refreshing protected objects on resume must also honor saved epub_schema.
        refresh_epub_objects(store, str(source))
        assert store.load_manifest()["meta"]["epub_objects"] == 1
        assemble(store, str(source), str(output), out_format, bilingual=True, about_page=False)
    assert output.exists() and output.stat().st_size > 0


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            "<div>A<br/><section>B<br/>C</section>D</div>",
            ["A", "B", "C", "D"],
        ),
        (
            "<div>Intro<pre><code>if x:\n    return x</code></pre></div>",
            ["Intro"],
        ),
        (
            '<form><button type="submit">Submit</button></form>',
            [],
        ),
        (
            '<div class="chapter"><header>第一章</header>正文一<br/>正文二</div>',
            ["第一章", "正文一", "正文二"],
        ),
        (
            "<blockquote>Quote text<footer>— Author</footer></blockquote>",
            ["Quote text", "— Author"],
        ),
        (
            '<div>Intro<figure>Caption only text<img src="c.png"/></figure></div>',
            ["Intro", "Caption only text"],
        ),
        (
            "<div><details><summary>Sum</summary>Detail body</details></div>",
            ["Sum", "Detail body"],
        ),
        (
            "<div>A<address>Addr</address></div>",
            ["A", "Addr"],
        ),
        (
            "<div><span>A<br/>B</span></div>",
            ["A", "B"],
        ),
        (
            "<div><span>A<script>window.x=1;</script>B</span></div>",
            ["A", "B"],
        ),
        (
            "<div>Hello<template>Hidden</template>World</div>",
            ["Hello", "World"],
        ),
        (
            '<a href="#target"><div><span id="inner">First</span><br/>Second</div></a>',
            ["First", "Second"],
        ),
        (
            '<a id="outer"><span id="inner">First</span><br/>Second</a>',
            ["First", "Second"],
        ),
        (
            '<ul><li><a href="#target"><p id="target"/>Line</a></li></ul>',
            ["Line"],
        ),
        (
            '<ul><li><a href="ch1.xhtml"><span>A<br/>B</span></a></li></ul>',
            ["A", "B"],
        ),
        (
            '<ul><li><a href="chapter2.xhtml">Einstein<br/>E = mc<sup>2</sup></a></li></ul>',
            ["Einstein", "E = mc2"],
        ),
        (
            '<ul><li><a epub:type="noteref" href="#fn1">First<br/>Second</a></li></ul>',
            ["First", "Second"],
        ),
        (
            '<template><a href="http://[invalid">bad</a></template><p>OK</p>',
            ["OK"],
        ),
        (
            '<a href="https://example.com">First<br/>Second</a>',
            ["First", "Second"],
        ),
        (
            "A <span>B<br/>C</span> D<br/>E",
            ["A", "B", "C", "D", "E"],
        ),
        (
            "A<span> B<br/>C </span>D",
            ["A", "B", "C", "D"],
        ),
        (
            "<div>x<span> <b>B</b><br/>C </span> y</div>",
            ["x", "B", "C", "y"],
        ),
        (
            "<p>Hello <template>Hidden</template> world</p>",
            ["Hello", "world"],
        ),
        (
            "<div>A<?pagebreak 5?>B<br/>C</div>",
            ["AB", "C"],
        ),
        (
            "<p><template><p>Hidden</p></template>Visible text</p>",
            ["Visible text"],
        ),
        (
            "<p><noscript><div>No JS</div></noscript>Visible text</p>",
            ["Visible text"],
        ),
        (
            '<p><svg xmlns="http://www.w3.org/2000/svg"><foreignObject><p>SVG</p></foreignObject></svg>Visible text</p>',
            ["Visible text"],
        ),
        (
            '<figure><svg xmlns="http://www.w3.org/2000/svg"><foreignObject><p>SVG</p></foreignObject></svg></figure>',
            [],
        ),
        (
            "<div>Before<hr/>After</div>",
            ["Before", "After"],
        ),
        (
            "A<span><template>Hidden</template>B</span>C",
            ["A", "B", "C"],
        ),
        (
            "<span>Hello<script>var x=1;</script>world</span>",
            ["Hello", "world"],
        ),
        (
            '<ul><li><a href="#target"><p id="target">First</p><p>Second</p></a></li></ul>',
            ["First", "Second"],
        ),
        (
            "<span>Before<p>Inside</p>After</span>",
            ["Before", "Inside", "After"],
        ),
        (
            "<span>Left<hr/>Right</span>",
            ["Left", "Right"],
        ),
        (
            "<b>bold A<br/>bold B</b><br/>C",
            ["bold A", "bold B", "C"],
        ),
        (
            "<div><div>Nested loose 1<br/>Nested loose 2</div></div>",
            ["Nested loose 1", "Nested loose 2"],
        ),
        (
            "　　<br/>Real line<br/>　　",
            ["Real line"],
        ),
        (
            '<img src="cover.jpg"/><a href="x"><img src="c2.jpg"/></a>'
            '<svg xmlns="http://www.w3.org/2000/svg"><text>Cover</text></svg>',
            [],
        ),
        (
            "<ruby>漢字<rt>かんじ</rt></ruby> text<br/><ruby><rt>only</rt></ruby>",
            ["漢字〘かんじ〙 text"],
        ),
        (
            "<!-- comment --><?page 1?>Line 1<br/><!DOCTYPE html>Line 2",
            ["Line 1", "Line 2"],
        ),
    ],
    ids=[
        "div-with-section",
        "div-with-pre-code",
        "form-with-button",
        "div-with-header",
        "blockquote-with-footer",
        "div-with-figure",
        "div-with-details-summary",
        "div-with-address",
        "div-inline-br",
        "div-inline-script",
        "div-direct-template",
        "link-block-split-inner-id",
        "anchor-only-split-inner-id",
        "li-link-empty-block-id",
        "li-link-nested-span-br",
        "li-link-formula-sup-br",
        "li-link-noteref-no-sup-br",
        "template-invalid-url",
        "external-link-br",
        "split-inline-boundary-spaces",
        "split-inline-inner-boundary-spaces",
        "split-inline-inner-tag-boundary-spaces",
        "p-template-boundary-spaces",
        "mid-run-pi-order",
        "p-with-template-block",
        "p-with-noscript-block",
        "p-with-svg-foreignobject",
        "figure-with-svg-foreignobject",
        "div-with-hr",
        "inline-with-template",
        "inline-with-script",
        "li-link-with-blocks",
        "inline-around-p",
        "inline-around-hr",
        "inline-around-br",
        "nested-div-loose",
        "ideographic-space-only",
        "cover-media-only",
        "ruby-and-rt-only",
        "comments-pi-doctype",
    ],
)
def test_schema7_edge_cases_rust_python_parity_and_export(body, expected, tmp_path, monkeypatch):
    markup = f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml"><body>{body}</body></html>'
    raw = markup.encode("utf-8")
    _, legacy = _legacy_resource(raw, monkeypatch)
    source = tmp_path / "edge.epub"
    document = _book(source, raw)
    actual = [s.source for c in document.chapters for s in c.segments]
    assert actual == expected
    assert [s.source for s in legacy] == expected

    # Verify mono + bilingual export preserves structural elements and never leaks wrappers.
    if document.chapters:
        for idx, seg in enumerate(document.chapters[0].segments):
            seg.target = f"T{idx}"
        with closing(FileStorage(str(tmp_path / "state"))) as store:
            store.init_from_document(document)
            store.set_chapter_status(0, "done")
            for bilingual in (False, True):
                out = tmp_path / f"out-{bilingual}.epub"
                assemble(
                    store, str(source), str(out), "epub", bilingual=bilingual, about_page=False
                )
                with ZipFile(out) as zf:
                    xhtml = zf.read("content.xhtml").decode("utf-8")
                assert "data-tn-line" not in xhtml
                if "<template>" in body:
                    assert "<template>" in xhtml
                if "<script>" in body:
                    assert "<script>" in xhtml
                if "<section>" in body:
                    assert "<section>" in xhtml
                if "<pre>" in body:
                    assert "<pre><code>if x:\n    return x</code></pre>" in xhtml
                if 'id="target"' in body or 'id="inner"' in body:
                    soup = BeautifulSoup(xhtml, "xml")
                    if 'id="target"' in body:
                        assert soup.find(id="target") is not None
                    if 'id="inner"' in body:
                        assert soup.find(id="inner") is not None
                    # Must not create nested <a> tags inside an outer <a href="#target">.
                    assert all(a.find("a") is None for a in soup.find_all("a"))
                if body == "A <span>B<br/>C</span> D<br/>E" and not bilingual:
                    assert "T0 <span>T1<br/>T2</span> T3<br/>T4" in xhtml
                if body == "A<span> B<br/>C </span>D" and not bilingual:
                    assert "T0<span> T1<br/>T2 </span>T3" in xhtml
                if body == "<div>x<span> <b>B</b><br/>C </span> y</div>" and not bilingual:
                    assert "<div>T0<span> T1<br/>T2 </span> T3</div>" in xhtml
                if body == "<p>Hello <template>Hidden</template> world</p>" and not bilingual:
                    assert "<p>T0 <template>Hidden</template> T1</p>" in xhtml
                if body == '<ul><li><a href="ch1.xhtml"><span>A<br/>B</span></a></li></ul>':
                    assert "<br" in xhtml and "<span>" in xhtml
                if (
                    body
                    == '<ul><li><a href="chapter2.xhtml">Einstein<br/>E = mc<sup>2</sup></a></li></ul>'
                ):
                    assert "<br" in xhtml and "chapter2.xhtml" in xhtml
                if (
                    body
                    == '<ul><li><a epub:type="noteref" href="#fn1">First<br/>Second</a></li></ul>'
                    and not bilingual
                ):
                    assert "T0<br/>T1" in xhtml
                if body == '<a href="https://example.com">First<br/>Second</a>' and not bilingual:
                    assert '<a href="https://example.com">T0<br/>T1</a>' in xhtml
                if body == "<div>A<?pagebreak 5?>B<br/>C</div>" and not bilingual:
                    assert "<div>T0<?pagebreak 5?><br/>T1</div>" in xhtml


def test_legacy_html_project_export_keeps_pre_schema7_anchors(tmp_path):
    from wenyi_core.ingest.html_reader import read_html

    source = tmp_path / "legacy.html"
    output = tmp_path / "out.html"
    source.write_text(
        "<html><body><div>Loose<br/><p>Saved paragraph</p></div></body></html>",
        encoding="utf-8",
    )
    document = read_html(str(source), "en", "zh", preserve_inline_markup=False)
    assert [s.source for c in document.chapters for s in c.segments] == ["Saved paragraph"]
    document.chapters[0].segments[0].target = "已保存的段落"
    with closing(FileStorage(str(tmp_path / "state"))) as store:
        store.init_from_document(document)
        store.set_chapter_status(0, "done")
        assemble(store, str(source), str(output), "html", bilingual=False, about_page=False)
    rendered = output.read_text(encoding="utf-8")
    assert "已保存的段落" in rendered
    assert "Loose" in rendered


def test_noteref_link_with_br_preserves_annotation_and_marker(tmp_path, monkeypatch):
    markup = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
        '<body><div><a epub:type="noteref" href="#fn1">First<br/>Second<sup>1</sup></a></div>'
        '<aside id="fn1" epub:type="footnote"><p>Footnote</p></aside></body></html>'
    )
    raw = markup.encode("utf-8")
    _, legacy = _legacy_resource(raw, monkeypatch)
    source = tmp_path / "noteref.epub"
    document = _book(source, raw)
    segs = document.chapters[0].segments
    assert len(segs) == 2
    assert segs == legacy
    ann_items = segs[0].meta["epub_annotations"]["items"]
    assert len(ann_items) == 1
    assert ann_items[0]["relation"] == "noteref"
    assert ann_items[0]["marker_text"] == "1"

    segs[0].target = "Translated note text"
    segs[1].target = "Translated footnote"
    with closing(FileStorage(str(tmp_path / "state"))) as store:
        store.init_from_document(document)
        store.set_chapter_status(0, "done")
        out = tmp_path / "out.epub"
        assemble(store, str(source), str(out), "epub", bilingual=False, about_page=False)
    with ZipFile(out) as zf:
        xhtml = zf.read("content.xhtml").decode("utf-8")
    assert "<sup>1</sup>" in xhtml
    assert 'epub:type="noteref"' in xhtml


def test_skip_navigation_excludes_loose_text_inside_nav_li(tmp_path):
    markup = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
        "<body>"
        '<nav epub:type="toc">'
        "<h1>Contents</h1>"
        "<div>TOC note</div>"
        "<ol>"
        '<li><a href="c1.xhtml">Chapter 1</a></li>'
        "<li><div>Loose inside nav li</div></li>"
        "</ol>"
        "</nav>"
        "</body></html>"
    )
    _, segments, _ = annotate_epub_resource(markup, 0, "nav.xhtml", skip_navigation=True)
    assert [s.source for s in segments] == ["Contents", "TOC note"]


def test_deeply_nested_spans_do_not_overflow_stack(tmp_path):
    depth = 15000
    markup = (
        '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
        + "<span>" * depth
        + "Deep bare text"
        + "</span>" * depth
        + "</body></html>"
    )
    document = _book(tmp_path / "deep.epub", markup.encode("utf-8"))
    assert [s.source for s in document.chapters[0].segments] == ["Deep bare text"]

    # Also verify Python's full annotation path handles deep nesting without RecursionError.
    from wenyi_core.markup.segments import annotate_resource_dom

    soup = BeautifulSoup("<html><body></body></html>", "html.parser")
    parent = soup.body
    for _ in range(1500):
        span = soup.new_tag("span")
        parent.append(span)
        parent = span
    parent.append("Deep python bare text")
    _, py_segments = annotate_resource_dom(soup, 0, "deep.xhtml")
    assert [s.source for s in py_segments] == ["Deep python bare text"]
