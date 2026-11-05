"""Annotation alignment routing, immutable-text validation, export, and resume tests."""

from __future__ import annotations

import json
import threading
from dataclasses import replace

import pytest
from bs4 import BeautifulSoup
from bs4.element import Tag
from wenyi_core.agents.annotation_aligner import AnnotationAligner, AnnotationUnit, target_digest
from wenyi_core.assemble.html_renderer import _render_segments_html
from wenyi_core.config import Config
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.markup.segments import annotate_epub_resource
from wenyi_core.models.document import Chapter, Segment
from wenyi_core.pipeline.annotate.annotations import AnnotationService
from wenyi_core.pipeline.annotate.docx_styles import DocxStyleService
from wenyi_core.pipeline.run.runtime import PipelineRuntime
from wenyi_core.storage.file import FileStorage


def unit(*, target="阿达见到了博。"):
    return AnnotationUnit(
        "paragraph",
        "Ada met Bo.",
        target,
        (
            {"id": "name", "mode": "range", "source_start": 0, "source_end": 3},
            {"id": "note", "mode": "point", "source_start": 10, "source_end": 10},
        ),
    )


def request(messages):
    return json.loads(messages[-1]["content"].split("INPUT JSON:\n", 1)[1])


def valid_response(payload):
    if "source_with_focus" in payload:
        assert set(payload) == {"source_with_focus", "immutable_target"}
        assert payload["source_with_focus"] == "<focus>Ada</focus> met Bo."
        return {"target_quote": "阿达", "context": "阿达见到了博"}
    assert payload["id"] == "note"
    assert payload["source_with_markers"] == "Ada met Bo⟪wenyi_alignment_note⟫."
    return {
        "id": "note",
        "search": "博。",
        "replacement": f"博{payload['markers'][0]}。",
    }


def align(value, handler, *, concurrency=4):
    config = Config.from_dict(
        {"llm": {"preset": "fake"}, "pipeline": {"annotation_alignment_concurrency": concurrency}}
    )
    client = FakeClient(handler=lambda messages, *_: json.dumps(handler(request(messages))))
    return AnnotationAligner(client, config).align_unit(value), client


def test_production_routes_one_annotation_per_request_without_editing_target():
    value = unit()
    result, client = align(value, valid_response)
    assert not result.used_fallback
    assert result.target_digest == target_digest(value.target)
    assert value.target == "阿达见到了博。"
    assert [row["id"] for row in result.placements] == ["name", "note"]
    assert [row["method"] for row in result.placements] == ["llm_source_focus", "llm_local_patch"]
    assert [(row["target_start"], row["target_end"]) for row in result.placements] == [
        (0, 2),
        (6, 6),
    ]
    assert len(client.calls) == 2
    assert all(call["stage"] == "annotation.align" for call in client.calls)
    assert all(call["tier"] == "cheap" and call["json_mode"] for call in client.calls)
    assert all(call["max_tokens"] is None for call in client.calls)


@pytest.mark.parametrize(
    ("relation", "marker_text"),
    [("internal_link", ""), ("noteref", "1"), ("backlink", "↩")],
)
@pytest.mark.parametrize("padding", ["", "\n \t"])
def test_whole_source_range_maps_entire_target_without_model(relation, marker_text, padding):
    value = unit()
    source = padding + value.source + padding
    item = {
        **value.items[0],
        "source_start": len(padding),
        "source_end": len(padding) + len(value.source),
        "relation": relation,
        "marker_text": marker_text,
    }
    value = replace(value, source=source, items=(item,))
    result, client = align(value, lambda _: pytest.fail("Whole-source ranges need no model"))
    assert not client.calls
    assert not result.used_fallback
    assert result.target_digest == target_digest(value.target)
    assert result.placements == (
        {
            "id": "name",
            "mode": "range",
            "target_start": 0,
            "target_end": len(value.target),
            "status": "aligned",
            "method": "whole_source_range",
        },
    )


def test_whole_source_range_does_not_skip_model_alignment_for_other_items():
    value = unit()
    value = replace(
        value, items=({**value.items[0], "source_end": len(value.source)}, value.items[1])
    )
    result, client = align(value, valid_response)
    assert len(client.calls) == 1
    assert not result.used_fallback
    assert [row["method"] for row in result.placements] == ["whole_source_range", "llm_local_patch"]
    assert result.placements[0]["target_end"] == len(value.target)
    assert result.placements[1]["target_start"] == 6


