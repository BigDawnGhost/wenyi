"""Protected EPUB objects use copied markers, semantic alignment, or pending state."""

from __future__ import annotations

import json
import zipfile

import pytest
from bs4 import BeautifulSoup, Tag
from wenyi_core.agents.annotation_aligner import target_digest
from wenyi_core.agents.precision import PrecisionAgent, PrecisionInputs
from wenyi_core.alignment import AlignmentUnit, unresolved_alignment
from wenyi_core.alignment.objects import marked_sources
from wenyi_core.assemble.html_renderer import _render_segments_html
from wenyi_core.assemble.writer import assemble
from wenyi_core.config import Config
from wenyi_core.ingest.epub_contexts import _build_epub_annotation_contexts
from wenyi_core.ingest.epub_reader import read_epub
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.markup.contexts import annotation_contexts_for_segments
from wenyi_core.markup.ruby import strip_ruby_markers
from wenyi_core.markup.segments import annotate_epub_resource
from wenyi_core.models.document import Chapter, Segment
from wenyi_core.pipeline.annotate.annotations import AnnotationService
from wenyi_core.pipeline.prepare.epub_objects import refresh_epub_objects
from wenyi_core.pipeline.prepare.input_preparation import (
    EPUB_INGEST_SCHEMA,
    ingest_config,
    load_parsed_document,
    parse_document,
)
from wenyi_core.pipeline.run.runtime import PipelineRuntime
from wenyi_core.pipeline.translate.translation_batch import BatchPlan, TranslationBatchExecutor
from wenyi_core.storage.file import FileStorage

HTML = (
    '<html><body><p>Let <math xmlns="http://www.w3.org/1998/Math/MathML">'
    "<msup><mi>x</mi><mn>2</mn></msup></math> be positive.</p></body></html>"
)


def parsed():
    _, segments, template = annotate_epub_resource(HTML, 0, "chapter.xhtml")
    item = segments[0].meta["epub_annotations"]["items"][0]
    return segments, template, item


def service(tmp_path, handler):
    config = Config.from_dict({"llm": {"preset": "fake"}, "pipeline": {"polish": False}})
    client = FakeClient(handler=handler)
    runtime = PipelineRuntime(config, client)
    return AnnotationService(runtime), FileStorage(str(tmp_path / "state")), client


def test_math_uses_annotation_context_without_changing_source():
    segments, template, item = parsed()
    assert segments[0].source == "Let be positive."
    assert item["relation"] == "inline_object"
    assert item["mode"] == "point"
    resource = {"href": "chapter.xhtml", "segments": segments, "template": template}
    registry = _build_epub_annotation_contexts([resource], [resource])
    contexts = annotation_contexts_for_segments(segments, registry)
    assert contexts[0][0]["marker"] == item["marker"]
    assert contexts[0][0]["source_offset"] == str(item["source_start"])
    assert "<msup>" in contexts[0][0]["source"]


def test_copied_marker_restores_math_without_alignment_call(tmp_path):
    segments, template, item = parsed()
    segments[0].target = f"设{item['marker']}为正数。"
    chapter = Chapter(index=0, segments=segments)
    annotations, store, client = service(
        tmp_path, lambda *_: pytest.fail("Copied object markers need no model call")
    )
    annotations.align_annotations_after_batch(0, chapter, 0, 1, store)
    saved = store.load_chapter(0)
    assert saved.segments[0].target == "设为正数。"
    placement = saved.segments[0].meta["epub_annotations"]["placements"][0]
    assert placement["method"] == "copied_object_marker"
    assert placement["target_start"] == 1
    rendered = BeautifulSoup(_render_segments_html(template, saved.segments), "html.parser")
    paragraph = rendered.find("p")
    assert isinstance(paragraph, Tag)
    assert [
        child.name if isinstance(child, Tag) else str(child) for child in paragraph.children
    ] == [
        "设",
        "math",
        "为正数。",
    ]
    math = rendered.find("math")
    assert isinstance(math, Tag) and math.find("msup") is not None
    annotations.align_annotations_after_batch(0, saved, 0, 1, store)
    assert not client.calls


