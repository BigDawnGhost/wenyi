"""Golden parity for the native subset of standalone HTML backfill."""

from __future__ import annotations

import pytest
from bs4 import BeautifulSoup
from wenyi_core.alignment import target_digest
from wenyi_core.assemble import html_renderer
from wenyi_core.assemble.writer import assemble
from wenyi_core.ingest.html_reader import annotate_html_document, read_html
from wenyi_core.markup import _epub_native
from wenyi_core.markup.contracts import ANNOTATION_META_KEY, HTML_ATTRIBUTE_META_KEY
from wenyi_core.models.document import Segment
from wenyi_core.storage.file import FileStorage


def _annotate(html: str) -> tuple[str, list[Segment]]:
    doc, template = annotate_html_document(html, "source.html", "en", "zh")
    return template, [s for ch in doc.chapters for s in ch.segments]


def _align(segment: Segment, spans: list[tuple[int, int]]) -> None:
    ann = segment.meta[ANNOTATION_META_KEY]
    assert segment.target is not None
    ann.update(
        target_digest=target_digest(segment.target),
        placements=[
            {
                "id": item["id"],
                "mode": item["mode"],
                "target_start": start,
                "target_end": end,
                "status": "aligned",
                "method": "copied_markers",
            }
            for item, (start, end) in zip(ann["items"], spans, strict=True)
        ],
    )


