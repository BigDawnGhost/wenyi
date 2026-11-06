"""HTML exports backfill the complete authoritative source, not saved chapter layouts."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from bs4.element import Comment, Tag
from wenyi_core.assemble.writer import assemble
from wenyi_core.ingest.html_reader import read_html
from wenyi_core.ingest.segmenter import split_long_segments
from wenyi_core.storage.file import FileStorage

_IMAGE = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="


def initialized_store(tmp_path: Path, source: Path, *, preserve_inline_markup: bool = True):
    document = read_html(str(source), "en", "zh", preserve_inline_markup=preserve_inline_markup)
    store = FileStorage(str(tmp_path / "state"))
    store.init_from_document(document)
    for chapter in document.chapters:
        for segment in chapter.segments:
            segment.target = f"译{segment.source}"
        store.save_chapter(chapter)
    return store, document


def required(node: object) -> Tag:
    assert isinstance(node, Tag)
    return node


@pytest.mark.parametrize(
    "markup",
    [
        "<!DOCTYPE html><html lang=en>"
        '<meta charset="utf-8"><style id="global">body{font-family:sans-serif}</style>'
        '<title>Original title</title><meta name="viewport" content="width=device-width">'
        '<body class="snapshot"><h1>Chapter</h1><p>Body.</p></body></html>',
        '<style id="global">body{font-family:sans-serif}</style>'
        "<title>Original title</title><p>Body.</p>",
    ],
    ids=["omitted-head", "fragment"],
)
def test_optional_document_shell_preserves_head_metadata(tmp_path, markup):
    source = tmp_path / "page.html"
    source.write_text(markup)
    store, document = initialized_store(tmp_path, source)
    assert "global" in document.meta["head_html"]
    assert "Original title" in document.meta["head_html"]
    assert all(
        "Original title" not in segment.source
        for chapter in document.chapters
        for segment in chapter.segments
    )
    output = tmp_path / "translated.html"
    assemble(store, str(source), out_path=str(output), out_format="html")
    rendered = BeautifulSoup(output.read_text(), "html.parser")
    head = required(rendered.head)
    assert required(head.find("style", id="global")).get_text() == "body{font-family:sans-serif}"
    assert required(head.find("title")).get_text() == "Original title"
    assert required(rendered.find("p")).get_text() == "译Body."
    assert len(rendered.find_all("html")) == 1
    assert len(rendered.find_all("head")) == 1
    assert len(rendered.find_all("body")) == 1


@pytest.mark.parametrize("preserve_inline_markup", [False, True])
def test_saved_state_recovers_source_layout_without_retranslation(tmp_path, preserve_inline_markup):
    source = tmp_path / "page.html"
    source.write_text(
        '<!DOCTYPE html><!-- before --><html class="original-shell"><head id="source-head">'
        '<style id="global">body > p{color:navy}</style><title>Original</title>'
        '</head><body class="original-body"><h1>One</h1><p>First.</p>'
        "<h1>Two</h1><p>Second.</p></body></html><!-- after -->"
    )
    store, document = initialized_store(
        tmp_path, source, preserve_inline_markup=preserve_inline_markup
    )
    manifest = store.load_manifest()
    # Old snapshots may have lost all head content and have no parsing-policy record.
    manifest["meta"].pop("html_parse", None)
    manifest["meta"]["head_html"] = ""
    manifest["meta"]["html_attrs"] = {"class": ["stale-shell"]}
    manifest["meta"]["body_attrs"] = {"class": ["stale-body"]}
    store.save_manifest(manifest)
    for chapter in document.chapters:
        saved = store.load_chapter(chapter.index)
        saved.template = "<p>This stale template must never be exported.</p>"
        store.save_chapter(saved)
    before = [store.load_chapter(chapter.index).model_dump() for chapter in document.chapters]
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    output = tmp_path / "translated.html"
    assemble(store, str(source), out_path=str(output), out_format="html")
    rendered = BeautifulSoup(output.read_text(), "html.parser")
    assert required(rendered.html)["class"] == ["original-shell"]
    assert required(rendered.body)["class"] == ["original-body"]
    assert required(rendered.head)["id"] == "source-head"
    assert required(rendered.head.find("style"))["id"] == "global"
    assert [paragraph.get_text() for paragraph in rendered.find_all("p")] == [
        "译First.",
        "译Second.",
    ]
    assert [str(node).strip() for node in rendered.children if isinstance(node, Comment)] == [
        "before",
        "after",
    ]
    assert "stale" not in output.read_text()
    assert original_hash == hashlib.sha256(source.read_bytes()).hexdigest()
    assert before == [
        store.load_chapter(chapter.index).model_dump() for chapter in document.chapters
    ]


def test_legacy_html_without_body_reuses_original_chapter_anchors(tmp_path):
    source = tmp_path / "page.html"
    source.write_text(
        '<html><head><style id="global">p{color:navy}</style></head>'
        "<h1>One</h1><p>First.</p><h1>Two</h1><p>Second.</p></html>"
    )
    # The original importer did not infer body: headings inside html stayed in one
    # chapter even when the saved boundary policy included h1.
    document = read_html(str(source), "en", "zh", chapter_tags=None, preserve_inline_markup=False)
    document.meta.pop("html_parse")
    document.meta["chapter_tags"] = ["h1"]
    store = FileStorage(str(tmp_path / "state"))
    store.init_from_document(document)
    for chapter in document.chapters:
        for segment in chapter.segments:
            segment.target = f"译{segment.source}"
        store.save_chapter(chapter)
    output = tmp_path / "translated.html"
    assemble(store, str(source), out_path=str(output))
    rendered = BeautifulSoup(output.read_text(), "html.parser")
    assert [paragraph.get_text() for paragraph in rendered.find_all("p")] == [
        "译First.",
        "译Second.",
    ]
    assert required(rendered.head).find("style", id="global") is not None


@pytest.mark.parametrize("bilingual", [False, True])
def test_html_defaults_to_a_single_html_file_with_embedded_local_media(tmp_path, bilingual):
    source = tmp_path / "snapshot.html"
    source.write_text(
        '<html><body><p>Body.</p><img src="cover.png">'
        f'<img id="embedded" src="{_IMAGE}" alt="A dot"></body></html>'
    )
    (tmp_path / "cover.png").write_bytes(b"image")
    store, _ = initialized_store(tmp_path, source)
    output = Path(assemble(store, str(source), bilingual=bilingual))
    assert output.name == f"snapshot.zh{'-bi' if bilingual else ''}.html"
    assert output.parent == tmp_path / "output"
    rendered = BeautifulSoup(output.read_text(), "html.parser")
    assert required(rendered.find("img"))["src"] == "data:image/png;base64,aW1hZ2U="
    assert required(rendered.find("img", id="embedded"))["src"] == _IMAGE
    assert required(rendered.find("img", id="embedded"))["alt"] == "译A dot"
    assert required(rendered.find("p", class_=False)).get_text() == "译Body."
    assert not output.with_suffix(".assets").exists()


@pytest.mark.parametrize("data_first", [False, True])
def test_single_file_export_embeds_local_and_embedded_srcset_candidates(tmp_path, data_first):
    source = tmp_path / "snapshot.html"
    candidates = [f"{_IMAGE} 1x", "cover.png 2x"]
    if not data_first:
        candidates.reverse()
    source.write_text(f'<p>Body.</p><img srcset="{", ".join(candidates)}">')
    (tmp_path / "cover.png").write_bytes(b"image")
    store, _ = initialized_store(tmp_path, source)
    output = Path(assemble(store, str(source)))
    rendered = BeautifulSoup(output.read_text(), "html.parser")
    srcset = required(rendered.find("img"))["srcset"]
    assert "cover.png" not in srcset
    assert f"{_IMAGE} 1x" in srcset
    assert "data:image/png;base64,aW1hZ2U= 2x" in srcset


@pytest.mark.parametrize("with_hash", [False, True])
def test_changed_source_is_rejected_before_replacing_an_export(tmp_path, with_hash):
    source = tmp_path / "page.html"
    source.write_text(
        "<html><head><title>Source</title></head><body><p>Original.</p></body></html>"
    )
    store, _ = initialized_store(tmp_path, source)
    if not with_hash:
        manifest = store.load_manifest()
        manifest.pop("source_sha256", None)
        store.save_manifest(manifest)
    source.write_text("<html><head><title>Source</title></head><body><p>Changed.</p></body></html>")
    output = tmp_path / "translated.html"
    output.write_text("Previous export")
    with pytest.raises(ValueError, match="HTML source does not match translation state"):
        assemble(store, str(source), out_path=str(output), out_format="html")
    assert output.read_text() == "Previous export"


def test_source_anchor_mismatch_is_not_silently_dropped(tmp_path):
    source = tmp_path / "page.html"
    source.write_text("<html><body><p>Original.</p></body></html>")
    store, _ = initialized_store(tmp_path, source)
    chapter = store.load_chapter(0)
    chapter.segments[0].anchor = "missing"
    store.save_chapter(chapter)
    with pytest.raises(ValueError, match="backfill anchors"):
        assemble(store, str(source), out_path=str(tmp_path / "out.html"), out_format="html")


@pytest.mark.parametrize("order", ["source_first", "target_first"])
def test_complete_document_backfill_merges_continuations_and_adds_bilingual_css(tmp_path, order):
    source = tmp_path / "page.html"
    source.write_text(
        '<html><meta charset="utf-8"><style id="global">p{font-size:17px}</style>'
        '<body><h1>Chapter</h1><p id="paragraph">One long original paragraph.</p>'
        "<h1>Next</h1><p>Another.</p></body></html>"
    )
    store, document = initialized_store(tmp_path, source)
    chapter = document.chapters[0]
    split_long_segments([chapter], 3)
    for segment in chapter.segments:
        segment.target = "译"
    store.save_chapter(chapter)
    output = tmp_path / "translated.html"
    assemble(
        store,
        str(source),
        out_path=str(output),
        out_format="html",
        bilingual=True,
        order=order,
    )
    rendered = BeautifulSoup(output.read_text(), "html.parser")
    assert required(rendered.head).find("style", id="global") is not None
    assert rendered.head.find("style", id="tn-bilingual-style") is not None
    paragraph = required(rendered.find(id="paragraph"))
    expected = "".join(
        segment.target or "" for segment in chapter.segments if segment.kind != "heading"
    )
    assert paragraph.get_text() == expected
    sources = rendered.select(".tn-source")
    assert [node.get_text() for node in sources] == ["One long original paragraph.", "Another."]
    assert not rendered.select("[data-tn-id], [data-tn-annotation-id], [data-tn-line]")


@pytest.mark.parametrize("encoding", ["utf-8", "gb18030"])
def test_source_backfill_reuses_encoding_and_normalizes_file_newlines(tmp_path, encoding):
    source = tmp_path / "page.html"
    source.write_bytes(
        '<html><head><meta charset="gb18030"></head>'
        "<body><p>原文\r\n第二行。</p></body></html>".encode(encoding)
    )
    document = read_html(str(source), "zh", "en", encoding=encoding)
    store = FileStorage(str(tmp_path / "state"))
    store.init_from_document(document)
    for chapter in document.chapters:
        for segment in chapter.segments:
            segment.target = "Translated."
        store.save_chapter(chapter)
    output = tmp_path / "translated.html"
    assemble(store, str(source), out_path=str(output), out_format="html")
    rendered = BeautifulSoup(output.read_text(encoding="utf-8"), "html.parser")
    assert required(rendered.find("p")).get_text() == "Translated."
    assert [node["charset"] for node in required(rendered.head).find_all("meta")] == ["utf-8"]


def test_source_layout_and_attribute_destinations_override_stale_chapter_metadata(tmp_path):
    source = tmp_path / "page.html"
    source.write_text(
        '<html><body><p title="First">Body.</p><img alt="Second" src="missing.png"></body></html>'
    )
    store, _ = initialized_store(tmp_path, source)
    chapter = store.load_chapter(0)
    for segment in chapter.segments:
        attribute = segment.meta.get("html_attribute")
        if isinstance(attribute, dict):
            attribute["node_id"] = "stale"
    store.save_chapter(chapter)
    output = tmp_path / "translated.html"
    assemble(store, str(source), out_path=str(output), out_format="html")
    rendered = BeautifulSoup(output.read_text(), "html.parser")
    assert required(rendered.find("p"))["title"] == "译First"
    assert required(rendered.find("img"))["alt"] == "译Second"


def test_source_backfill_rejects_duplicate_anchors(tmp_path):
    source = tmp_path / "page.html"
    source.write_text("<p>First.</p><p>Second.</p>")
    store, _ = initialized_store(tmp_path, source)
    chapter = store.load_chapter(0)
    chapter.segments[1].anchor = chapter.segments[0].anchor
    store.save_chapter(chapter)
    with pytest.raises(ValueError, match="duplicate backfill anchors"):
        assemble(store, str(source), out_path=str(tmp_path / "out.html"), out_format="html")