def test_punctuation_outside_source_link_keeps_range_on_model_path():
    value = unit()
    value = replace(value, items=({**value.items[0], "source_end": len(value.source) - 1},))
    result, client = align(
        value, lambda _: {"target_quote": value.target[:-1], "context": value.target}
    )
    assert len(client.calls) == 1
    assert not result.used_fallback
    assert result.placements[0]["method"] == "llm_source_focus"
    assert result.placements[0]["target_end"] == len(value.target) - 1


@pytest.mark.parametrize("with_note_marker", [False, True])
def test_whole_source_link_rejoins_continuations_and_exports_without_model(
    tmp_path, with_note_marker
):
    marker = '<sup id="ref-note">1</sup>' if with_note_marker else ""
    html = f'<html><body><p><a href="notes.xhtml#n1">Ada met Bo.{marker}</a></p></body></html>'
    _, parsed, template = annotate_epub_resource(html, 0, "chapter.xhtml")
    first = parsed[0]
    source = first.source
    assert source == "Ada met Bo."
    first.source, first.target = source[:4], "阿达"
    chapter = Chapter(
        index=0,
        segments=[first, Segment(index=1, source=source[4:], target=None, cont=True)],
    )
    config = Config.from_dict({"llm": {"preset": "fake"}})
    client = FakeClient(handler=lambda *_: pytest.fail("Whole-source links need no model"))
    runtime = PipelineRuntime(config, client)
    service = AnnotationService(runtime)
    store = FileStorage(str(tmp_path / "book"))
    service.align_annotations_after_batch(0, chapter, 0, 1, store)
    assert "placements" not in first.meta["epub_annotations"]
    chapter.segments[1].target = "见到了博。"
    service.align_annotations_after_batch(0, chapter, 1, 1, store)
    saved = store.load_chapter(0)
    metadata = saved.segments[0].meta["epub_annotations"]
    assert metadata["placements"][0]["method"] == "whole_source_range"
    assert metadata["placements"][0]["target_start"] == 0
    assert metadata["placements"][0]["target_end"] == len("阿达见到了博。")
    service.align_annotations_after_batch(0, saved, 1, 1, store)
    assert not client.calls
    for bilingual in (False, True):
        rendered = BeautifulSoup(
            _render_segments_html(template, saved.segments, bilingual=bilingual, source_lang="en"),
            "html.parser",
        )
        paragraph = rendered.select_one("p:not(.tn-source)")
        assert paragraph is not None
        link = paragraph.find("a", href="notes.xhtml#n1")
        assert link is not None
        assert link.get_text().replace("1", "") == "阿达见到了博。"
        assert bool(link.find("sup", id="ref-note")) == with_note_marker
        assert paragraph.get_text() == link.get_text()


@pytest.mark.parametrize("failed_mode", ["point", "range"])
def test_invalid_response_only_falls_back_that_annotation_without_repair_calls(failed_mode):
    def handler(payload):
        mode = "range" if "source_with_focus" in payload else "point"
        return {} if mode == failed_mode else valid_response(payload)

    result, client = align(unit(), handler)
    assert result.used_fallback
    assert len(client.calls) == 2
    assert [row["status"] for row in result.placements] == (
        ["fallback", "aligned"] if failed_mode == "range" else ["aligned", "fallback"]
    )


@pytest.mark.parametrize(
    "bad_item",
    [
        {"source_start": True},
        {"source_start": -1},
        {"source_end": 100},
        {"source_end": 0},
        {"source_end": 10, "mode": "unknown"},
    ],
)
def test_corrupt_source_metadata_does_not_crash_or_poison_the_other_item(bad_item):
    value = unit()
    value = replace(value, items=(value.items[0], {**value.items[1], **bad_item}))
    result, client = align(value, valid_response)
    assert result.used_fallback
    assert result.placements[0]["status"] == "aligned"
    assert len(client.calls) == 1