@pytest.mark.parametrize("damage", ["missing", "duplicate", "changed"])
def test_missing_or_duplicate_marker_uses_annotation_alignment(tmp_path, damage):
    segments, template, item = parsed()
    marker = {"missing": "", "duplicate": item["marker"] * 2, "changed": "〘object:changed〙"}[
        damage
    ]
    segments[0].target = f"设{marker}为正数。"

    def handler(messages, *_):
        payload = json.loads(messages[-1]["content"].split("INPUT JSON:\n", 1)[1])
        assert payload["object_reference"]["source"]
        assert payload["immutable_target"] == "设为正数。"
        return json.dumps(
            {"id": item["id"], "search": "设为", "replacement": f"设{payload['markers'][0]}为"}
        )

    annotations, store, client = service(tmp_path, handler)
    chapter = Chapter(index=0, segments=segments)
    annotations.align_annotations_after_batch(0, chapter, 0, 1, store)
    saved = store.load_chapter(0)
    assert len(client.calls) == 1
    assert saved.segments[0].target == "设为正数。"
    assert (
        saved.segments[0].meta["epub_annotations"]["placements"][0]["method"] == "llm_local_patch"
    )
    assert BeautifulSoup(_render_segments_html(template, saved.segments), "html.parser").math


def test_failed_object_alignment_stays_pending_and_is_omitted_from_export(tmp_path, caplog):
    segments, template, item = parsed()
    segments[0].target = "设为正数。"
    annotations, store, client = service(
        tmp_path, lambda *_: json.dumps({"id": item["id"], "abstain": True})
    )
    chapter = Chapter(index=0, segments=segments)
    annotations.align_annotations_after_batch(0, chapter, 0, 1, store)
    saved = store.load_chapter(0)
    placement = saved.segments[0].meta["epub_annotations"]["placements"][0]
    assert placement["status"] == "pending"
    assert placement["target_start"] is None
    rendered = BeautifulSoup(_render_segments_html(template, saved.segments), "html.parser")
    assert rendered.math is None
    assert "设为正数。" in rendered.get_text()
    assert any("Omitting protected object" in message for message in caplog.messages)
    annotations.align_annotations_after_batch(0, saved, 0, 1, store)
    assert len(client.calls) == 1


def test_object_markers_wait_for_final_continuation(tmp_path):
    segments, template, item = parsed()
    first = segments[0]
    first.source, first.target = "Let ", f"设{item['marker']}"
    chapter = Chapter(
        index=0,
        segments=[first, Segment(index=1, source="be positive.", cont=True)],
    )
    annotations, store, client = service(tmp_path, lambda *_: pytest.fail("No model needed"))
    annotations.align_annotations_after_batch(0, chapter, 0, 1, store)
    assert first.target.endswith(item["marker"])
    chapter.segments[1].target = "为正数。"
    annotations.align_annotations_after_batch(0, chapter, 1, 1, store)
    saved = store.load_chapter(0)
    assert "".join(s.target or "" for s in saved.segments) == "设为正数。"
    assert BeautifulSoup(_render_segments_html(template, saved.segments), "html.parser").math
    assert not client.calls


def registry_for(segments, template):
    resource = {"href": "chapter.xhtml", "segments": segments, "template": template}
    return _build_epub_annotation_contexts([resource], [resource])


