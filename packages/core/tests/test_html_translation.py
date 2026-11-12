"""Offline HTML extraction, semantic inline alignment and template backfill tests."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from bs4.element import Tag
from wenyi_core.assemble.html_renderer import _render_chapter_html
from wenyi_core.assemble.writer import assemble
from wenyi_core.config import Config
from wenyi_core.ingest.html_reader import read_html
from wenyi_core.ingest.segmenter import split_long_segments
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.models.document import Chapter
from wenyi_core.pipeline.annotate.annotations import AnnotationService
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.pipeline.run.runtime import PipelineRuntime
from wenyi_core.storage.file import FileStorage

from tests.fake_llm import routing_handler


def read(tmp_path: Path, body: str):
    path = tmp_path / "page.html"
    path.write_text(f"<html><body>{body}</body></html>", encoding="utf-8")
    return read_html(str(path), "en", "zh", chapter_tags=None)


def required(node: object) -> Tag:
    assert isinstance(node, Tag)
    return node


def align(chapter: Chapter, tmp_path: Path, handler):
    config = Config.from_dict({"llm": {"preset": "fake"}})
    client = FakeClient(handler=handler)
    service = AnnotationService(PipelineRuntime(config, client))
    store = FileStorage(str(tmp_path / "alignment"))
    service.align_annotations_after_batch(0, chapter, 0, len(chapter.text_segments), store)
    return client, service, store


def focus_response(messages, *_args):
    payload = json.loads(messages[-1]["content"].split("INPUT JSON:\n", 1)[1])
    assert payload["source_with_focus"] == "Please <focus>sign in</focus> to continue."
    return json.dumps({"target_quote": "登录", "context": "请先登录"})


def test_extracts_mixed_containers_and_ui_in_source_order(tmp_path):
    document = read(
        tmp_path,
        '<header><a href="/home">Home</a></header>'
        "<main>Introduction <em>before</em>."
        "<section>Section lead.<p>Body <strong>text</strong>.</p>Section tail.</section>"
        "Outro.</main>"
        "<ul><li>Parent.<ul><li>Child.</li></ul>Tail.</li></ul>"
        "<table><tr><th>Heading</th><td>A <span>cell</span>.</td></tr></table>"
        "<form><label>Name</label><button>Send</button></form>",
    )
    assert [segment.source for segment in document.chapters[0].segments] == [
        "Home",
        "Introduction before.",
        "Section lead.",
        "Body text.",
        "Section tail.",
        "Outro.",
        "Parent.",
        "Child.",
        "Tail.",
        "Heading",
        "A cell.",
        "Name",
        "Send",
    ]


def test_nested_inline_tags_use_existing_aligner_without_splitting_sentence(tmp_path):
    document = read(
        tmp_path,
        '<p id="intro" class="lead">Please <a href="https://example.test/login" '
        'class="action"><strong id="login" style="font-weight:700">sign in</strong></a>'
        " to continue.</p>",
    )
    chapter = document.chapters[0]
    assert [segment.source for segment in chapter.segments] == ["Please sign in to continue."]
    segment = chapter.segments[0]
    segment.target = "若要继续，请先登录。"
    client, service, store = align(chapter, tmp_path, focus_response)
    assert len(client.calls) == 2
    assert all(call["stage"] == "annotation.align" for call in client.calls)
    saved = store.load_chapter(0)
    service.align_annotations_after_batch(0, saved, 0, 1, store)
    assert len(client.calls) == 2
    # Rendering uses only validated ranges; the model never returns HTML or attributes.
    rendered = BeautifulSoup(_render_chapter_html(saved), "html.parser")
    paragraph = required(rendered.find("p", id="intro"))
    link = required(paragraph.find("a"))
    strong = required(link.find("strong"))
    assert paragraph.get_text() == segment.target
    assert paragraph["class"] == ["lead"]
    assert link["href"] == "https://example.test/login"
    assert link["class"] == ["action"]
    assert strong.get_text() == "登录"
    assert strong["id"] == "login"
    assert strong["style"] == "font-weight:700"
    assert saved.segments[0].target == "若要继续，请先登录。"
    assert not rendered.select("[data-tn-id], [data-tn-annotation-id], [data-tn-line]")


@pytest.mark.parametrize(
    "protected",
    [
        "<span hidden>Keep original</span>",
        '<span style="DISPLAY: none !important">Keep original</span>',
        '<span style="visibility: hidden">Keep original</span>',
        '<span translate="no">Keep original</span>',
        '<span class="notranslate">Keep original</span>',
        '<span contenteditable="true">Keep original</span>',
        "<code>Keep original</code>",
        "<pre><p>Keep original</p></pre>",
        "<math><mi>Keep original</mi></math>",
        "<svg><text>Keep original</text></svg>",
        "<template><p>Keep original</p></template>",
        "<script>Keep original</script>",
        "<style>Keep original</style>",
    ],
)
def test_protected_subtrees_are_not_translated_or_erased(tmp_path, protected):
    chapter = read(tmp_path, f"<div>Before.{protected}After.</div>").chapters[0]
    assert [segment.source for segment in chapter.segments] == ["Before.", "After."]
    for segment, target in zip(chapter.segments, ["之前。", "之后。"], strict=True):
        segment.target = target
    rendered = BeautifulSoup(_render_chapter_html(chapter), "html.parser")
    expected = BeautifulSoup(protected, "html.parser").find(True)
    assert str(rendered.find(required(expected).name)) == str(expected)
    assert "之前。" in str(rendered)
    assert "之后。" in str(rendered)


def test_attributes_are_translated_without_exposing_structural_attributes(tmp_path):
    chapter = read(
        tmp_path,
        '<p>Body.</p><img src="photo.png" alt="A photo" title="Preview"/>'
        '<input id="query" name="q" placeholder="Search" aria-label="Search site" '
        'value="Keep value"/>'
        '<input type="submit" value="Send" data-action="submit"/>',
    ).chapters[0]
    translations = {
        "Body.": "正文。",
        "A photo": "一张照片",
        "Preview": "预览",
        "Search": "搜索",
        "Search site": "站内搜索",
        "Send": "发送",
    }
    assert {segment.source for segment in chapter.segments} == set(translations)
    for segment in chapter.segments:
        segment.target = translations[segment.source]
    rendered = BeautifulSoup(_render_chapter_html(chapter), "html.parser")
    image = required(rendered.find("img"))
    query = required(rendered.find("input", id="query"))
    submit = required(rendered.find("input", type="submit"))
    assert image["alt"] == "一张照片"
    assert image["title"] == "预览"
    assert image["src"] == "photo.png"
    assert query["placeholder"] == "搜索"
    assert query["aria-label"] == "站内搜索"
    assert query["name"] == "q"
    assert query["value"] == "Keep value"
    assert submit["value"] == "发送"
    assert submit["data-action"] == "submit"
    assert "data-tn-" not in str(rendered)


@pytest.mark.parametrize("order", ["source_first", "target_first"])
def test_bilingual_inline_markup_and_cells_keep_valid_structure(tmp_path, order):
    chapter = read(
        tmp_path,
        '<table><tr><td id="cell">Please <a href="/login"><strong id="login">'
        "sign in</strong></a> to continue.</td></tr></table>",
    ).chapters[0]
    chapter.segments[0].target = "若要继续，请先登录。"
    align(chapter, tmp_path, focus_response)
    rendered = BeautifulSoup(
        _render_chapter_html(chapter, bilingual=True, order=order),
        "html.parser",
    )
    row = required(rendered.find("tr"))
    assert [child.name for child in row.find_all(recursive=False)] == ["td"]
    cell = required(row.find("td"))
    source = required(cell.find(class_="tn-source"))
    assert source.get_text() == "Please sign in to continue."
    assert required(source.find("a"))["href"] == "/login"
    assert required(source.find("strong")).get_text() == "sign in"
    assert len(rendered.find_all(id="login")) == 1
    assert len(rendered.find_all(id="cell")) == 1
    assert cell.get_text().startswith("Please" if order == "source_first" else "若要继续")


def test_alignment_waits_for_continuations_and_stale_ranges_are_not_reused(tmp_path):
    chapter = read(
        tmp_path,
        '<p>Please <a href="/login"><strong id="login">sign in</strong></a> to continue.</p>',
    ).chapters[0]
    split_long_segments([chapter], 3)
    assert len(chapter.segments) > 1
    target = "若要继续，请先登录。"
    chapter.segments[0].target = target[0]
    client, service, store = align(chapter, tmp_path, focus_response)
    assert not client.calls
    for position, segment in enumerate(chapter.segments[1:], 1):
        segment.target = target[position : position + 1]
    chapter.segments[-1].target = target[len(chapter.segments) - 1 :]
    service.align_annotations_after_batch(0, chapter, len(chapter.segments) - 1, 1, store)
    assert len(client.calls) == 2
    chapter.segments[-1].target = "新译文。"
    rendered = BeautifulSoup(_render_chapter_html(chapter), "html.parser")
    assert "".join(segment.target or "" for segment in chapter.segments) in rendered.get_text()
    assert not rendered.find("strong", string="登录")
    assert rendered.find(id="login") is not None


@pytest.mark.parametrize("fmt", ["html", "epub"])
def test_export_preserves_nontext_fragments_shell_and_attribute_only_content(tmp_path, fmt):
    source = tmp_path / "page.html"
    source.write_text(
        '<html class="page"><head><title>Sample</title><style>.page{color:black}</style>'
        '</head><body class="reader" id="page"><pre>Keep code</pre>'
        '<h1>Chapter</h1><p>Body.</p><img src="missing.png" alt="Caption"/></body></html>',
        encoding="utf-8",
    )
    document = read_html(str(source), "en", "zh")
    store = FileStorage(str(tmp_path / "state"))
    store.init_from_document(document)
    for chapter in document.chapters:
        for segment in chapter.segments:
            segment.target = f"译{segment.source}"
        store.save_chapter(chapter)
    output = tmp_path / f"translated.{fmt}"
    assemble(
        store,
        document.source_path,
        out_path=str(output),
        out_format=fmt,
        about_page=False,
    )
    if fmt == "epub":
        with zipfile.ZipFile(output) as archive:
            html = "".join(
                archive.read(name).decode()
                for name in archive.namelist()
                if name.endswith(".xhtml")
            )
    else:
        html = output.read_text()
    rendered = BeautifulSoup(html, "html.parser")
    assert required(rendered.find("pre")).get_text() == "Keep code"
    assert required(rendered.find("img"))["alt"] == "译Caption"
    assert "译Body." in rendered.get_text()
    assert "data-tn-" not in html
    page = required(rendered.find("html", class_="page"))
    assert required(page.find("body"))["class"] == ["reader"]
    assert required(page.find("body"))["id"] == "page"


def test_inline_media_keeps_its_original_link_and_dom_position(tmp_path):
    chapter = read(
        tmp_path,
        '<p>Before <a href="/photo"><img src="photo.png"/>photo</a> after.</p>',
    ).chapters[0]
    assert [segment.source for segment in chapter.segments] == ["Before", "photo", "after."]
    for segment in chapter.segments:
        segment.target = f"译{segment.source}"
    rendered = BeautifulSoup(_render_chapter_html(chapter), "html.parser")
    link = required(rendered.find("a"))
    assert link["href"] == "/photo"
    assert required(link.find("img"))["src"] == "photo.png"
    assert link.get_text() == "译photo"
    assert required(link.find("img")).next_sibling == "译photo"


@pytest.mark.parametrize("fixed", ["<code>foo</code>", '<img src="photo.png"/>'])
def test_inline_protected_content_keeps_surrounding_word_spaces(tmp_path, fixed):
    chapter = read(tmp_path, f"<p>Use {fixed} now.</p>").chapters[0]
    for segment, target in zip(chapter.segments, ["Utilisez", "maintenant."], strict=True):
        segment.target = target
    rendered = BeautifulSoup(_render_chapter_html(chapter), "html.parser")
    paragraph = str(required(rendered.find("p")))
    assert paragraph.startswith("<p>Utilisez ")
    assert paragraph.endswith(" maintenant.</p>")
    assert fixed in paragraph


def test_whole_source_nested_styles_need_no_alignment_calls(tmp_path):
    chapter = read(
        tmp_path,
        '<p><span class="outer"><a href="/login"><strong>sign in</strong></a></span></p>',
    ).chapters[0]
    chapter.segments[0].target = "登录"
    client, _, _ = align(chapter, tmp_path, lambda *_: pytest.fail("No model needed"))
    assert not client.calls
    rendered = BeautifulSoup(_render_chapter_html(chapter), "html.parser")
    assert required(rendered.select_one("p > span.outer > a > strong")).get_text() == "登录"
    assert rendered.get_text() == "登录"


@pytest.mark.parametrize("invalid", ["crossing", "overlapping", "failed"])
def test_invalid_inline_ranges_never_remove_or_rewrite_translation(tmp_path, invalid):
    chapter = read(
        tmp_path,
        '<p><strong id="one">One</strong> then <em id="two">two</em>.</p>',
    ).chapters[0]
    segment = chapter.segments[0]
    segment.target = "先二后一。"

    def handler(messages, *_):
        payload = json.loads(messages[-1]["content"].split("INPUT JSON:\n", 1)[1])
        quote = "一" if "<focus>One</focus>" in payload["source_with_focus"] else "二"
        return json.dumps({"target_quote": quote, "context": "先二后一。"})

    align(chapter, tmp_path, handler)
    metadata = segment.meta["epub_annotations"]
    first, second = metadata["placements"]
    if invalid == "failed":
        first["status"] = "fallback"
    else:
        first.update(target_start=0, target_end=3)
        second.update(target_start=2, target_end=4 if invalid == "crossing" else 3)
    rendered = BeautifulSoup(_render_chapter_html(chapter), "html.parser")
    assert rendered.get_text() == segment.target
    assert required(rendered.find(id="one")).parent is not None
    assert required(rendered.find(id="two")).get_text() == ("二" if invalid == "failed" else "")
    assert required(rendered.find("strong")).find("em") is None


def test_failed_link_keeps_navigation_without_fabricating_a_translated_range(tmp_path):
    chapter = read(tmp_path, '<p>Please <a href="/login">sign in</a> to continue.</p>').chapters[0]
    chapter.segments[0].target = "若要继续，请先登录。"
    client, service, store = align(chapter, tmp_path, lambda *_: "{}")
    assert len(client.calls) == 1
    saved = store.load_chapter(0)
    service.align_annotations_after_batch(0, saved, 0, 1, store)
    assert len(client.calls) == 1
    rendered = BeautifulSoup(_render_chapter_html(saved), "html.parser")
    assert rendered.get_text() == "若要继续，请先登录。 sign in"
    assert required(rendered.find("a"))["href"] == "/login"
    assert required(rendered.find("a")).get_text() == "sign in"
    assert "↩" not in rendered.get_text()


def test_attribute_only_content_and_textarea_values_are_preserved(tmp_path):
    chapter = read(
        tmp_path,
        '<textarea placeholder="Write here">Keep draft</textarea>'
        '<input type="password" value="private-value" placeholder="Password"/>',
    ).chapters[0]
    assert [segment.source for segment in chapter.segments] == ["Write here", "Password"]
    for segment in chapter.segments:
        segment.target = '<script>alert("not HTML")</script>'
    rendered = BeautifulSoup(_render_chapter_html(chapter, bilingual=True), "html.parser")
    assert required(rendered.find("textarea")).get_text() == "Keep draft"
    assert required(rendered.find("input"))["value"] == "private-value"
    assert rendered.find("script") is None
    assert required(rendered.find("input"))["placeholder"] == '<script>alert("not HTML")</script>'
    assert rendered.find(class_="tn-source") is None


def test_hidden_headings_do_not_leak_into_chapter_titles(tmp_path):
    document = read(
        tmp_path,
        "<h1>Chapter <span hidden>Hidden title</span>One</h1><p>Body.</p>",
    )
    assert document.chapters[0].title == "Chapter One"
    assert "Hidden title" not in "\n".join(
        segment.source for segment in document.chapters[0].segments
    )


@pytest.mark.parametrize("fmt", ["txt", "markdown", "docx"])
def test_attribute_segments_do_not_become_body_paragraphs(tmp_path, fmt):
    document = read(tmp_path, '<p>Body.</p><img src="missing.png" alt="A caption"/>')
    store = FileStorage(str(tmp_path / "state"))
    store.init_from_document(document)
    chapter = document.chapters[0]
    for segment in chapter.segments:
        segment.target = "正文。" if segment.source == "Body." else "图片说明"
    store.save_chapter(chapter)
    output = tmp_path / f"translated.{fmt}"
    assemble(store, document.source_path, out_path=str(output), out_format=fmt)
    if fmt == "docx":
        from docx import Document

        text = "\n".join(paragraph.text for paragraph in Document(output).paragraphs)
    else:
        text = output.read_text()
    assert "正文。" in text
    assert "图片说明" not in text


@pytest.mark.parametrize("initial_fallback", [False, True], ids=["aligned", "repaired"])
def test_full_translation_pipeline_and_resume_reuse_html_alignment(tmp_path, initial_fallback):
    document = read(
        tmp_path,
        '<p>Please <a href="/login"><strong>sign in</strong></a> to continue.</p>',
    )
    config = Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {"preset": "fake"},
            "pipeline": {
                "book_understanding": False,
                "polish": False,
                "review": False,
                "annotation_alignment_concurrency": 1,
            },
            "paths": {"state_dir": str(tmp_path / "state")},
            "output": {"about_page": False},
        }
    )
    alignment_calls = 0

    def handler(messages, tier, json_mode):
        nonlocal alignment_calls
        if "INPUT JSON:\n" in messages[-1]["content"]:
            alignment_calls += 1
            if initial_fallback and alignment_calls <= 2:
                return "{}"
            return focus_response(messages)
        if "literary translator" in messages[0]["content"]:
            return json.dumps({"translations": ["若要继续，请先登录。"]})
        return routing_handler(messages, tier, json_mode)

    client = FakeClient(handler=handler)
    runner = Orchestrator(config, client=client)
    output = tmp_path / "translated.html"
    runner.run_all(document.source_path, out_format="html", out_path=str(output))
    rendered = BeautifulSoup(output.read_text(), "html.parser")
    assert required(rendered.select_one("p > a > strong")).get_text() == "登录"
    assert alignment_calls == (4 if initial_fallback else 2)
    calls = len(client.calls)
    runner.run_all(document.source_path, out_format="html", out_path=str(output))
    assert len(client.calls) == calls
