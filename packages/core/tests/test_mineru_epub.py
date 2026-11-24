"""Offline checks for MinerU Content List V2 to source EPUB."""

import json
from base64 import b64decode
from io import BytesIO
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest
from wenyi_core.assemble.writer import assemble
from wenyi_core.config import Config
from wenyi_core.ingest import mineru_api
from wenyi_core.ingest.epub_reader import read_epub
from wenyi_core.ingest.mineru_epub import convert_json
from wenyi_core.ingest.pdf_reader import pdf_cache_epub_path
from wenyi_core.ingest.source_hash import source_sha256
from wenyi_core.pipeline.prepare.input_preparation import parse_document
from wenyi_core.storage.file import FileStorage
from wenyi_core.storage.sqlite import SqliteStorage

from tests.pdf_fixtures import pdf_bytes

NS = {
    "m": "http://www.w3.org/1998/Math/MathML",
    "x": "http://www.w3.org/1999/xhtml",
    "o": "http://www.idpf.org/2007/opf",
    "n": "http://www.daisy.org/z3986/2005/ncx/",
    "e": "http://www.idpf.org/2007/ops",
}
GIF = b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")


def _span(text, kind="text", **fields):
    return {"type": kind, "content": text, **fields}


def _block(kind, anchor=None, **content):
    return {"type": kind, "content": content, **({"anchor": anchor} if anchor else {})}


def _xml(archive, suffix):
    return ET.fromstring(
        archive.read(next(name for name in archive.namelist() if name.endswith(suffix)))
    )


def _find(element: ET.Element, path: str) -> ET.Element:
    node = element.find(path, NS)
    assert node is not None, path
    return node


@pytest.mark.parametrize("storage_type", [FileStorage, SqliteStorage])
def test_pdf_uses_ordered_json_and_exports_against_frozen_epub(
    tmp_path, monkeypatch, storage_type, request
):
    source = tmp_path / "paper.pdf"
    source.write_bytes(pdf_bytes(["one", "two"]))
    store = storage_type(str(tmp_path / "state"))
    request.addfinalizer(store.close)
    cache = pdf_cache_epub_path(store.source_dir, source_sha256(source))
    cache.parent.mkdir(parents=True)
    legacy = cache.with_name("converted.html")
    legacy.write_text("<p>Legacy source must not be read for new imports.</p>", encoding="utf-8")
    monkeypatch.setattr(mineru_api, "MAX_PAGES", 1)
    submitted = []

    def bundle(label, image):
        stream = BytesIO()
        blocks = [
            _block("title", anchor="heading", level=1, title_content=[_span(label)]),
            _block("image", image_source={"path": "images/same.gif"}, image_caption=[_span(label)]),
            _block(
                "paragraph",
                paragraph_content=[_span("Back", "hyperlink", url="#heading")],
            ),
            _block(
                "table",
                table_caption=[_span(f"{label} table")],
                html=f'<table><tr><th colspan="2">{label} cell</th></tr>'
                "<tr><td>$x$</td><td>Other cell</td></tr></table>",
            ),
        ]
        with ZipFile(stream, "w") as archive:
            archive.writestr("generated_content_list_v2.json", json.dumps([blocks]))
            archive.writestr("images/same.gif", image)
        return stream.getvalue()

    def submit(_self, paths):
        submitted.extend(paths)
        return [bundle("One", GIF), bundle("Two", GIF[:-1] + b"\x00;")]

    monkeypatch.setattr(mineru_api.MinerUApi, "submit_batch", submit)
    config = Config.from_dict({"language": {"source": "en", "target": "zh"}})
    with store.lock():
        doc = parse_document(store, str(source), config, mineru_token_resolver=lambda: "fake-token")
        saved = parse_document(
            store,
            str(source),
            config,
            mineru_token_resolver=lambda: pytest.fail("Cache must not resolve a token"),
        )
    assert len(submitted) == 2
    assert doc.model_dump() == saved.model_dump()
    assert doc.fmt == "pdf" and doc.source_path == str(source)
    assert doc.meta["mineru_epub_sha256"] == source_sha256(cache)
    assert legacy.read_text().startswith("<p>Legacy source")
    assert [c.title for c in doc.chapters] == ["One", "Two"]
    manifest = store.init_from_document(doc)
    assert manifest["fmt"] == "pdf"
    assert manifest["source_sha256"] == source_sha256(source)
    for chapter in doc.chapters:
        for segment in chapter.segments:
            if segment.source.endswith(("table", "cell")):
                segment.target = "译文"
        store.save_chapter(chapter)

    out = tmp_path / "translated.epub"
    assemble(store, str(source), str(out), "epub", bilingual=True)
    with ZipFile(out) as archive:
        chapter = _xml(archive, "source.xhtml")
        assert [h.get("id") for h in chapter.findall(".//x:h1", NS)] == [
            "chunk-1-heading",
            "chunk-2-heading",
        ]
        assert [a.get("href") for a in chapter.findall(".//x:a", NS)] == [
            "#chunk-1-heading",
            "#chunk-2-heading",
        ]
        assert archive.read("EPUB/images/image-1.gif") != archive.read("EPUB/images/image-2.gif")
        assert all(
            {cell.tag for cell in row} <= {f"{{{NS['x']}}}th", f"{{{NS['x']}}}td"}
            for row in chapter.findall(".//x:tr", NS)
        )
        headings = chapter.findall(".//x:th", NS)
        assert [h.get("colspan") for h in headings] == ["2", "2"]
        assert [h.findtext("x:div", namespaces=NS) for h in headings] == ["One cell", "Two cell"]
        assert all(_find(h, "x:div").get("colspan") is None for h in headings)
        assert all(
            "译文" in "".join(node.itertext()) for node in chapter.findall(".//x:caption", NS)
        )
        assert len(chapter.findall(".//x:td//m:math", NS)) == 2

    html = tmp_path / "translated.html"
    assemble(store, str(source), str(html), "html")
    assert "data:image/gif;base64," in html.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="must not point at the source"):
        assemble(store, str(source), str(source), "epub")
    with pytest.raises(ValueError, match="must not point at the source"):
        assemble(store, str(source), str(cache), "txt")
    before = out.read_bytes()
    cache.write_bytes(b"changed")
    with pytest.raises(ValueError, match="MinerU source EPUB is missing or changed"):
        assemble(store, str(source), str(out), "epub")
    assert out.read_bytes() == before


