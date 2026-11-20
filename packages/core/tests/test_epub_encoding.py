"""Synthetic EPUB fixtures for source decoding and UTF-8 template publication."""

from contextlib import closing
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from bs4 import BeautifulSoup, UnicodeDammit
from wenyi_core.assemble.writer import assemble
from wenyi_core.ingest.epub_reader import peek_epub_title, read_epub
from wenyi_core.markup import epub_dom
from wenyi_core.markup.contracts import INLINE_META_KEY
from wenyi_core.markup.segments import annotate_epub_resource
from wenyi_core.models.document import Chapter, Document
from wenyi_core.storage.file import FileStorage

CONTAINER = '<container><rootfile full-path="book.opf"/></container>'
PACKAGE = (
    '<package xmlns:dc="http://purl.org/dc/elements/1.1/">'
    "<metadata><dc:title>Старая книга</dc:title><dc:language>ru</dc:language></metadata>"
    '<manifest><item id="body" href="body.xhtml" media-type="application/xhtml+xml"/>'
    '<item id="extra" href="extra.xhtml" media-type="application/xhtml+xml"/></manifest>'
    '<spine><itemref idref="body"/></spine></package>'
)
RUSSIAN = "Привет, мир! Это старая книга."
TARGET = "译文，仍然是正确的。"
MIME = """text/html; profile='keep; charset=windows-1252'; Charset = 'windows-1251'; extra=yes"""
FAKE_META = '<!-- <meta charset="windows-1252"> -->'
FAKE_XML = '<?xml version="1.0" encoding="windows-1252"?>'


def _page(text, head=""):
    return f'<html><head>{head}</head><body><p id="text">{text}</p></body></html>'


def _write_book(path, body, *, extra=None, container=CONTAINER, package=PACKAGE):
    with ZipFile(path, "w") as archive:
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("book.opf", package)
        archive.writestr("body.xhtml", body)
        if extra is not None:
            archive.writestr("extra.xhtml", extra)


def _export(document, source, output, tmp_path, *, bilingual=False):
    with closing(FileStorage(str(tmp_path / "state"))) as store:
        store.init_from_document(document)
        store.set_chapter_status(0, "done")
        manifest = store.load_manifest()
        assemble(
            store,
            str(source),
            str(output),
            output.suffix[1:],
            bilingual=bilingual,
            about_page=False,
        )
        assert store.load_manifest() == manifest
        assert store.load_chapter(0).segments == document.chapters[0].segments


@pytest.mark.parametrize(
    ("head", "attr", "expected"),
    [
        ('<meta charset="windows-1251"/>', "charset", "utf-8"),
        (
            f'<META CONTENT="{MIME}" HTTP-EQUIV=Content-Type>',
            "content",
            MIME.replace("'windows-1251'", "'utf-8'"),
        ),
    ],
    ids=["charset", "pragma"],
)
@pytest.mark.parametrize("bilingual", [False, True], ids=["monolingual", "bilingual"])
def test_rewritten_xhtml_declares_utf8_in_spine_and_auxiliary(
    tmp_path, head, attr, expected, bilingual
):
    source, output = tmp_path / "source.epub", tmp_path / "output.epub"
    # These cp1251 bytes are also valid UTF-8 (for "и"). A real declaration wins.
    original = "Рё"
    body = _page(original, head).encode("cp1251")
    extra = _page(RUSSIAN, head).encode("cp1251")
    _write_book(source, body, extra=extra)
    document = read_epub(source, "ru", "zh")
    segment = document.chapters[0].segments[0]
    assert segment.source == original
    segment.target = TARGET
    _export(document, source, output, tmp_path, bilingual=bilingual)

    reopened = read_epub(output, "zh", "ru")
    assert [seg.source for seg in reopened.chapters[0].segments] == (
        [TARGET, original] if bilingual else [TARGET]
    )
    with ZipFile(output) as archive:
        for path, text in [("body.xhtml", TARGET), ("extra.xhtml", RUSSIAN)]:
            page = BeautifulSoup(archive.read(path), "html.parser")
            assert page.meta is not None and page.meta[attr] == expected
            node = page.find(id="text")
            assert node is not None and node.get_text() == text
    with ZipFile(source) as archive:
        assert archive.read("body.xhtml") == body
        assert archive.read("extra.xhtml") == extra


def test_rewrite_updates_only_real_encoding_declarations(tmp_path):
    source, output = tmp_path / "source.epub", tmp_path / "output.epub"
    decl = "<?xml version='1.0' encoding='windows-1251' standalone='yes'?>"
    stylesheet_pi = '<?xml-stylesheet href="sheet.css" encoding="windows-1252"?>'
    comment = f"<!-- {FAKE_XML} -->"
    pi_like_comment = "<!--" + FAKE_XML[1:-1] + "-->"
    script = f"<script>const sample = '{FAKE_XML}';</script>"
    unrelated = '<meta name="sample" content="text/plain; charset=windows-1252"/>'
    page = decl + stylesheet_pi + _page(RUSSIAN, comment + pi_like_comment + script + unrelated)
    _write_book(
        source, page.encode("cp1251"), package=PACKAGE.replace("<metadata>", "<metadata>" + comment)
    )
    document = read_epub(source, "ru", "zh")
    document.chapters[0].segments[0].target = TARGET
    _export(document, source, output, tmp_path)

    with ZipFile(output) as archive:
        result = archive.read("body.xhtml").decode()
        assert result.startswith(decl.replace("windows-1251", "utf-8"))
        assert stylesheet_pi in result
        assert comment in result
        assert pi_like_comment in result
        assert script in result
        meta = BeautifulSoup(result, "html.parser").find("meta", attrs={"name": "sample"})
        assert meta is not None and meta["content"] == "text/plain; charset=windows-1252"
        assert comment in archive.read("book.opf").decode()