@pytest.mark.parametrize("polish", [False, True])
@pytest.mark.parametrize("drop_marker", [False, True])
def test_translation_and_polish_receive_object_context_and_preserve_ruby_distinction(
    tmp_path, polish, drop_marker
):
    segments, template, item = parsed()
    registry = registry_for(segments, template)
    contexts = annotation_contexts_for_segments(segments, registry)
    marker = item["marker"]

    def handler(messages, *_):
        if "INPUT JSON:\n" in messages[-1]["content"]:
            payload = json.loads(messages[-1]["content"].split("INPUT JSON:\n", 1)[1])
            assert "<msup>" in payload["object_reference"]["source"]
            return json.dumps(
                {"id": item["id"], "search": "设为", "replacement": f"设{payload['markers'][0]}为"}
            )
        if len(messages) == 4:
            return json.dumps({"polished": [f"设{'' if drop_marker else marker}为正数。"]})
        assert f"Let {marker}be positive." in messages[-1]["content"]
        assert "<msup>" in messages[-1]["content"]
        return json.dumps({"translations": [f"设{marker}为正数。"]})

    annotations, store, client = service(tmp_path, handler)
    store.save_annotation_contexts(registry)
    runtime = annotations._runtime
    plan = BatchPlan.capture(0, 0, segments, [], "", "", "", "", contexts, "")
    result = TranslationBatchExecutor(runtime.translator, runtime.polisher).execute(
        plan, polish=polish
    )
    segments[0].target = result.targets[0]
    segments[0].target_before_polish = result.before_polish[0]
    chapter = Chapter(index=0, segments=segments)
    annotations.align_annotations_after_batch(0, chapter, 0, 1, store)
    assert chapter.segments[0].target == "设为正数。"
    if polish:
        assert chapter.segments[0].target_before_polish == "设为正数。"
    expected_fallback = polish and drop_marker
    assert (
        sum(call["operation"] == "annotation.align" for call in client.calls) == expected_fallback
    )
    assert strip_ruby_markers(f"字〘じ〙{marker}") == f"字{marker}"


def test_precision_packet_marks_sources_using_same_annotation_context():
    segments, template, item = parsed()
    contexts = annotation_contexts_for_segments(segments, registry_for(segments, template))
    config = Config.from_dict({"llm": {"preset": "fake"}})
    agent = PrecisionAgent(FakeClient(), config)
    packet = agent._packet(PrecisionInputs((segments[0].source,), (), "", "", "", "", contexts, ""))
    assert item["marker"] in packet
    assert "<msup>" in packet


def test_slice_boundaries_receive_each_object_marker_once():
    segments, template, item = parsed()
    registry = registry_for(segments, template)
    first = segments[0]
    first.source = "Let "
    pieces = [first, Segment(index=1, source="be positive.", cont=True)]
    contexts = annotation_contexts_for_segments(pieces, registry)
    marked = marked_sources([s.source for s in pieces], contexts)
    assert marked == [f"Let {item['marker']}", "be positive."]


def test_disabled_alignment_still_accepts_copied_markers_and_persists_missing_as_pending(tmp_path):
    segments, template, item = parsed()
    annotations, store, client = service(tmp_path, lambda *_: pytest.fail("Alignment disabled"))
    annotations._runtime.config.pipeline.annotation_alignment = False
    chapter = Chapter(index=0, segments=segments)
    segments[0].target = f"设{item['marker']}为正数。"
    annotations.align_annotations_after_batch(0, chapter, 0, 1, store)
    assert segments[0].meta["epub_annotations"]["placements"][0]["status"] == "aligned"
    segments[0].target = "假定为正数。"
    annotations.align_annotations_after_batch(0, chapter, 0, 1, store)
    assert segments[0].meta["epub_annotations"]["placements"][0]["status"] == "pending"
    rendered = BeautifulSoup(_render_segments_html(template, segments), "html.parser")
    assert rendered.math is None
    assert "假定为正数。" in rendered.get_text()
    assert not client.calls


def write_epub(path, html=HTML):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr(
            "META-INF/container.xml",
            '<container><rootfiles><rootfile full-path="content.opf"/></rootfiles></container>',
        )
        archive.writestr(
            "content.opf",
            "<package><metadata><title>Math</title></metadata><manifest>"
            '<item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>'
            '</manifest><spine><itemref idref="chapter"/></spine></package>',
        )
        archive.writestr("chapter.xhtml", html)


