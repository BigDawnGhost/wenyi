"""Bilingual source blocks are complete DOM snapshots, not filtered translation inputs."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Literal

import pytest
from bs4 import BeautifulSoup, Tag
from wenyi_core.alignment import target_digest
from wenyi_core.alignment.objects import resolve_objects
from wenyi_core.assemble.html_renderer import _render_segments_html
from wenyi_core.assemble.writer import assemble
from wenyi_core.ingest.epub_reader import read_epub
from wenyi_core.markup.objects import object_anchors
from wenyi_core.models.document import Segment
from wenyi_core.storage.file import FileStorage


def required_tag(root: Tag, selector: str) -> Tag:
    result = root.select_one(selector)
    assert isinstance(result, Tag), selector
    return result


@pytest.mark.parametrize("order", ["source_first", "target_first"])
@pytest.mark.parametrize("preserve_source_style", [False, True])
def test_source_snapshot_retains_all_inline_content_and_structure(order, preserve_source_style):
    content = (
        'Where <em class="emphasis">a <strong>value</strong></em> '
        "<ruby>漢字<rp>(</rp><rt>かんじ</rt><rp>)</rp></ruby> "
        '<code data-example="literal">x &lt; y</code> '
        '<math xmlns="http://www.w3.org/1998/Math/MathML"><msup><mi>x</mi><mn>2</mn>'
        "</msup></math>"
        '<img src="figure.png" alt="Original figure"/> '
        '<svg xmlns="http://www.w3.org/2000/svg"><text>Diagram</text></svg> '
        '<a href="notes.xhtml#note"><sup>1</sup>reference</a>'
        '<span style="font-variant:small-caps">tail</span><!-- original comment -->.'
    )
    template = (
        '<html><body><p data-tn-id="paragraph" dir="rtl" lang="ar" '
        f'title="Original title" data-example="original">{content}</p></body></html>'
    )
    segment = Segment(index=0, anchor="paragraph", source="Where a value tail.", target="译文。")
    before = segment.model_dump()
    rendered = BeautifulSoup(
        _render_segments_html(
            template,
            [segment],
            bilingual=True,
            order=order,
            source_lang="en",
            preserve_source_style=preserve_source_style,
        ),
        "html.parser",
    )
    source = rendered.select_one(".tn-source")
    assert source is not None
    expected = BeautifulSoup(content, "html.parser")
    assert source.decode_contents() == str(expected)
    assert source["dir"] == "rtl"
    assert source["lang"] == "ar"
    assert source["title"] == "Original title"
    assert source["data-example"] == "original"
    assert required_tag(rendered, "p:not(.tn-source)").get_text() == "译文。"
    body = required_tag(rendered, "body")
    assert [node is source for node in body.find_all("p", recursive=False)] == (
        [True, False] if order == "source_first" else [False, True]
    )
    assert segment.model_dump() == before


def test_source_snapshot_remaps_ids_and_local_references_without_flattening_objects():
    template = """<html><body>
<p id="body" data-tn-id="paragraph">Use
<math id="formula"><mrow id="expression"><mi>x</mi></mrow><share href="#expression"/></math>
<svg id="diagram" aria-labelledby="caption">
  <title id="caption">Original diagram</title><defs><linearGradient id="gradient"/></defs>
  <path id="shape" fill="url(#gradient)"/><use xlink:href="#shape"/>
</svg><span aria-describedby="caption" style="filter:url('#gradient')">reference</span>
<a href="#expression">the expression</a>.</p></body></html>"""
    rendered = BeautifulSoup(
        _render_segments_html(
            template,
            [Segment(index=0, anchor="paragraph", source="Use reference.", target="译文。")],
            bilingual=True,
        ),
        "html.parser",
    )
    source = rendered.select_one(".tn-source")
    assert source is not None and source.math is not None and source.svg is not None
    assert rendered.find(id="body") is not source
    expression = required_tag(source, "math mrow")["id"]
    caption = required_tag(source, "svg title")["id"]
    gradient = required_tag(source, "svg lineargradient")["id"]
    shape = required_tag(source, "svg path")["id"]
    assert required_tag(source, "math share")["href"] == f"#{expression}"
    assert source.svg["aria-labelledby"] == caption
    assert required_tag(source, "svg use")["xlink:href"] == f"#{shape}"
    assert required_tag(source, "svg path")["fill"] == f"url(#{gradient})"
    assert required_tag(source, "span")["aria-describedby"] == caption
    assert required_tag(source, "span")["style"] == f"filter:url('#{gradient}')"
    assert required_tag(source, "a")["href"] == f"#{expression}"
    ids = [node["id"] for node in rendered.find_all(id=True)]
    assert len(ids) == len(set(ids))
    assert not any(
        str(attribute).startswith("data-tn-")
        for node in rendered.find_all(True)
        for attribute in node.attrs
    )


def test_source_snapshot_precedes_all_translated_attribute_updates():
    template = """<html><body><p data-tn-id="paragraph" data-tn-attribute-id="body-slot"
