"""Differential coverage for standalone HTML and cached MinerU HTML."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from pypdf import PdfWriter
from wenyi_core.assemble.html_renderer import _render_plain_native, _render_segments_html
from wenyi_core.assemble.html_resources import _embed_html_resources
from wenyi_core.assemble.html_writer import _html_presentation
from wenyi_core.assemble.writer import assemble
from wenyi_core.ingest import pdf_to_html
from wenyi_core.ingest.html_reader import _extract_chapter, annotate_html_document, read_html
from wenyi_core.ingest.pdf_reader import pdf_cache_html_path, read_pdf
from wenyi_core.ingest.source_hash import source_sha256
from wenyi_core.markup import _epub_native
from wenyi_core.markup.html import html_translation_targets
from wenyi_core.markup.segments import _normalize_html_text, annotate_epub_resource
from wenyi_core.models.document import Segment
from wenyi_core.models.errors import MinerUError
from wenyi_core.storage.file import FileStorage


@pytest.mark.parametrize("offsets", [[], [-2, 0, 1, 3, 4, 5, 7, 14, 999]])
def test_normalization_keeps_python_character_offsets(offsets):
    raw = "\x1c\u3000\u00a0\t💬 \n ě e\u0301\r\n字\u2028"
    assert _epub_native.html_normalize_text(raw, offsets) == _normalize_html_text(raw, offsets)


def test_region_plan_preserves_nested_barriers_and_ruby():
    soup = BeautifulSoup(
        'Front <!-- kept --> <main>First <span>outer <span style="display:block">Block</span>'
        "outer tail</span> Next <ruby>基<rp>(</rp><rt>き</rt><rp>)</rp></ruby>"
        '<span style="display:none!important">Hidden</span>Last<br/></main>',
        "html.parser",
    )
    assert [node.decode_contents() for node in html_translation_targets(soup)] == [
        "Front <!-- kept --> ",
        "First ",
        "outer ",
        "Block",
        "outer tail",
        " Next <ruby>基<rp>(</rp><rt>き</rt><rp>)</rp></ruby>",
        "Last",
    ]


@pytest.mark.parametrize(
    "fragment",
    [
        "<h1>Title<br/>Second line</h1><p>  💬 &amp; words</p>",
        '<div class="page"><p id="p1">First</p><ol><li>Second</li></ol></div>',
        "<div>Header <table><tr><td>Cell<br/>next</td><td>last</td></tr></table></div>",
        '<section><div>Loose<br/>Lines</div><p hidden="">MinerU is block mode.</p></section>',
        "<p>Lead <strong>Bold <em>and italic</em></strong> text.</p>",
        '<div><figure><img src="figure.png"/><figcaption>Caption</figcaption></figure>'
        '<p><img src="before.png"/>lead <span><img src="inside.png"/></span>'
        ' tail<img src="after.png"/></p></div>',
        "<section><p> </p></section>",
    ],
)
def test_mineru_native_plain_templates_match_legacy(fragment):
    assert _epub_native.mineru_annotate_plain(fragment, 3) is not None
    title, expected, template = annotate_epub_resource(fragment, 3, "html-chapter-3.xhtml")
    for segment in expected:
        segment.resource_href = None
    assert _extract_chapter(fragment, 3, preserve_inline_markup=False) == (
        title,
        expected,
        template,
    )


def test_rich_fragments_keep_legacy_annotation_and_rendering():
    fragment = '<p id="start">💬 <a href="#fn1">label <sup>1</sup></a></p><p id="fn1">Note.</p>'
    assert _epub_native.mineru_annotate_plain(fragment, 0) is None
    title, expected, parsed_template = annotate_epub_resource(fragment, 0, "html-chapter-0.xhtml")
    for segment in expected:
        segment.resource_href = None
    assert _extract_chapter(fragment, 0, preserve_inline_markup=False) == (
        title,
        expected,
        parsed_template,
    )

    doc, template = annotate_html_document(fragment, "example.html", "en", "zh")
    segments = doc.chapters[0].segments
    segments[0].target = "翻译"
    assert _render_plain_native(template, segments, None) is None
    assert _render_segments_html(template, segments, prefer_native=True) == _render_segments_html(
        template, segments
    )


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("First line\nSecond line", "<p>First line<br/>Second line</p>"),
        (
            "\r\nFirst & <second>\r\n\rLast\n",
            "<p><br/>First &amp; &lt;second&gt;<br/><br/>Last<br/></p>",
        ),
        ("Original", "<p>Original</p>"),
    ],
)
@pytest.mark.parametrize("prefer_native", [False, True])
def test_plain_backfill_keeps_legacy_line_breaks(target, expected, prefer_native):
    _, segments, template = _extract_chapter("<p>Original</p>", 0, preserve_inline_markup=False)
    assert not segments[0].meta
    segments[0].target = target
    if prefer_native:
        assert _render_plain_native(template, segments, None) == expected
    assert _render_segments_html(template, segments, prefer_native=prefer_native) == expected


def test_plain_backfill_preserves_processing_instruction():
    segment = Segment(index=0, source="First word", target="New\nwords", anchor="p")
    template = '<p data-tn-id="p">First<?sample value?> word</p>'
    expected = "<?sample value?><p>New<br/>words</p>"
    assert _render_segments_html(template, [segment]) == expected
    assert _render_segments_html(template, [segment], prefer_native=True) == expected


@pytest.mark.parametrize("html_mode", [True, False])
def test_plain_backfill_matches_legacy_continuations_edges_and_attributes(html_mode):
    raw = (
        '<div class="layout" style="display:block"> Before <code>fixed</code> After </div>'
        '<p title="Summary">Para<br/>second</p><input type="submit" value="Send"/>'
    )
    if not html_mode:
        raw = '<div class="page"><p>Before<br/>After</p><p>Third</p></div>'
    doc, template = annotate_html_document(
        raw, "example.html", "en", "zh", preserve_inline_markup=html_mode
    )
    segments = doc.chapters[0].segments
    first = segments[0]
    continuation = first.model_copy(
        update={"cont": True, "anchor": None, "source": first.source[2:], "target": "尾"}
    )
    first.source = first.source[:2]
    first.target = "新\r\n行"
    segments.insert(1, continuation)
    segments[2].target = ""  # A saved blank follows the existing export fallback.
    segments[-1].target = "字 & '\" < >"
    legacy = _render_segments_html(template, segments)
    assert _render_plain_native(template, segments, None) == legacy
    assert _render_segments_html(template, segments, prefer_native=True) == legacy
    assert segments[2].target == ""


def test_unrecognized_saved_backfill_metadata_uses_the_legacy_renderer():
    doc, template = annotate_html_document("<p>Source</p>", "example.html", "en", "zh")
    segments = doc.chapters[0].segments
    segments[0].meta = {"html_markup": {}}
    segments[0].target = "首\n尾"
    assert _render_plain_native(template, segments, None) is None
    assert _render_segments_html(template, segments, prefer_native=True) == _render_segments_html(
        template, segments
    )


def test_document_shell_and_media_match_legacy(tmp_path):
    markup = (
        '<!DOCTYPE html><html xml:lang="en"><head><style>p{color:red}</style>'
        '<title>T</title><meta charset="latin-1"/><meta http-equiv="CONTENT-TYPE"/>'
        '<meta name="viewport" content="width=device-width"/></head><body>'
        '<img src="a%20b.png?version=1" srcset="data:image/png;base64,AAA 1x, a%20b.png 2x"/>'
        '<video src="//offline.invalid/movie.mp4" poster="a%20b.png"></video>'
        '<img src="../not-in-root.png"/>'
        '<object data="missing.pdf"></object></body></html>'
    )
    (tmp_path / "a b.png").write_bytes(b"\x12\xff")
    options = {"bilingual": True, "preserve_source_style": False}
    canonical = str(BeautifulSoup(markup, "html.parser"))
    assert _epub_native.html_present(canonical, "zh", "", "") is not None
    legacy = _html_presentation(canonical, "zh", **options)
    assert _html_presentation(canonical, "zh", prefer_native=True, **options) == legacy
    assert _epub_native.html_inline_media(legacy, lambda _: None) is not None
    native = _embed_html_resources(legacy, source_dir=str(tmp_path), prefer_native=True)
    assert native == _embed_html_resources(legacy, source_dir=str(tmp_path))
    assert native.count("data:image/png;base64,Ev8=") == 3
    assert 'src="//offline.invalid/movie.mp4"' in native
    assert 'src="../not-in-root.png"' in native
    assert 'data="missing.pdf"' in native


def test_noncanonical_svg_media_uses_the_compatibility_renderer(tmp_path):
    (tmp_path / "copy.png").write_bytes(b"media")
    markup = '<svg><image href="copy.png" xlink:href="#local"/></svg>'
    assert _epub_native.html_inline_media(markup, lambda _: None) is None
    assert _embed_html_resources(
        markup, source_dir=str(tmp_path), prefer_native=True
    ) == _embed_html_resources(markup, source_dir=str(tmp_path))


@pytest.mark.parametrize("preserve_inline_markup", [False, True])
def test_read_html_keeps_chapter_templates_without_rebuilding_shell(
    tmp_path, preserve_inline_markup
):
    source = tmp_path / "chapter.html"
    source.write_text(
        "<!DOCTYPE html><html><head><title>Metadata</title></head><body>"
        '<img src="cover.png"/><h1>Title</h1><p>Paragraph.</p></body></html>',
        encoding="utf-8",
    )
    options = {"preserve_inline_markup": preserve_inline_markup}
    document = read_html(str(source), "en", "zh", **options)
    expected, _ = annotate_html_document(source.read_text(), str(source), "en", "zh", **options)
    assert document == expected
    assert document.chapters[0].template == '<img src="cover.png"/>'
    assert document.chapters[0].segments == []
    assert document.chapters[1].template == (
        '<h1 data-tn-id="tn1_0">Title</h1><p data-tn-id="tn1_1">Paragraph.</p>'
    )


def _zip(*members: tuple[str, str | bytes], compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression) as archive:
        for name, payload in members:
            archive.writestr(name, payload)
    return output.getvalue()


@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_mineru_standard_zip_selects_first_case_sensitive_html(monkeypatch, compression):
    members = (("SKIP.HTML", "wrong"), ("nested/输出.htm", "first"), ("other.html", "second"))
    payload = _zip(*members, compression=compression)
    assert _epub_native.mineru_html_from_zip(payload) == "first"
    monkeypatch.setattr(
        zipfile, "ZipFile", lambda *_, **__: pytest.fail("standard ZIP should stay native")
    )
    assert pdf_to_html._html_from_zip(payload) == "first"


def test_mineru_response_zip_compatibility_and_errors_are_unchanged():
    with pytest.warns(UserWarning, match="Duplicate name"):
        duplicate = _zip(("output.html", "old"), ("other.html", "second"), ("output.html", "new"))
    assert _epub_native.mineru_html_from_zip(duplicate) is None
    assert pdf_to_html._html_from_zip(duplicate) == "new"
    bzip = _zip(("output.html", "other codec"), compression=zipfile.ZIP_BZIP2)
    assert _epub_native.mineru_html_from_zip(bzip) is None
    assert pdf_to_html._html_from_zip(bzip) == "other codec"
    prefixed = b"prefix" + _zip(("output.html", "prefixed"))
    assert _epub_native.mineru_html_from_zip(prefixed) is None
    assert pdf_to_html._html_from_zip(prefixed) == "prefixed"
    with pytest.raises(MinerUError, match="No HTML file in result ZIP"):
        pdf_to_html._html_from_zip(_zip(("plain.txt", "no HTML")))
    with pytest.raises(UnicodeDecodeError):
        pdf_to_html._html_from_zip(_zip(("output.html", b"\xff")))
    with pytest.raises(zipfile.BadZipFile, match="Bad CRC"):
        corrupt = _zip(("output.html", "correct original"), compression=zipfile.ZIP_STORED)
        pdf_to_html._html_from_zip(corrupt.replace(b"correct original", b"corrupt original"))
    mismatch = _zip(("output.html", "content")).replace(b"output.html", b"wrong_.html", 1)
    assert _epub_native.mineru_html_from_zip(mismatch) is None
    with pytest.raises(zipfile.BadZipFile, match="differ"):
        pdf_to_html._html_from_zip(mismatch)


@pytest.mark.parametrize("source_format", ["html", "mineru"])
@pytest.mark.parametrize("bilingual", [False, True])
def test_html_templates_to_epub_backfill_canonical_paragraphs_natively(
    tmp_path, monkeypatch, source_format, bilingual
):
    store = FileStorage(str(tmp_path / "state"))
    source = tmp_path / ("source.html" if source_format == "html" else "source.pdf")
    if source_format == "html":
        html = source
    else:
        with PdfWriter() as writer:
            writer.add_blank_page(width=144, height=144)
            writer.write(source)
        html = Path(pdf_cache_html_path(store.source_dir, source_sha256(str(source))))
        html.parent.mkdir(parents=True)
    html.write_text('<p id="greeting">Greeting</p>', encoding="utf-8")
    doc = (
        read_html(str(source), "en", "zh")
        if source_format == "html"
        else read_pdf(
            str(source),
            "en",
            "zh",
            cache_dir=store.source_dir,
            mineru_token_resolver=lambda: pytest.fail("cached HTML must not resolve credentials"),
        )
    )
    store.init_from_document(doc)
    chapter = store.load_chapter(0)
    chapter.segments[0].target = "First line\nSecond line"
    store.save_chapter(chapter)

    function = "html_backfill_paired" if bilingual else "html_backfill_plain"
    native = getattr(_epub_native, function)
    calls = []

    def track(*args):
        result = native(*args)
        calls.append(result)
        return result

    monkeypatch.setattr(_epub_native, function, track)
    out = tmp_path / "output.epub"
    assemble(store, str(source), out_path=str(out), out_format="epub", bilingual=bilingual)
    with zipfile.ZipFile(out) as archive:
        section = next(name for name in archive.namelist() if name.endswith("/ch0.xhtml"))
        rendered = archive.read(section).decode()
    golden = '<p id="greeting">First line<br/>Second line</p>'
    assert golden in rendered
    soup = BeautifulSoup(rendered, "html.parser")
    assert len(calls) == 1 and golden in calls[0]
    if bilingual:
        node = soup.select_one(".tn-source")
        assert node is not None and node.get_text() == "Greeting"
    else:
        assert calls == [golden]
        assert not soup.select(".tn-source")
    assert store.load_chapter(0) == chapter
    assert html.read_text() == '<p id="greeting">Greeting</p>'


def test_mineru_chunk_assembly_cache_and_html_export(tmp_path, monkeypatch):
    source = tmp_path / "Book.pdf"
    with PdfWriter() as writer:
        writer.add_blank_page(width=144, height=144)
        writer.add_blank_page(width=144, height=144)
        writer.write(source)
    store = FileStorage(str(tmp_path / "state"))
    cache = Path(pdf_cache_html_path(store.source_dir, source_sha256(str(source))))
    calls = []

    class OfflineApi:
        def __init__(self, token):
            assert token == "synthetic"

        def submit_batch(self, paths):
            calls.append(len(paths))
            return [
                _zip(
                    (
                        "parts/first.html",
                        "<HTML><HEAD><TITLE>old</TITLE><META CHARSET='x'/>"
                        "<style>p { color: red }</style></HEAD>"
                        "<BODY><h1>One</h1><p id='p1'>First.</p></BODY></HTML>",
                    )
                ),
                _zip(("second.htm", "<h2>Two</h2><p id='p2'>Second.</p>")),
            ]

        def close(self):
            pass

    monkeypatch.setattr(pdf_to_html, "MinerUApi", OfflineApi)
    monkeypatch.setattr(pdf_to_html, "MAX_PAGES", 1)

    def read(**kwargs):
        return read_pdf(str(source), "en", "zh", cache_dir=store.source_dir, **kwargs)

    original = read(api_token="synthetic")
    frozen = (
        '<!DOCTYPE html>\n<html>\n<head>\n    <meta charset="utf-8">\n'
        "    <title>Book.pdf</title>\n<style>p { color: red }</style>\n"
        "</head>\n<body>\n<h1>One</h1><p id='p1'>First.</p>"
        "<h2>Two</h2><p id='p2'>Second.</p>\n</body>\n</html>"
    )
    assert cache.read_text() == frozen
    assert (
        original.model_dump()
        == read(
            mineru_token_resolver=lambda: pytest.fail("cached HTML must not resolve credentials")
        ).model_dump()
    )
    assert calls == [2]
    assert original.fmt == "pdf"
    assert original.source_path == str(source)
    assert [s.anchor for ch in original.chapters for s in ch.segments] == [
        "tn0_0",
        "tn0_1",
        "tn1_0",
        "tn1_1",
    ]
    store.init_from_document(original)
    for chapter in original.chapters:
        chapter.segments[0].target = f"Chapter {chapter.index}"
        chapter.segments[1].target = "" if chapter.index == 0 else "译文"
        store.save_chapter(chapter)
    out = tmp_path / "output.html"
    assemble(store, str(source), out_path=str(out), out_format="html")
    soup = BeautifulSoup(out.read_text(), "html.parser")
    p1, p2 = soup.find(id="p1"), soup.find(id="p2")
    assert p1 is not None and p1.get_text() == ""
    assert p2 is not None and p2.get_text() == "译文"
    assert not soup.select("[data-tn-id]")
    assert store.load_chapter(0).segments[1].target == ""
    assert cache.read_text() == frozen