def test_resume_refreshes_old_project_and_exports_pending_objects_as_omissions(tmp_path):
    path = tmp_path / "source.epub"
    write_epub(path)
    doc = read_epub(str(path), "en", "zh")
    annotations, store, client = service(tmp_path, lambda *_: '{"abstain":true}')
    manifest = store.stage_document(doc)
    manifest["meta"].pop("epub_objects")
    store.save_manifest(manifest)
    chapter = store.load_chapter(0)
    chapter.segments[0].meta.pop("epub_annotations")
    chapter.segments[0].target = "设为正数。"
    store.save_chapter_with_status(chapter, "done")
    with store.lock():
        refresh_epub_objects(store, str(path))
    saved = store.load_chapter(0)
    assert saved.segments[0].target == "设为正数。"
    assert store.load_manifest()["chapters"][0]["status"] == "done"
    annotations.repair_fallbacks(store)
    saved = store.load_chapter(0)
    assert saved.segments[0].meta["epub_annotations"]["placements"][0]["status"] == "pending"
    output = tmp_path / "output.epub"
    output.write_bytes(b"existing export")
    assemble(store, str(path), str(output), out_format="epub", about_page=False)
    with zipfile.ZipFile(output) as archive:
        rendered = BeautifulSoup(archive.read("chapter.xhtml"), "html.parser")
    assert rendered.math is None
    assert "设为正数。" in rendered.get_text()
    assert len(client.calls) == 1


def test_pending_objects_retry_without_retranslating_completed_text(tmp_path):
    path = tmp_path / "source.epub"
    write_epub(path)
    doc = read_epub(str(path), "en", "zh")
    annotations, store, client = service(tmp_path, lambda *_: '{"abstain":true}')
    store.save_manifest(store.stage_document(doc))
    chapter = store.load_chapter(0)
    chapter.segments[0].target = "设为正数。"
    annotations.align_annotations_after_batch(0, chapter, 0, 1, store)
    item = chapter.segments[0].meta["epub_annotations"]["items"][0]

    def repaired(messages, *_):
        payload = json.loads(messages[-1]["content"].split("INPUT JSON:\n", 1)[1])
        return json.dumps(
            {"id": item["id"], "search": "设为", "replacement": f"设{payload['markers'][0]}为"}
        )

    client.handler = repaired
    # Completed failures do not recharge unchanged inputs.
    annotations.repair_fallbacks(store)
    assert len(client.calls) == 1
    changed = store.load_chapter(0)
    changed.segments[0].target = "设为正数！"
    store.save_chapter(changed)
    annotations.repair_fallbacks(store)
    saved = store.load_chapter(0)
    assert saved.segments[0].target == "设为正数！"
    assert saved.segments[0].meta["epub_annotations"]["placements"][0]["status"] == "aligned"


def test_unique_markers_align_independently_when_another_object_marker_is_duplicated(tmp_path):
    html = HTML.replace(
        " be positive.",
        '<math xmlns="http://www.w3.org/1998/Math/MathML"><mi>y</mi></math> be positive.',
    )
    _, segments, template = annotate_epub_resource(html, 0, "chapter.xhtml")
    one, two = segments[0].meta["epub_annotations"]["items"]
    segments[0].target = f"设{one['marker']}和{two['marker'] * 2}为正数。"

    def handler(messages, *_):
        payload = json.loads(messages[-1]["content"].split("INPUT JSON:\n", 1)[1])
        assert payload["id"] == two["id"]
        return json.dumps(
            {"id": two["id"], "search": "和为", "replacement": f"和{payload['markers'][0]}为"}
        )

    annotations, store, client = service(tmp_path, handler)
    chapter = Chapter(index=0, segments=segments)
    annotations.align_annotations_after_batch(0, chapter, 0, 1, store)
    placements = segments[0].meta["epub_annotations"]["placements"]
    assert [row["method"] for row in placements] == ["copied_object_marker", "llm_local_patch"]
    assert [row["target_start"] for row in placements] == [1, 2]
    assert len(client.calls) == 1
    rendered = BeautifulSoup(_render_segments_html(template, segments), "html.parser")
    assert len(rendered.find_all("math")) == 2