def test_zip_converts_json_without_using_markdown_or_exported_html(tmp_path):
    source = tmp_path / "result.zip"
    output = tmp_path / "source.epub"
    image_source = {"path": "images/figure%20one.gif"}
    data = [
        [
            _block("page_header", page_header_content=[_span("Repeated page header")]),
            _block("title", level=1, title_content=[_span("Example")]),
            _block(
                "paragraph",
                paragraph_content=[
                    _span("Let "),
                    _span("k^2", "equation_inline"),
                    _span(" be positive. 0 < k. Contact author"),
                    _span("*", "hyperlink", url="#contact"),
                    _span("Broken reference", "hyperlink", url="#missing"),
                ],
            ),
            _block(
                "equation_interline",
                math_content=r"u+x=1\tag{1}",
                math_type="latex",
                image_source={"path": "unused-crop.gif"},
            ),
            _block(
                "page_footnote",
                anchor="contact",
                page_footnote_content=[_span("Contact details retained only in JSON.")],
            ),
        ],
        [
            _block("title", level=2, title_content=[_span("Results")]),
            _block(
                "chart",
                image_source=image_source,
                chart_caption=[_span("Diagram of "), _span("x^2", "equation_inline")],
                chart_footnote=[_span("Figure footnote")],
            ),
            _block(
                "table",
                table_caption=[_span("Conditions")],
                table_footnote=[_span("Table footnote")],
                html='<table onclick="ignored()"><tr><td colspan="2">For $x &lt; y$ only.'
                "<eq>a<b</eq><script>active()</script><code>$not_math$</code>"
                "</td></tr></table>",
            ),
            _block("image", image_source=image_source, image_caption=[_span("The same image")]),
            _block(
                "list",
                list_type="reference_list",
                list_items=[
                    {"item_type": "text", "item_content": [_span("[1] Example citation.")]}
                ],
            ),
        ],
    ]
    with ZipFile(source, "w") as archive:
        archive.writestr("bundle/test_content_list_v2.json", json.dumps(data))
        archive.writestr("bundle/images/figure one.gif", GIF)
        archive.writestr("bundle/full.md", "Not the source.")
        archive.writestr("bundle/full.html", "Not the source either.")
    convert_json(source, output)

    with ZipFile(output) as archive:
        content = _xml(archive, "/source.xhtml")
        assert len(content.findall(".//m:math", NS)) == 5
        assert content.findtext(".//m:math[@display='block']//m:mtext", namespaces=NS) == "(1)"
        assert [node.text for node in content.findall(".//x:td//m:annotation", NS)] == [
            "x < y",
            "a<b",
        ]
        assert content.findtext(".//x:td/x:code", namespaces=NS) == "$not_math$"
        assert content.find(".//x:script", NS) is None
        assert _find(content, ".//x:table").attrib == {}
        assert _find(content, ".//x:td").get("colspan") == "2"
        assert content.findtext(".//x:caption/x:p", namespaces=NS) == "Conditions"
        assert len(content.findall(".//x:figcaption", NS)) == 2
        assert (
            content.findtext(".//x:aside/x:p", namespaces=NS)
            == "Contact details retained only in JSON."
        )
        assert content.findtext(".//x:ul/x:li", namespaces=NS) == "[1] Example citation."
        prose = " ".join(content.itertext())
        assert "Repeated page header" not in prose and "Not the source" not in prose
        assert "0 < k." in prose and "Figure footnote" in prose and "Table footnote" in prose
        links = content.findall(".//x:a", NS)
        assert [link.attrib for link in links] == [
            {"href": "#contact", f"{{{NS['e']}}}type": "noteref"}
        ]
        assert "Broken reference" in prose
        images = content.findall(".//x:img", NS)
        assert len(images) == 2 and images[0].get("src") == images[1].get("src")
        assert len([name for name in archive.namelist() if name.endswith(".gif")]) == 1
        opf = _xml(archive, ".opf")
        item = _find(opf, ".//o:item[@href='source.xhtml']")
        assert "mathml" in item.attrib["properties"].split()

    doc = read_epub(str(output), "en", "zh-CN")
    segments = [segment for chapter in doc.chapters for segment in chapter.segments]
    assert "Conditions" in [segment.source for segment in segments]
    paragraph = next(segment for segment in segments if segment.source.startswith("Let "))
    items = paragraph.meta["epub_annotations"]["items"]
    assert "k^2" not in paragraph.source
    assert {item["relation"] for item in items} == {"inline_object", "noteref"}
    key = next(item["target_key"] for item in items if item["relation"] == "noteref")
    assert doc.meta["epub_annotation_contexts"]["contexts"][key]["source_blocks"] == [
        "Contact details retained only in JSON."
    ]