@pytest.mark.parametrize("end", [0, -1, 100, True])
def test_empty_reversed_or_invalid_source_range_is_not_sent_to_model(end):
    value = unit()
    value = replace(value, items=({**value.items[0], "source_end": end},))
    result, client = align(value, lambda _: pytest.fail("Invalid range must be rejected locally"))
    assert result.used_fallback
    assert not client.calls


def test_focus_wrapper_collision_falls_back_without_guessing_a_span():
    value = replace(unit(), source="<focus>Ada</focus> met Bo.", items=unit().items[:1])
    result, client = align(value, lambda _: pytest.fail("Ambiguous wrapper must not be sent"))
    assert result.used_fallback
    assert not client.calls


def test_empty_targets_and_unannotated_units_do_not_call_model():
    for value in (unit(target=""), replace(unit(), items=())):
        result, client = align(value, lambda _: pytest.fail("No model call is needed"))
        assert result.target_digest == target_digest(value.target)
        assert not client.calls
        assert result.used_fallback == bool(value.items)


def test_duplicate_ids_are_rejected_before_requests():
    value = unit()
    value = replace(value, items=(value.items[0], {**value.items[1], "id": "name"}))
    result, client = align(value, lambda _: pytest.fail("Ambiguous identity must not be sent"))
    assert result.used_fallback
    assert not client.calls


def test_concurrency_cap_applies_across_all_annotations_not_separately_per_route():
    value = unit()
    items = tuple({**value.items[index % 2], "id": f"item-{index}"} for index in range(6))
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    active = maximum = 0

    def handler(payload):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            barrier.wait(timeout=5)
            if "source_with_focus" in payload:
                return {"target_quote": "阿达", "context": value.target}
            return {
                "id": payload["id"],
                "search": "博。",
                "replacement": f"博{payload['markers'][0]}。",
            }
        finally:
            with lock:
                active -= 1

    result, client = align(replace(value, items=items), handler, concurrency=2)
    assert maximum == 2
    assert len(client.calls) == 6
    assert not result.used_fallback
    assert [row["id"] for row in result.placements] == [item["id"] for item in items]


def test_repeated_range_quote_is_located_by_unique_context():
    value = replace(unit(), target="阿达离开了。阿达见到了博。", items=unit().items[:1])
    result, _ = align(value, lambda _: {"target_quote": "阿达", "context": "阿达见到了博"})
    assert not result.used_fallback
    assert result.placements[0]["target_start"] == len("阿达离开了。")


@pytest.mark.parametrize(
    "response",
    [
        {"target_quote": "阿达"},
        {"target_quote": "阿达", "context": "阿达"},
        {"target_quote": "阿达", "context": "阿达阿达"},
        {"target_quote": "阿达", "context": "not in the target"},
        {"abstain": True},
    ],
)
def test_missing_or_ambiguous_range_context_is_not_guessed(response):
    value = replace(unit(), target="阿达阿达", items=unit().items[:1])
    result, client = align(value, lambda _: response)
    assert result.used_fallback
    assert len(client.calls) == 1


def test_short_patch_in_long_target_uses_exact_codepoint_offsets():
    target = "序" * 16000 + "👩‍👩‍👧‍👦cafe\u0301。"
    value = replace(unit(target=target), items=unit().items[1:])
    result, _ = align(
        value,
        lambda payload: {
            "id": "note",
            "search": "cafe\u0301。",
            "replacement": f"cafe\u0301{payload['markers'][0]}。",
        },
    )
    assert not result.used_fallback
    assert result.placements[0]["target_start"] == len(target) - 1


def test_literal_marker_tokens_are_preserved_and_request_tokens_do_not_collide():
    value = replace(
        unit(), target="符号⟪literal⟫和⟪wenyi_alignment_note⟫。", items=unit().items[1:]
    )

    def handler(payload):
        assert payload["markers"] == ["⟪wenyi_alignment_x_note⟫"]
        return {
            "id": "note",
            "search": value.target,
            "replacement": value.target + payload["markers"][0],
        }

    result, _ = align(value, handler)
    assert not result.used_fallback
    assert result.placements[0]["target_start"] == len(value.target)