def test_literal_marker_text_and_math_ids_are_not_consumed_or_hoisted(tmp_path):
    literal = "〘object:tn0_0_inline_0〙"
    html = HTML.replace("Let ", f"Let {literal} ").replace(
        "<msup><mi>x</mi>", '<msup id="power"><mi id="variable">x</mi>'
    )
    _, segments, template = annotate_epub_resource(html, 0, "chapter.xhtml")
    item = segments[0].meta["epub_annotations"]["items"][0]
    assert item["marker"] != literal
    segments[0].target = f"保留{literal}，设{item['marker']}为正数。"
    annotations, store, _ = service(tmp_path, lambda *_: pytest.fail("Copied marker"))
    annotations.align_annotations_after_batch(0, Chapter(index=0, segments=segments), 0, 1, store)
    assert literal in segments[0].target
    rendered = BeautifulSoup(_render_segments_html(template, segments), "html.parser")
    power = rendered.select_one("math msup")
    variable = rendered.select_one("math mi")
    assert isinstance(power, Tag) and isinstance(variable, Tag)
    assert power["id"] == "power"
    assert variable["id"] == "variable"


def test_unclosed_known_marker_is_cleaned_then_repaired_without_rewriting_prose(tmp_path):
    segments, template, item = parsed()
    segments[0].target = f"设{item['marker'][:-1]}为正数。"

    def handler(messages, *_):
        payload = json.loads(messages[-1]["content"].split("INPUT JSON:\n", 1)[1])
        return json.dumps(
            {"id": item["id"], "search": "设", "replacement": "设" + payload["markers"][0]}
        )

    annotations, store, client = service(tmp_path, handler)
    annotations.align_annotations_after_batch(0, Chapter(index=0, segments=segments), 0, 1, store)
    placement = segments[0].meta["epub_annotations"]["placements"][0]
    assert placement["status"] == "aligned"
    assert placement["method"] == "llm_local_patch"
    assert segments[0].target == "设为正数。"
    assert BeautifulSoup(_render_segments_html(template, segments), "html.parser").math
    assert len(client.calls) == 1


def test_refresh_and_repair_keep_successful_note_placements_without_paying_again(tmp_path):
    path = tmp_path / "source.epub"
    html = HTML.replace(
        " be positive.",
        ' be positive.<sup><a epub:type="noteref" href="#note">1</a></sup>',
    ).replace("</body>", '<aside id="note" epub:type="footnote"><p>Explanation.</p></aside></body>')
    write_epub(path, html)
    doc = read_epub(str(path), "en", "zh")

    def handler(messages, *_):
        payload = json.loads(messages[-1]["content"].split("INPUT JSON:\n", 1)[1])
        assert "object_reference" in payload
        return json.dumps(
            {"id": payload["id"], "search": "设为", "replacement": f"设{payload['markers'][0]}为"}
        )

    annotations, store, client = service(tmp_path, handler)
    manifest = store.stage_document(doc)
    manifest["meta"].pop("epub_objects")
    store.save_manifest(manifest)
    chapter = store.load_chapter(0)
    paragraph = chapter.segments[0]
    paragraph.target = "设为正数。"
    metadata = paragraph.meta["epub_annotations"]
    metadata["items"] = [item for item in metadata["items"] if item["relation"] != "inline_object"]
    note = {
        "id": metadata["items"][0]["id"],
        "mode": "point",
        "target_start": 5,
        "target_end": 5,
        "status": "aligned",
        "method": "llm_local_patch",
    }
    metadata.update(target_digest=target_digest(paragraph.target), placements=[note])
    chapter.segments[1].target = "说明。"
    store.save_chapter_with_status(chapter, "done")
    refresh_epub_objects(store, str(path))
    annotations.repair_fallbacks(store)
    saved = store.load_chapter(0)
    assert note in saved.segments[0].meta["epub_annotations"]["placements"]
    assert len(client.calls) == 1


def test_old_parsed_cache_is_not_used_to_initialize_object_blind_state(tmp_path):
    path = tmp_path / "source.epub"
    write_epub(path)
    doc = read_epub(str(path), "en", "zh")
    config = Config.from_dict({"llm": {"preset": "fake"}})
    _, store, _ = service(tmp_path, lambda *_: pytest.fail("Parsing is model-free"))
    manifest = store.stage_document(doc)
    store.save_manifest(manifest)
    doc.meta.pop("epub_objects")
    store.write_artifact(
        "parsed_document.json",
        {
            "source_sha256": manifest["source_sha256"],
            "ingest_config": ingest_config(config),
            "document": doc.model_dump(mode="json"),
        },
    )
    assert load_parsed_document(store, str(path), config) is None