def test_inline_math_spacing_and_alignment_preserve_formula_source(tmp_path):
    source = tmp_path / "example_content_list_v2.json"
    output = tmp_path / "source.epub"
    latex = [r"\mathrm{O}(h^2)", "x+1", "y", "n", "z", "a"]
    paragraphs = [
        [
            _span("The standard "),
            _span(latex[0], "equation_inline"),
            _span("scheme uses ", styles=["emphasis"]),
            _span(latex[1], "equation_inline"),
            _span("and"),
            _span(latex[2], "equation_inline"),
            _span("."),
        ],
        [
            _span("当("),
            _span(latex[3], "equation_inline"),
            _span(")成立时"),
            _span(latex[4], "equation_inline"),
            _span("有效。"),
        ],
        [_span(latex[5], "equation_inline")],
        [_span("Ordinary prose keeps the reader's alignment.")],
    ]
    source.write_text(
        json.dumps([[_block("paragraph", paragraph_content=spans) for spans in paragraphs]]),
        encoding="utf-8",
    )
    convert_json(source, output)

    with ZipFile(output) as archive:
        content = _xml(archive, "/source.xhtml")
        prose, cjk, math_only, plain = content.findall(".//x:p", NS)
        assert prose.get("class") == cjk.get("class") == "inline-math"
        assert math_only.get("class") is None and plain.get("class") is None
        style = _find(content, ".//x:style").text
        assert (
            style is not None and "p.inline-math { text-align: left; text-align: start; }" in style
        )
        first, second, third = prose.findall("m:math", NS)
        assert prose.text == "The standard "
        assert _find(prose, "x:em").text == " scheme uses "
        assert second.tail == " and " and third.tail == "."
        assert first.tail is None
        assert cjk.text == "当("
        assert [math.tail for math in cjk.findall("m:math", NS)] == [")成立时", "有效。"]
        assert [a.text for a in content.findall(".//m:annotation", NS)] == latex

    doc = read_epub(str(output), "en", "zh-CN")
    paragraph = next(
        s
        for chapter in doc.chapters
        for s in chapter.segments
        if s.source.startswith("The standard")
    )
    assert len(paragraph.meta["epub_annotations"]["items"]) == 3