@pytest.mark.parametrize(
    "response",
    [
        {"id": "note", "search": "甲", "replacement": "甲⟪wenyi_alignment_note⟫"},
        {"id": "note", "search": "甲甲", "replacement": "甲甲⟪wenyi_alignment_note⟫"},
        {"id": "note", "search": "", "replacement": "⟪wenyi_alignment_note⟫"},
        {"id": "note", "search": "missing", "replacement": "missing⟪wenyi_alignment_note⟫"},
        {"id": "note", "search": "甲乙", "replacement": "甲已⟪wenyi_alignment_note⟫"},
        {"id": "note", "search": "甲乙", "replacement": "甲乙"},
        {
            "id": "note",
            "search": "甲乙",
            "replacement": "甲⟪wenyi_alignment_note⟫乙⟪wenyi_alignment_note⟫",
        },
        {"id": "note", "search": "甲乙", "replacement": "甲⟪unknown⟫乙"},
        {"id": "other", "search": "甲乙", "replacement": "甲乙⟪wenyi_alignment_note⟫"},
        {"id": "note", "abstain": True},
    ],
)
def test_unsafe_local_patches_fall_back_without_changing_target_or_retrying(response):
    value = replace(unit(target="甲甲甲乙"), items=unit().items[1:])
    result, client = align(value, lambda _: response)
    assert result.used_fallback
    assert result.placements[0]["status"] == "fallback"
    assert value.target == "甲甲甲乙"
    assert result.target_digest == target_digest(value.target)
    assert len(client.calls) == 1


@pytest.mark.parametrize("mode", ["point", "range"])
def test_unicode_normalization_is_rejected_in_both_protocols(mode):
    value = unit(target="cafe\u0301。")
    value = replace(value, items=tuple(item for item in value.items if item["mode"] == mode))
    response = (
        {"target_quote": "café", "context": value.target}
        if mode == "range"
        else {"id": "note", "search": "café。", "replacement": "café⟪wenyi_alignment_note⟫。"}
    )
    result, client = align(value, lambda _: response)
    assert result.used_fallback
    assert len(client.calls) == 1
    assert value.target == "cafe\u0301。"


def test_units_are_sequential_and_duplicate_unit_identity_makes_no_requests():
    config = Config.from_dict({"llm": {"preset": "fake"}})
    client = FakeClient(handler=lambda messages, *_: json.dumps(valid_response(request(messages))))
    aligner = AnnotationAligner(client, config)
    first = replace(unit(), items=unit().items[:1])
    second = replace(unit(), unit_id="second", items=unit().items[1:])
    results = aligner.align_units([first, second])
    assert [result.unit_id for result in results] == ["paragraph", "second"]
    assert all(not result.used_fallback for result in results)
    assert "source_with_focus" in request(client.calls[0]["messages"])
    assert "source_with_markers" in request(client.calls[1]["messages"])
    before = len(client.calls)
    assert all(result.used_fallback for result in aligner.align_units([first, first]))
    assert len(client.calls) == before