def test_parsed_cache_from_older_epub_reader_is_reparsed(tmp_path):
    path = tmp_path / "source.epub"
    write_epub(path)
    doc = read_epub(str(path), "en", "zh")
    config = Config.from_dict({"llm": {"preset": "fake"}})
    _, store, _ = service(tmp_path, lambda *_: pytest.fail("Parsing is model-free"))
    manifest = store.stage_document(doc)
    store.save_manifest(manifest)
    cache = {
        "source_sha256": manifest["source_sha256"],
        "ingest_config": ingest_config(config),
        "document": doc.model_dump(mode="json"),
    }
    store.write_artifact("parsed_document.json", cache)
    assert load_parsed_document(store, str(path), config) is not None

    # A pre-upgrade cache lacks creators and navigation labels. Read-only views keep using it,
    # while a parse run refreshes it from the source.
    cache["document"]["meta"]["epub_schema"] = 5
    store.write_artifact("parsed_document.json", cache)
    assert load_parsed_document(store, str(path), config) is not None
    reparsed = parse_document(store, str(path), config)
    assert reparsed.meta["epub_schema"] == EPUB_INGEST_SCHEMA
    refreshed = load_parsed_document(store, str(path), config)
    assert refreshed is not None and refreshed.meta["epub_schema"] == EPUB_INGEST_SCHEMA


def test_unclosed_marker_failure_does_not_discard_valid_sibling(tmp_path):
    html = HTML.replace(" be positive.", "<math><mi>y</mi></math> be positive.")
    _, segments, template = annotate_epub_resource(html, 0, "chapter.xhtml")
    one, two = segments[0].meta["epub_annotations"]["items"]
    target = f"令{one['marker'][:-1]}为正数，另令{two['marker']}为负数。"
    segments[0].target = segments[0].target_before_polish = target
    annotations, store, client = service(tmp_path, lambda *_: '{"abstain":true}')
    annotations.align_annotations_after_batch(0, Chapter(index=0, segments=segments), 0, 1, store)
    saved = store.load_chapter(0)
    assert saved.segments[0].target == "令为正数，另令为负数。"
    assert saved.segments[0].target_before_polish == target
    assert [row["status"] for row in saved.segments[0].meta["epub_annotations"]["placements"]] == [
        "pending",
        "aligned",
    ]
    annotations.align_segment_annotation(0, saved, 0, store, retry_fallbacks=True)
    assert len(client.calls) == 1
    rendered = BeautifulSoup(_render_segments_html(template, saved.segments), "html.parser")
    assert len(rendered.find_all("math")) == 1
    saved.segments[0].target = f"令{one['marker']}为正数，另令{two['marker']}为负数。"
    annotations.align_annotations_after_batch(0, saved, 0, 1, store)
    repaired = store.load_chapter(0)
    assert repaired.segments[0].target == "令为正数，另令为负数。"
    rendered = BeautifulSoup(_render_segments_html(template, repaired.segments), "html.parser")
    assert len(rendered.find_all("math")) == 2
    assert len(client.calls) == 1


@pytest.mark.parametrize("out_format", ["epub", "html"])
@pytest.mark.parametrize("bilingual", [False, True])
def test_adjacent_object_order_survives_save_resume_and_export(tmp_path, out_format, bilingual):
    path = tmp_path / "source.epub"
    write_epub(path, HTML.replace(" be positive.", "<math><mi>y</mi></math> be positive."))
    doc = read_epub(str(path), "en", "zh")
    annotations, store, client = service(tmp_path, lambda *_: pytest.fail("Copied markers"))
    store.save_manifest(store.stage_document(doc))
    chapter = store.load_chapter(0)
    one, two = chapter.segments[0].meta["epub_annotations"]["items"]
    chapter.segments[0].target = f"使用{two['marker']}{one['marker']}。"
    annotations.align_annotations_after_batch(0, chapter, 0, 1, store)
    annotations.repair_fallbacks(store)
    output = tmp_path / f"output.{out_format}"
    assemble(
        store,
        str(path),
        str(output),
        out_format=out_format,
        bilingual=bilingual,
        about_page=False,
    )
    if out_format == "epub":
        with zipfile.ZipFile(output) as archive:
            rendered = archive.read("chapter.xhtml").decode()
    else:
        rendered = output.read_text()
    soup = BeautifulSoup(rendered, "html.parser")
    target = soup.select_one("p:not(.tn-source)")
    assert isinstance(target, Tag)
    assert [math.get_text() for math in target.find_all("math")] == ["y", "x2"]
    source = soup.select_one(".tn-source")
    if bilingual:
        assert isinstance(source, Tag)
        assert [math.get_text() for math in source.find_all("math")] == ["x2", "y"]
    else:
        assert source is None
    placements = store.load_chapter(0).segments[0].meta["epub_annotations"]["placements"]
    assert [row["target_order"] for row in placements] == [1, 0]
    assert not client.calls