def test_nav_and_ncx_use_explicit_json_title_levels(tmp_path):
    source = tmp_path / "example_content_list_v2.json"
    output = tmp_path / "source.epub"
    titles = [
        (1, "Paper"),
        (2, "8. Numerical results"),
        (2, "8.1. Same level in JSON"),
        (3, "An explicit subsection"),
    ]
    blocks = []
    for index, (level, text) in enumerate(titles):
        blocks.append(_block("title", level=level, title_content=[_span(text)]))
        blocks.append(_block("paragraph", paragraph_content=[_span(f"Body {index}.")]))
    source.write_text(json.dumps([blocks]), encoding="utf-8")
    convert_json(source, output, title="Original PDF filename")

    with ZipFile(output) as archive:
        nav = _find(_xml(archive, "/nav.xhtml"), ".//x:nav")
        ncx = _find(_xml(archive, ".ncx"), "n:navMap")
        child = "x:ol/x:li"
        links = nav.findall(f"{child}/{child}", NS)
        assert [li.findtext("x:a", namespaces=NS) for li in links] == [
            "8. Numerical results",
            "8.1. Same level in JSON",
        ]
        assert links[0].find(child, NS) is None
        assert links[1].findtext(f"{child}/x:a", namespaces=NS) == "An explicit subsection"
        assert [
            point.findtext("n:navLabel/n:text", namespaces=NS)
            for point in ncx.findall("n:navPoint/n:navPoint", NS)
        ] == ["8. Numerical results", "8.1. Same level in JSON"]
        chapter = _xml(archive, "/source.xhtml")
        anchors = {node.get("id") for node in chapter.iter() if node.get("id")}
        assert [a.get("href") for a in nav.findall(".//x:a", NS)] == [
            c.get("src") for c in ncx.findall(".//n:content", NS)
        ]
        assert all(a.attrib["href"].split("#")[1] in anchors for a in nav.findall(".//x:a", NS))

    doc = read_epub(str(output), "en", "zh")
    assert [c.title for c in doc.chapters] == [text for _, text in titles]
    assert all(c.meta.get("toc_entry_id") for c in doc.chapters)
    for index, chapter in enumerate(doc.chapters):
        assert [s.source for s in chapter.segments] == [chapter.title, f"Body {index}."]


@pytest.mark.parametrize("order", ["source_first", "target_first"])
def test_translated_figure_has_one_bilingual_caption(tmp_path, order):
    from wenyi_core.assemble import epub_native

    source = tmp_path / "sample.json"
    (tmp_path / "chart.gif").write_bytes(GIF)
    chart = _block("chart", image_source={"path": "chart.gif"}, chart_caption=[_span("One chart.")])
    source.write_text(json.dumps([[chart]]), encoding="utf-8")
    epub, out = tmp_path / "source.epub", tmp_path / "paired.epub"
    convert_json(source, epub)
    doc = read_epub(str(epub), "en", "zh")
    segment = next(s for c in doc.chapters for s in c.segments if s.source == "One chart.")
    segment.target = "图表。"
    state = epub_native.options(
        doc.chapters,
        doc.meta,
        book_title=doc.title,
        source_lang="en",
        target_lang="zh",
        export_title="图表",
        bilingual=True,
        order=order,
        preserve_source_style=False,
    )
    epub_native.export(str(epub), out, state)
    with ZipFile(out) as archive:
        figure = _find(_xml(archive, "/source.xhtml"), ".//x:figure")
        assert [node.tag.split("}")[-1] for node in figure] == ["img", "figcaption"]
        expected = ["图表。", "One chart."] if order == "target_first" else ["One chart.", "图表。"]
        assert list(figure[-1].itertext()) == expected


def test_code_and_algorithm_v2_spans_preserve_lines_math_and_captions(tmp_path):
    source, output = tmp_path / "blocks.json", tmp_path / "source.epub"
    code = 'if x < 2:\n    print("<sup>")\n'
    blocks = [
        _block(
            "code",
            code_content=[_span(code), _span("return x")],
            code_language="python",
            code_caption=[_span("Listing 1")],
        ),
        _block(
            "algorithm",
            algorithm_caption=[_span("Algorithm 1")],
            algorithm_content=[_span("Start\n  set "), _span("x^2", "equation_inline")],
        ),
    ]
    source.write_text(json.dumps([blocks]), encoding="utf-8")
    convert_json(source, output)
    with ZipFile(output) as archive:
        chapter = _xml(archive, "/source.xhtml")
        assert chapter.findtext(".//x:pre/x:code", namespaces=NS) == code + "return x"
        captions = chapter.findall(".//x:figcaption", NS)
        assert ["".join(c.itertext()) for c in captions] == ["Listing 1", "Algorithm 1"]
        algorithm = _find(chapter, ".//x:figure/x:p")
        assert algorithm.text == "Start\n  set "
        assert algorithm.findtext(".//m:annotation", namespaces=NS) == "x^2"
    doc = read_epub(str(output), "en", "zh")
    segments = [s for c in doc.chapters for s in c.segments]
    assert not any("return x" in s.source for s in segments)
    assert any(s.source.startswith("Start") and s.meta.get("epub_annotations") for s in segments)