@pytest.mark.parametrize("kind", ["epub_annotations", "docx_styles"])
@pytest.mark.parametrize("fail", [False, True])
def test_services_persist_completed_continuations_and_reuse_success_or_fallback_on_resume(
    tmp_path, kind, fail
):
    value = unit()
    items = [dict(item) for item in value.items]
    if kind == "docx_styles":
        items = [{**items[0], "bold": True}]
    chapter = Chapter(
        index=0,
        segments=[
            Segment(
                index=0,
                source="Ada",
                target="阿达",
                anchor="paragraph",
                meta={kind: {"version": 1, "source_length": len(value.source), "items": items}},
            ),
            Segment(index=1, source=" met Bo.", target=None, cont=True),
        ],
    )
    config = Config.from_dict({"llm": {"preset": "fake"}})
    client = FakeClient(
        handler=lambda messages, *_: (
            "not JSON" if fail else json.dumps(valid_response(request(messages)))
        )
    )
    runtime = PipelineRuntime(config, client)
    if kind == "epub_annotations":
        run = AnnotationService(runtime).align_annotations_after_batch
    else:
        run = DocxStyleService(runtime).align_styles_after_batch
    store = FileStorage(str(tmp_path / "book"))
    run(0, chapter, 0, 1, store)
    assert not client.calls
    chapter.segments[1].target = "见到了博。"
    run(0, chapter, 1, 1, store)
    saved = store.load_chapter(0)
    metadata = saved.segments[0].meta[kind]
    assert metadata["target_digest"] == target_digest(value.target)
    assert len(metadata["placements"]) == len(items)
    assert all(
        row["status"] == ("fallback" if fail else "aligned") for row in metadata["placements"]
    )
    if kind == "docx_styles":
        assert metadata["placements"][0]["bold"] is True
        assert metadata["placements"][0]["method"] == (
            "proportional_source_range" if fail else "llm_source_focus"
        )
    assert [segment.target for segment in saved.segments] == ["阿达", "见到了博。"]
    paid_calls = len(client.calls)
    # Reopen persisted state and retry the interrupted batch, with no new calls/events.
    resumed_store = FileStorage(str(tmp_path / "book"))
    run(0, resumed_store.load_chapter(0), 1, 1, resumed_store)
    assert len(client.calls) == paid_calls == len(items)
    # A changed formal target must not reuse old offsets.
    previous_target = saved.segments[1].target
    assert previous_target is not None
    saved.segments[1].target = previous_target + "后来。"
    run(0, saved, 1, 1, resumed_store)
    assert len(client.calls) == paid_calls * 2
    assert saved.segments[0].meta[kind]["target_digest"] == target_digest(value.target + "后来。")


@pytest.mark.parametrize("kind", ["epub_annotations", "docx_styles"])
def test_legacy_placements_remain_legacy_and_do_not_trigger_paid_realignments(tmp_path, kind):
    value = unit()
    item = {**value.items[0], "bold": True}
    legacy = {
        "id": "name",
        "mode": "range",
        "target_start": 0,
        "target_end": 2,
        "status": "aligned",
        "method": "llm_markers",
    }
    chapter = Chapter(
        index=0,
        segments=[
            Segment(
                index=0,
                source=value.source,
                target=value.target,
                meta={
                    kind: {
                        "items": [item],
                        "target_digest": target_digest(value.target),
                        "placements": [legacy],
                    }
                },
            )
        ],
    )
    config = Config.from_dict({"llm": {"preset": "fake"}})
    client = FakeClient(handler=lambda *_: pytest.fail("Upgrade must not invalidate paid caches"))
    runtime = PipelineRuntime(config, client)
    store = FileStorage(str(tmp_path / "book"))
    if kind == "epub_annotations":
        AnnotationService(runtime).align_annotations_after_batch(0, chapter, 0, 1, store)
    else:
        DocxStyleService(runtime).align_styles_after_batch(0, chapter, 0, 1, store)
    assert chapter.segments[0].meta[kind]["placements"] == [legacy]
    assert not client.calls


@pytest.mark.parametrize("bilingual", [False, True])
def test_hybrid_offsets_restore_epub_links_without_changing_visible_translation(bilingual):
    value = unit()
    result, _ = align(value, valid_response)
    template = """<html><body><p data-tn-id="paragraph">
<a data-tn-annotation-id="name" href="notes.xhtml#ada" id="ref-ada">Ada</a> met Bo<sup
data-tn-annotation-id="note"><a href="notes.xhtml#bo" id="ref-bo">1</a></sup>.
</p></body></html>"""
    segment = Segment(
        index=0,
        anchor="paragraph",
        source=value.source,
        target=value.target,
        meta={
            "epub_annotations": {
                "version": 1,
                "source_length": len(value.source),
                "items": list(value.items),
                "target_digest": result.target_digest,
                "placements": list(result.placements),
            }
        },
    )
    rendered = BeautifulSoup(
        _render_segments_html(template, [segment], bilingual=bilingual, source_lang="en"),
        "html.parser",
    )
    name = rendered.find("a", string="阿达")
    assert name is not None and name["href"] == "notes.xhtml#ada"
    paragraph = name.find_parent("p")
    assert paragraph is not None
    note = paragraph.find("a", href="notes.xhtml#bo")
    assert note is not None
    assert paragraph.get_text().replace("1", "").strip() == value.target
    assert isinstance(note.parent, Tag)
    assert str(note.parent.previous_sibling).endswith("博")
    assert str(note.parent.next_sibling).startswith("。")