def test_format_service_adapts_to_an_injected_text_locator(tmp_path):
    html = HTML.replace(" be positive.", ' be positive.<sup><a href="#note">1</a></sup>')
    _, segments, template = annotate_epub_resource(html, 0, "chapter.xhtml")
    segments[0].target = "设为正数。"
    original, store, client = service(tmp_path, lambda *_: pytest.fail("No model agent"))
    store.save_annotation_contexts(registry_for(segments, template))
    calls = []

    class Locator:
        def align_unit(self, unit: AlignmentUnit):
            calls.append(unit)
            return unresolved_alignment(unit)

    annotations = AnnotationService(original._runtime, aligner=Locator())
    annotations.align_annotations_after_batch(0, Chapter(index=0, segments=segments), 0, 1, store)
    placements = store.load_chapter(0).segments[0].meta["epub_annotations"]["placements"]
    assert [row["status"] for row in placements] == ["pending", "fallback"]
    assert placements[0]["target_start"] is None
    assert placements[1]["target_start"] == len("设为正数。")
    assert len(calls) == 1
    assert all(
        set(item)
        <= {"id", "mode", "source_start", "source_end", "object_reference", "validation_errors"}
        for item in calls[0].items
    )
    assert "<msup>" in calls[0].items[0]["object_reference"]["source"]
    assert calls[0].items[0]["validation_errors"][0]["code"] == "missing_marker"
    assert "object_reference" not in calls[0].items[1]
    assert not client.calls


@pytest.mark.parametrize("out_format", ["epub", "html"])
@pytest.mark.parametrize("bilingual", [False, True])
def test_read_only_export_omits_broken_object_but_keeps_valid_sibling(
    tmp_path, out_format, bilingual, caplog
):
    path = tmp_path / "source.epub"
    write_epub(path, HTML.replace(" be positive.", "<math><mi>y</mi></math> be positive."))
    doc = read_epub(str(path), "en", "zh")
    _, store, client = service(tmp_path, lambda *_: pytest.fail("Export must not call a model"))
    store.save_manifest(store.stage_document(doc))
    chapter = store.load_chapter(0)
    one, two = chapter.segments[0].meta["epub_annotations"]["items"]
    chapter.segments[0].target = f"使用{one['marker'][:-1]}和{two['marker']}。"
    store.save_chapter(chapter)
    before = store.load_chapter(0)
    output = tmp_path / f"output.{out_format}"
    assemble(
        store, str(path), str(output), out_format=out_format, bilingual=bilingual, about_page=False
    )
    if out_format == "epub":
        with zipfile.ZipFile(output) as archive:
            rendered = archive.read("chapter.xhtml").decode()
    else:
        rendered = output.read_text()
    soup = BeautifulSoup(rendered, "html.parser")
    target = soup.select_one("p:not(.tn-source)")
    assert target is not None
    assert target.get_text() == "使用和y。"
    assert [math.get_text() for math in target.find_all("math")] == ["y"]
    if bilingual:
        source = soup.select_one(".tn-source")
        assert source is not None
        assert len(source.find_all("math")) == 2
    assert any("Omitting protected object" in message for message in caplog.messages)
    assert store.load_chapter(0) == before
    assert not client.calls