def test_unrenderable_formula_keeps_visible_number_and_uses_crop(tmp_path):
    source = tmp_path / "example_content_list_v2.json"
    output = tmp_path / "source.epub"
    (tmp_path / "figure.gif").write_bytes(GIF)
    source.write_text(
        json.dumps(
            [
                [
                    _block("paragraph", paragraph_content=[_span("Bad\x01 character")]),
                    _block(
                        "equation_interline",
                        math_content=r"\begin{aligned}x&=y\\a&=b\end{aligned}\tag{4}",
                        image_source={"path": "figure.gif"},
                    ),
                ]
            ]
        ),
        encoding="utf-8",
    )
    convert_json(source, output)

    with ZipFile(output) as archive:
        chapter = _xml(archive, "/source.xhtml")
        text = _find(chapter, ".//x:p").text
        assert text is not None and "\ufffd" in text
        image = _find(chapter, ".//x:img")
        assert r"\begin{aligned}" in image.attrib["alt"]
        assert image.tail is not None and image.tail.strip() == "(4)"
        assert archive.read("EPUB/" + image.attrib["src"]) == GIF


@pytest.mark.parametrize(
    "path", ["../outside.gif", "%2e%2e/outside.gif", "https://example.org/a.gif"]
)
def test_images_must_be_local_to_export(tmp_path, path):
    source = tmp_path / "example_content_list_v2.json"
    source.write_text(
        json.dumps([[_block("image", image_source={"path": path})]]), encoding="utf-8"
    )
    output = tmp_path / "source.epub"
    with pytest.raises(ValueError, match="Image must be local"):
        convert_json(source, output)
    assert not output.exists()


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ([{"type": "text"}], "Expected Content List V2"),
        ([[_block("new_block")]], "MinerU page 1, block 1: Unsupported"),
    ],
)
def test_unhandled_source_fails_without_publishing(tmp_path, data, message):
    source = tmp_path / "source.json"
    source.write_text(json.dumps(data), encoding="utf-8")
    output = tmp_path / "source.epub"
    with pytest.raises(ValueError, match=message):
        convert_json(source, output)
    assert not output.exists()


def test_mineru_epub_export_updates_head_title_from_translated_chapter(tmp_path):
    source_json = tmp_path / "paper_content_list_v2.json"
    source_json.write_text(
        json.dumps(
            [
                [
                    _block("title", level=1, title_content=[_span("Quantum Notes")]),
                    _block("paragraph", paragraph_content=[_span("Body paragraph.")]),
                ]
            ]
        ),
        encoding="utf-8",
    )
    pdf_path = tmp_path / "Quantum Notes.pdf"
    pdf_path.write_bytes(pdf_bytes(["page one"]))
    store = FileStorage(str(tmp_path / "state"))
    cache = pdf_cache_epub_path(store.source_dir, source_sha256(pdf_path))
    cache.parent.mkdir(parents=True)
    convert_json(source_json, cache, title=pdf_path.stem, lang="en")

    doc = read_epub(str(cache), "en", "zh")
    doc.fmt = "pdf"
    doc.source_path = str(pdf_path)
    doc.meta["mineru_epub_sha256"] = source_sha256(cache)
    manifest = store.init_from_document(doc)
    assert manifest["title"] == manifest["chapters"][0]["title"]
    chapter = store.load_chapter(0)
    chapter.segments[0].target = "量子笔记"
    chapter.segments[1].target = "正文段落。"
    store.save_chapter(chapter)
    manifest["chapters"][0]["title_translated"] = "量子笔记"
    store.save_manifest(manifest)

    out = tmp_path / "translated.epub"
    assemble(store, str(pdf_path), str(out), "epub", about_page=False)
    with ZipFile(out) as archive:
        opf_name = next(name for name in archive.namelist() if name.endswith(".opf"))
        opf = archive.read(opf_name).decode("utf-8")
        xhtml = ET.fromstring(archive.read("EPUB/source.xhtml"))
    assert "<dc:title>Quantum Notes-wenyi-zh</dc:title>" in opf
    assert xhtml.findtext("x:head/x:title", namespaces=NS) == "量子笔记"