def _watch_native(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    native = _epub_native.html_backfill_paired
    results = []

    def wrapper(*args):
        result = native(*args)
        results.append(result)
        return result

    monkeypatch.setattr(_epub_native, "html_backfill_paired", wrapper)
    return results


def _no_python_backfill(*args, **kwargs):
    pytest.fail("eligible HTML backfill fell through to the Python DOM")


def _force_legacy_html(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("html_backfill_paired", "html_present", "html_inline_media"):
        monkeypatch.setattr(_epub_native, name, lambda *args: None)


_HTML = (
    "<!DOCTYPE html><html><head><style>p { color: red; }</style></head><body>"
    "<h1>Heading</h1>"
    '<p class="original old" id="main" lang="fr" name="named" '
    'style="color:blue" xml:lang="fr">Before <strong>bold</strong> after</p>'
    '<div xml:lang="und">Nested</div>'
    "<div> First <code>fixed</code> Last </div>"
    '<img src="detail.png" alt="Detail"/><p>Fallback</p>'
    '<div id="tn-source-tn0_1"><!-- reserves the first source ID --></div>'
    "</body></html>"
)


@pytest.mark.parametrize("order", ["source_first", "target_first"])
@pytest.mark.parametrize("preserve_style", [False, True])
def test_standalone_export_uses_native_and_matches_old_dom(
    tmp_path, monkeypatch, order, preserve_style
):
    source = tmp_path / "source.html"
    source.write_text(_HTML, encoding="utf-8")
    (tmp_path / "detail.png").write_bytes(b"image")
    document = read_html(str(source), "en", "zh")
    store = FileStorage(str(tmp_path / "state"))
    store.init_from_document(document)
    for chapter in document.chapters:
        for segment in chapter.segments:
            segment.target = {
                "Before bold after": "新🌎\r\n尾",
                "Fallback": "",
                "First": "先",
                "Last": "後",
                "Detail": '細節 & "Alt"',
            }.get(segment.source, "譯")
            if ANNOTATION_META_KEY in segment.meta:
                _align(segment, [(1, 2)])
        store.save_chapter(chapter)

    def export(name: str) -> str:
        output = tmp_path / name
        assemble(
            store,
            str(source),
            out_path=str(output),
            out_format="html",
            bilingual=True,
            order=order,
            preserve_source_style=preserve_style,
        )
        return output.read_text(encoding="utf-8")

    with monkeypatch.context() as legacy:
        _force_legacy_html(legacy)
        expected = export("old.html")
    native = _watch_native(monkeypatch)
    monkeypatch.setattr(html_renderer, "_render_segments_dom", _no_python_backfill)
    output = export("native.html")
    assert output == expected
    assert len(native) == 1 and native[0] is not None
    soup = BeautifulSoup(output, "html.parser")
    main = soup.find(id="main")
    assert main is not None and main.decode_contents() == "新<strong>🌎</strong><br/>尾"
    pair = soup.find(id="tn-source-tn0_1-2")
    assert pair is not None
    assert pair.decode_contents() == "Before <strong>bold</strong> after"
    assert pair["name"] == pair["id"]
    assert pair["lang"] == pair["xml:lang"] == "fr"
    neighbor = pair.find_next_sibling() if order == "source_first" else pair.find_previous_sibling()
    assert neighbor is not None and neighbor["id"] == "main"
    assert pair.get("style") == ("color:blue" if preserve_style else None)
    assert pair["class"][0] == ("original" if preserve_style else "tn-source")
    assert bool(soup.find(id="tn-bilingual-style")) == (not preserve_style)
    inherited = soup.select_one("div[xml\\:lang=und] span.tn-source")
    assert inherited is not None and inherited["xml:lang"] == "en"
    assert " First " in output and " Last " in output and "<code>fixed</code>" in output
    assert soup.img is not None and soup.img["alt"] == '細節 & "Alt"'
    assert soup.img["src"] == "data:image/png;base64,aW1hZ2U="
    assert not soup.select_one(".tn-source:-soup-contains(Fallback, Heading)")
    assert "data-tn-" not in output
    assert any(s.source == "Fallback" and s.target == "" for s in store.load_chapter(0).segments)


@pytest.mark.parametrize("order", ["source_first", "target_first"])
def test_html_uses_native_with_media_and_nested_pairs(tmp_path, monkeypatch, order):
    source = tmp_path / "sample.html"
    store = FileStorage(str(tmp_path / "state"))
    source.write_text(
        '<html><body><h1>Heading</h1><p id="media"><img src="edge.png"/>Left '
        '<img src="inside.png"/> right</p><ul><li class="entry">Item</li></ul>'
        "<blockquote>Quote</blockquote><p>Untranslated</p></body></html>",
        encoding="utf-8",
    )
    for name in ("edge.png", "inside.png"):
        (source.parent / name).write_bytes(name.encode())
    doc = read_html(str(source), "en", "zh", preserve_inline_markup=False)
    store.init_from_document(doc)
    chapter = doc.chapters[0]
    for segment in chapter.segments:
        segment.target = "" if segment.source == "Untranslated" else "新🌎\n尾"
        if ANNOTATION_META_KEY in segment.meta:
            _align(segment, [(2, 2)])
    store.save_chapter(chapter)

    def export(name: str) -> str:
        output = tmp_path / name
        assemble(
            store,
            str(source),
            out_path=str(output),
            out_format="html",
            bilingual=True,
            order=order,
            preserve_source_style=True,
        )
        return output.read_text(encoding="utf-8")

    with monkeypatch.context() as legacy:
        _force_legacy_html(legacy)
        expected = export("old.html")
    native = _watch_native(monkeypatch)
    monkeypatch.setattr(html_renderer, "_render_segments_dom", _no_python_backfill)
    output = export("native.html")
    assert output == expected
    assert len(native) == 1 and native[0] is not None
    soup = BeautifulSoup(output, "html.parser")
    media = soup.find(id="media")
    assert media is not None and media.get_text() == "新🌎尾"
    assert len(soup.select("p img[src^='data:image/png;base64,']")) == 4
    item, quote = (
        soup.select_one("li > div.entry.tn-source"),
        soup.select_one("blockquote > div.tn-source"),
    )
    assert item is not None and item.get_text() == "Item"
    assert quote is not None and quote.get_text() == "Quote"
    assert not soup.select_one(".tn-source:-soup-contains(Untranslated, Heading)")
    assert "data-tn-" not in output


def test_rich_mono_native_uses_merged_placements_and_continuations(monkeypatch):
    template, segments = _annotate("<p>A <em>word</em> here.</p>")
    segment = segments[0]
    assert segment.anchor is not None
    fresh = {segment.anchor: segment.model_copy(deep=True).meta}
    segment.target = '首\r\n🌎 "&'
    _align(segment, [(4, 5)])
    continuation = segment.model_copy(
        update={"anchor": None, "cont": True, "source": segment.source[4:], "target": ' "&'}
    )
    segment.source = segment.source[:4]
    segment.target = "首\r\n🌎"
    segments.append(continuation)
    expected = html_renderer._render_segments_html(template, segments, render_meta_by_anchor=fresh)

    native = _watch_native(monkeypatch)
    monkeypatch.setattr(html_renderer, "_render_segments_dom", _no_python_backfill)
    assert (
        html_renderer._render_segments_html(
            template, segments, prefer_native=True, render_meta_by_anchor=fresh
        )
        == expected
    )
    assert '<p>首<br/>🌎<em> </em>"&amp;</p>' in expected
    assert native == [expected]


@pytest.mark.parametrize(
    ("raw", "spans"),
    [
        ('<p>Before <strong id="ref">bold</strong> after</p>', [(1, 3)]),
        ('<p><a href="#end">Go</a></p><p id="end">End</p>', [(1, 3)]),
        ("<p>Before <strong>bold <em>nested</em></strong></p>", [(1, 4), (2, 3)]),
        ("<p>Before <em>unplaced</em> after</p>", []),
    ],
)
def test_complex_source_clones_or_placements_retain_python(monkeypatch, raw, spans):
    template, segments = _annotate(raw)
    for segment in segments:
        segment.target = "譯文🌎了"
        if spans and ANNOTATION_META_KEY in segment.meta:
            _align(segment, spans)
    expected = html_renderer._render_segments_html(template, segments, bilingual=True)
    native = _watch_native(monkeypatch)
    assert (
        html_renderer._render_segments_html(template, segments, prefer_native=True, bilingual=True)
        == expected
    )
    assert native == [None]


def test_explicit_source_map_retains_text_only_source_fallback(monkeypatch):
    template = '<p data-tn-id="tn0_0" lang="fr"> A <b>word</b> </p>'
    segments = [Segment(index=0, anchor="tn0_0", source="A word", target="譯文")]
    options = {"bilingual": True, "source_ids_by_anchor": {}, "source_lang": "en"}
    expected = html_renderer._render_segments_html(template, segments, **options)
    native = _watch_native(monkeypatch)
    assert (
        html_renderer._render_segments_html(template, segments, prefer_native=True, **options)
        == expected
    )
    assert native == []
    original = BeautifulSoup(expected, "html.parser").select_one(".tn-source")
    assert original is not None and original.decode_contents() == "A word"


@pytest.mark.parametrize("case", ["stale_digest", "wrong_slot", "noncanonical", "old_markup"])
def test_unvalidated_states_and_trees_retain_python(monkeypatch, case):
    template, segments = _annotate('<p title="Summary">A <em>word</em> here.</p>')
    segments[0].target = "譯文"
    segments[1].target = "摘要"
    _align(segments[0], [(0, 1)])
    if case == "stale_digest":
        segments[0].target = "New target"
    elif case == "wrong_slot":
        segments[1].meta[HTML_ATTRIBUTE_META_KEY]["name"] = "id"
    elif case == "noncanonical":
        template = template.replace("</p>", "")
    else:
        segments[0].meta["html_markup"] = {}
    expected = html_renderer._render_segments_html(template, segments, bilingual=True)
    native = _watch_native(monkeypatch)
    assert (
        html_renderer._render_segments_html(template, segments, prefer_native=True, bilingual=True)
        == expected
    )
    assert native == [None]