@pytest.mark.parametrize(
    "head",
    [
        FAKE_META,
        "<script>const sample = '<meta charset=\"windows-1252\">';</script>",
        """<link title='<meta charset="windows-1252">'/>""",
        '<meta name="sample" content="text/html; charset=windows-1252"/>',
        f"<!-- {FAKE_XML} -->",
    ],
    ids=["comment", "script", "attribute", "non-pragma-meta", "commented-xml"],
)
def test_utf8_sniffing_ignores_non_declarations(tmp_path, head):
    source = tmp_path / "source.epub"
    _write_book(source, _page(RUSSIAN, head).encode())
    assert read_epub(source, "ru", "zh").chapters[0].segments[0].source == RUSSIAN


@pytest.mark.parametrize("out_format", ["epub", "html"])
def test_commented_meta_does_not_reject_old_saved_state(tmp_path, monkeypatch, out_format):
    source, output = tmp_path / "source.epub", tmp_path / f"output.{out_format}"
    raw = _page(RUSSIAN, FAKE_META).encode()
    _write_book(source, raw)
    # Reproduce the legacy Python decode and annotation, without current native ingest.
    markup = UnicodeDammit(raw).unicode_markup
    assert markup is not None
    with monkeypatch.context() as patch:
        patch.setattr(epub_dom, "xhtml_tokens", None)
        title, segments, _ = annotate_epub_resource(markup, 0, "body.xhtml")
    assert [segment.source for segment in segments] == [RUSSIAN]
    segments[0].meta.pop(INLINE_META_KEY, None)
    segments[0].target = TARGET
    document = Document(
        title="Старая книга",
        source_lang="ru",
        target_lang="zh",
        fmt="epub",
        source_path=str(source),
        chapters=[Chapter(index=0, title=title, href="body.xhtml", segments=segments)],
        meta={
            "epub_schema": 5,
            "epub_objects": 1,
            "opf_path": "book.opf",
            "epub_resources": [{"index": 0, "href": "body.xhtml"}],
        },
    )
    _export(document, source, output, tmp_path, bilingual=True)
    if out_format == "epub":
        assert [s.source for s in read_epub(output, "zh", "ru").chapters[0].segments] == [
            TARGET,
            RUSSIAN,
        ]
    else:
        page = BeautifulSoup(output.read_text(encoding="utf-8"), "html.parser")
        target, source_node = page.find(id="text"), page.select_one(".tn-source")
        assert target is not None and target.get_text() == TARGET
        assert source_node is not None and source_node.get_text() == RUSSIAN


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be"])
@pytest.mark.parametrize(
    "decl", ['<?xml version="1.0" encoding="UTF-16"?>', "\n "], ids=["decl", "no-decl"]
)
def test_package_xml_reads_and_exports_bomless_utf16(tmp_path, encoding, decl):
    source, output = tmp_path / "source.epub", tmp_path / "output.epub"
    container, package = [(decl + xml).encode(encoding) for xml in (CONTAINER, PACKAGE)]
    expected = ET.fromstring(package).findtext(".//{http://purl.org/dc/elements/1.1/}title")
    assert expected
    rootfile = ET.fromstring(container).find("rootfile")
    assert rootfile is not None and rootfile.get("full-path") == "book.opf"
    _write_book(source, _page(RUSSIAN), container=container, package=package)

    assert peek_epub_title(source) == expected
    document = read_epub(source, "ru", "zh")
    assert document.title == expected
    document.chapters[0].segments[0].target = TARGET
    _export(document, source, output, tmp_path)
    assert read_epub(output, "zh", "ru").chapters[0].segments[0].source == TARGET
    with ZipFile(output) as archive:
        opf = archive.read("book.opf")
        if decl.startswith("<?xml"):
            assert opf.startswith(decl.replace("UTF-16", "utf-8").encode())
        language = ET.fromstring(opf).findtext(".//{http://purl.org/dc/elements/1.1/}language")
        assert language == "zh-Hans"
        assert archive.read("META-INF/container.xml") == container


@pytest.mark.parametrize(
    "meta", [FAKE_META, '<meta charset="windows-1252"/>'], ids=["comment", "element"]
)
def test_package_xml_does_not_use_html_meta_encoding(tmp_path, meta):
    source = tmp_path / "source.epub"
    package = PACKAGE.replace("<metadata>", "<metadata>" + meta)
    _write_book(source, _page(RUSSIAN), package=package)
    assert peek_epub_title(source) == "Старая книга"
