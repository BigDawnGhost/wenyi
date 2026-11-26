"""Differential coverage for standalone HTML."""

from __future__ import annotations

import zipfile

import pytest
from bs4 import BeautifulSoup
from wenyi_core.assemble.html_renderer import _render_plain_native, _render_segments_html
from wenyi_core.assemble.html_resources import _embed_html_resources
from wenyi_core.assemble.html_writer import _html_presentation
from wenyi_core.assemble.writer import assemble
from wenyi_core.ingest.html_reader import _extract_chapter, annotate_html_document, read_html
from wenyi_core.markup import _epub_native
from wenyi_core.markup.html import html_translation_targets
from wenyi_core.markup.segments import _normalize_html_text, annotate_epub_resource
from wenyi_core.models.document import Segment
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
def test_legacy_block_templates_match_epub_extraction(fragment):
    title, expected, template = annotate_epub_resource(
        fragment, 3, "html-chapter-3.xhtml", extract_loose_text=False
    )
    for segment in expected:
        segment.resource_href = None
    assert _extract_chapter(fragment, 3, preserve_inline_markup=False) == (
        title,
        expected,
        template,
    )


def test_rich_fragments_keep_legacy_annotation_and_rendering():
    fragment = '<p id="start">💬 <a href="#fn1">label <sup>1</sup></a></p><p id="fn1">Note.</p>'
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


@pytest.mark.parametrize("bilingual", [False, True])
def test_html_templates_to_epub_backfill_canonical_paragraphs_natively(
    tmp_path, monkeypatch, bilingual
):
    store = FileStorage(str(tmp_path / "state"))
    source = tmp_path / "source.html"
    source.write_text('<p id="greeting">Greeting</p>', encoding="utf-8")
    doc = read_html(str(source), "en", "zh")
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
    assert source.read_text() == '<p id="greeting">Greeting</p>'