title="Original body title">Body.<img
data-tn-inline-id="image" data-tn-attribute-id="image-slot" src="figure.png"
alt="Original description"/></p></body></html>"""
    segments = [
        Segment(
            index=1,
            anchor="attribute",
            source="Original description",
            target="译文描述",
            meta={"html_attribute": {"node_id": "image-slot", "name": "alt"}},
        ),
        Segment(
            index=2,
            anchor="body-attribute",
            source="Original body title",
            target="正文标题",
            meta={"html_attribute": {"node_id": "body-slot", "name": "title"}},
        ),
        Segment(
            index=0,
            anchor="paragraph",
            source="Body.",
            target="正文。",
            meta={
                "epub_inline": {
                    "source_length": 5,
                    "nodes": [{"id": "image", "offset": 5}],
                }
            },
        ),
    ]
    rendered = BeautifulSoup(
        _render_segments_html(template, segments, bilingual=True), "html.parser"
    )
    source = rendered.select_one(".tn-source")
    target = rendered.select_one("p:not(.tn-source)")
    assert source is not None and target is not None
    assert required_tag(source, "img")["alt"] == "Original description"
    assert required_tag(target, "img")["alt"] == "译文描述"
    assert source["title"] == "Original body title"
    assert target["title"] == "正文标题"


def test_source_descendant_ids_avoid_original_and_source_wrapper_collisions():
    template = """<html><body>
<span id="tn-source-first-fragment-1"></span>
<p data-tn-id="first">First <em id="emphasis">word</em>.</p>
<p data-tn-id="first-fragment-1-2">Second paragraph.</p></body></html>"""
    segments = [
        Segment(index=0, anchor="first", source="First word.", target="第一段。"),
        Segment(
            index=1, anchor="first-fragment-1-2", source="Second paragraph.", target="第二段。"
        ),
    ]
    rendered = BeautifulSoup(
        _render_segments_html(template, segments, bilingual=True), "html.parser"
    )
    source = rendered.select_one(".tn-source")
    assert source is not None
    assert required_tag(source, "em")["id"] == "tn-source-first-fragment-1-3"
    ids = [node["id"] for node in rendered.find_all(id=True)]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("out_format", ["epub", "html"])
@pytest.mark.parametrize("order", ["source_first", "target_first"])
def test_complete_source_survives_public_export_without_changing_state(
    tmp_path: Path, out_format: str, order: Literal["source_first", "target_first"]
):
    source_path = tmp_path / "source.epub"
    pages = {
        "one.xhtml": """<html><body><p>First <em>important</em>
<ruby>漢字<rt>かんじ</rt></ruby> <code>x &lt; y</code>
<math><mi id="x">x</mi><share href="two.xhtml#y"/></math>
<img src="figure.png" alt="Original figure"/>.</p></body></html>""",
        "two.xhtml": """<html><body><p>Second
<math><mi id="y">y</mi><share xlink:href="one.xhtml#x"/></math>.</p></body></html>""",
    }
    with zipfile.ZipFile(source_path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr(
            "META-INF/container.xml",
            '<container><rootfiles><rootfile full-path="content.opf"/></rootfiles></container>',
        )
        archive.writestr(
            "content.opf",
            "<package><metadata><title>Source snapshots</title></metadata><manifest>"
            '<item id="one" href="one.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="two" href="two.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="figure" href="figure.png" media-type="image/png"/>'
            '</manifest><spine><itemref idref="one"/><itemref idref="two"/></spine></package>',
        )
        for href, html in pages.items():
            archive.writestr(href, html)
        archive.writestr("figure.png", b"original image bytes")
    original_bytes = source_path.read_bytes()
    store = FileStorage(str(tmp_path / "state"))
    manifest = store.stage_document(read_epub(str(source_path), "en", "zh"))
    for entry in manifest["chapters"]:
        chapter = store.load_chapter(entry["index"])
        for segment in chapter.segments:
            metadata = segment.meta.get("epub_annotations", {})
            anchors = object_anchors(metadata.get("items", []))
            marked_target = "译文。" + "".join(anchor.marker for anchor in anchors)
            resolved = resolve_objects([marked_target], anchors, segment.source)
            segment.target = resolved.targets[0]
            if anchors:
                metadata.update(
                    target_digest=target_digest(segment.target),
                    placements=list(resolved.placements),
                )
        store.save_chapter(chapter)
    store.save_manifest(manifest)
    before = [store.load_chapter(entry["index"]).model_dump() for entry in manifest["chapters"]]
    output = tmp_path / f"output.{out_format}"
    assemble(
        store,
        str(source_path),
        str(output),
        out_format=out_format,
        bilingual=True,
        order=order,
        punctuation_normalize=True,
        about_page=False,
    )
    if out_format == "epub":
        with zipfile.ZipFile(output) as archive:
            rendered = [BeautifulSoup(archive.read(href), "html.parser") for href in pages]
            assert archive.namelist().count("figure.png") == 1
            assert archive.read("figure.png") == b"original image bytes"
    else:
        rendered = [BeautifulSoup(output.read_text(), "html.parser")]
    sources = [source for page in rendered for source in page.select(".tn-source")]
    assert len(sources) == 2
    first, second = sources
    assert required_tag(first, "em").get_text() == "important"
    assert required_tag(first, "ruby rt").get_text() == "かんじ"
    assert required_tag(first, "code").get_text() == "x < y"
    assert required_tag(first, "img")["alt"] == "Original figure"
    assert [node.get_text() for source in sources for node in source.find_all("math")] == [
        "x",
        "y",
    ]
    first_id = required_tag(first, "math mi")["id"]
    second_id = required_tag(second, "math mi")["id"]
    assert required_tag(first, "math share")["href"] == f"two.xhtml#{second_id}"
    assert required_tag(second, "math share")["xlink:href"] == f"one.xhtml#{first_id}"
    for page in rendered:
        ids = [node["id"] for node in page.find_all(id=True)]
        assert len(ids) == len(set(ids))
        assert len(page.find_all("math")) == 2 * len(page.select(".tn-source"))
    assert source_path.read_bytes() == original_bytes
    assert store.load_manifest() == manifest
    assert [
        store.load_chapter(entry["index"]).model_dump() for entry in manifest["chapters"]
    ] == before
