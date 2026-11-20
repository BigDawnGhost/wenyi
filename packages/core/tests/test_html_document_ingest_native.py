"""Offline parity checks for full-document standalone HTML ingest."""

from __future__ import annotations

import pytest
from wenyi_core.assemble.writer import assemble
from wenyi_core.ingest import html_reader
from wenyi_core.markup import _epub_native
from wenyi_core.models.document import Segment
from wenyi_core.storage.file import FileStorage

_BOOK = (
    '<!DOCTYPE html>\n<html lang="en">\n'
    '<head><meta charset="utf-8"/><title>Document title</title></head>\n'
    '<body dir="ltr">\n<p id="front">Front &amp; &lt;matter&gt;.</p>\n'
    "<h1>One\u00a0 Two</h1>\n<h2>Sub\n title</h2>\n"
    "<p>  Hello\t💬  世界  </p>\n"
    "<h3>Next</h3><p>Last.</p>\n</body>\n</html>\n"
)


def _legacy(monkeypatch, markup, path, **options):
    with monkeypatch.context() as mock:
        mock.setattr(_epub_native, "html_annotate_document", lambda *_: None)
        return html_reader.annotate_html_document(markup, str(path), "en", "zh", **options)


def _unexpected_reparse(*args, **kwargs):
    pytest.fail("canonical HTML ingest should not use BeautifulSoup")


def test_canonical_document_uses_one_native_tree_for_ingest_and_annotation(tmp_path, monkeypatch):
    path = tmp_path / "chapter book.html"
    path.write_bytes(_BOOK.replace("\n", "\r\n").encode("utf-8"))
    expected, legacy_template = _legacy(monkeypatch, path.read_text(), path)
    monkeypatch.setattr(html_reader, "html_document_soup", _unexpected_reparse)
    monkeypatch.setattr(html_reader, "BeautifulSoup", _unexpected_reparse)
    monkeypatch.setattr(html_reader, "annotate_epub_resource", _unexpected_reparse)

    document = html_reader.read_html(str(path), "en", "zh")
    native, template = html_reader.annotate_html_document(
        path.read_bytes().decode("utf-8"), str(path), "en", "zh"
    )
    assert document == native == expected
    assert document.title == "chapter book"
    assert [(ch.index, ch.title) for ch in document.chapters] == [
        (0, ""),
        (1, "One Two / Sub title"),
        (2, "Next"),
    ]
    markup = {"html_markup": {"version": 1}}
    assert document.chapters[1].segments == [
        Segment(index=0, source="One\u00a0 Two", kind="heading", anchor="tn1_0", meta=markup),
        Segment(index=1, source="Sub title", kind="heading", anchor="tn1_1", meta=markup),
        Segment(index=2, source="Hello 💬 世界", anchor="tn1_2", meta=markup),
    ]
    assert document.chapters[0].template == (
        '\n<p data-tn-id="tn0_0" id="front">Front &amp; &lt;matter&gt;.</p>\n'
    )
    assert document.meta["html_attrs"] == {"lang": "en"}
    assert document.meta["body_attrs"] == {"dir": "ltr"}
    assert document.meta["head_html"] == '<meta charset="utf-8"/><title>Document title</title>\n\n'
    golden_template = (
        '<!DOCTYPE html>\n\n<html lang="en">'
        '<head><meta charset="utf-8"/><title>Document title</title>\n\n</head>'
        '<body dir="ltr">\n<p data-tn-id="tn0_0" id="front">Front &amp; &lt;matter&gt;.</p>\n'
        '<h1 data-tn-id="tn1_0">One\u00a0 Two</h1>\n<h2 data-tn-id="tn1_1">Sub\n title</h2>\n'
        '<p data-tn-id="tn1_2">  Hello\t💬  世界  </p>\n'
        '<h3 data-tn-id="tn2_0">Next</h3><p data-tn-id="tn2_1">Last.</p>\n</body></html>\n'
    )
    assert template == legacy_template == golden_template


@pytest.mark.parametrize("tags", [None, frozenset({"h2"}), frozenset({"p"})])
@pytest.mark.parametrize(
    "body",
    [
        "",
        " ",
        "<h2></h2>",
        "<h1>Title</h1><p>First</p><h2> </h2><p></p><h2>Later</h2><p>Second</p><h2></h2>",
    ],
)
def test_chapter_policy_and_empty_chapter_templates_match_legacy(tmp_path, monkeypatch, tags, body):
    path = tmp_path / "book.html"
    markup = f"<html><head></head><body>{body}</body></html>"
    expected = _legacy(monkeypatch, markup, path, chapter_tags=tags)
    monkeypatch.setattr(html_reader, "html_document_soup", _unexpected_reparse)
    assert html_reader.annotate_html_document(markup, str(path), "en", "zh", chapter_tags=tags) == (
        expected
    )


@pytest.mark.parametrize(
    "markup",
    [
        "<p>Optional shell</p>",
        "<HTML><HEAD></HEAD><BODY><P>Noncanonical case</P></BODY></HTML>",
        "<html><head></head><body><p>Malformed</body></html>",
        "<html><head></head><body><p>One <em>span</em>.</p></body></html>",
        "<html><head></head><body><p>One<br/>two.</p></body></html>",
        '<html><head></head><body><p title="Translate me">Prose.</p></body></html>',
        '<html><head></head><body><p translate="no">Protected.</p></body></html>',
        "<html><head></head><body>Loose prose<p>Paragraph.</p></body></html>",
        "<html><head></head><body><p>Prose &nbsp; entity.</p></body></html>",
        "<html><head></head><body><!-- Keep --><p>Prose.</p></body></html>",
    ],
)
def test_unproven_documents_use_compatibility_parser(tmp_path, monkeypatch, markup):
    path = tmp_path / "book.html"
    assert _epub_native.html_annotate_document(markup, ["h1", "h2"], True) is None
    expected = _legacy(monkeypatch, markup, path)
    assert html_reader.annotate_html_document(markup, str(path), "en", "zh") == expected


def test_source_backfill_accepts_chapters_saved_by_legacy_parser(tmp_path, monkeypatch):
    source = tmp_path / "book.html"
    source.write_text(_BOOK, encoding="utf-8")
    document, _ = _legacy(monkeypatch, _BOOK, source)
    store = FileStorage(str(tmp_path / "state"))
    store.init_from_document(document)
    chapter = store.load_chapter(1)
    chapter.segments[-1].target = "First line\nSecond line"
    store.save_chapter(chapter)
    monkeypatch.setattr(html_reader, "html_document_soup", _unexpected_reparse)

    output = tmp_path / "out.html"
    assemble(store, str(source), out_path=str(output), out_format="html", about_page=False)
    rendered = output.read_text(encoding="utf-8")
    assert "<p>First line<br/>Second line</p>" in rendered
    assert '<p id="front">Front &amp; &lt;matter&gt;.</p>' in rendered
    assert "data-tn-id" not in rendered
    assert store.load_chapter(1) == chapter
