"""End-of-translation annotation repair without retranslating saved text."""

from __future__ import annotations

import json

import pytest
from wenyi_core.alignment import AlignmentUnit, unresolved_alignment
from wenyi_core.config import Config
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.llm.retrying import ProviderRequestError
from wenyi_core.markup.objects import annotation_placements
from wenyi_core.models.document import Chapter, Segment
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.storage.file import FileStorage
from wenyi_core.storage.sqlite import SqliteStorage

from tests.fake_llm import MeteredFakeClient


@pytest.fixture(params=[FileStorage, SqliteStorage], ids=["file", "sqlite"])
def store(tmp_path, request):
    storage = request.param(str(tmp_path / "book"))
    try:
        yield storage
    finally:
        storage.close()


def repair_config():
    return Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {"preset": "fake"},
            "pipeline": {"annotation_alignment_concurrency": 1},
        }
    )


def annotated_chapter(index=0):
    items = (
        {"id": "name", "mode": "range", "source_start": 0, "source_end": 3},
        {"id": "note", "mode": "point", "source_start": 10, "source_end": 10},
    )
    unit = AlignmentUnit("paragraph", "Ada met Bo.", "阿达见到了博。", items)
    result = unresolved_alignment(unit)
    placements = annotation_placements(result, items, unit.target)
    placements[0].update(status="aligned", method="llm_markers", target_start=0, target_end=2)
    return Chapter(
        index=index,
        segments=[
            Segment(
                index=0,
                source="Ada",
                target="阿达",
                anchor=f"paragraph-{index}",
                meta={
                    "epub_annotations": {
                        "version": 1,
                        "source_length": 11,
                        "items": list(items),
                        "target_digest": result.target_digest,
                        "placements": placements,
                    }
                },
            ),
            Segment(index=1, source=" met Bo.", target="见到了博。", cont=True),
        ],
    )


def save_book(store, chapters):
    for chapter in chapters:
        store.save_chapter(chapter)
    store.save_manifest(
        {"chapters": [{"index": chapter.index, "status": "done"} for chapter in chapters]}
    )


def repair_response(messages, *_):
    payload = json.loads(messages[-1]["content"].split("INPUT JSON:\n", 1)[1])
    assert payload["immutable_target"] == "阿达见到了博。"
    if "source_with_focus" in payload:
        return json.dumps({"target_quote": "阿达", "context": "阿达见到了博"})
    assert payload["id"] == "note"
    return json.dumps(
        {"id": "note", "search": "博。", "replacement": f"博{payload['markers'][0]}。"}
    )


def test_completed_book_retries_only_fallback_annotations_at_translation_end(store, monkeypatch):
    chapter = annotated_chapter()
    save_book(store, [chapter])
    before_manifest = store.load_manifest()
    successful = dict(chapter.segments[0].meta["epub_annotations"]["placements"][0])
    client = MeteredFakeClient(handler=repair_response)
    translation = Orchestrator(repair_config(), client)._translation
    monkeypatch.setattr(translation._titles, "run", lambda *args, **kwargs: None)

    with store.lock():
        translation.run(store, book_synopsis="")

    assert [call["operation"] for call in client.calls] == ["annotation.align"]
    saved = store.load_chapter(0)
    placements = saved.segments[0].meta["epub_annotations"]["placements"]
    assert placements[0] == successful
    assert [item["status"] for item in placements] == ["aligned", "aligned"]
    assert [segment.target for segment in saved.segments] == ["阿达", "见到了博。"]
    assert store.load_manifest() == before_manifest
    usage = store.load_usage()
    assert usage["totals"]["calls"] == 1
    repair_events = [
        event
        for event in store.list_events(limit=0)
        if event["event"] == "annotation_alignment_completed"
    ]
    assert len(repair_events) == 1
    assert repair_events[0]["annotations"] == 1
    assert repair_events[0]["retry_fallbacks"] is True

    with store.lock():
        translation.run(store, book_synopsis="")
    assert len(client.calls) == 1
    assert store.load_usage() == usage


def test_repair_retries_all_fallbacks_in_original_item_order(store):
    chapter = annotated_chapter()
    metadata = chapter.segments[0].meta["epub_annotations"]
    metadata["placements"][0]["status"] = "fallback"
    save_book(store, [chapter])
    client = FakeClient(handler=repair_response)
    service = Orchestrator(repair_config(), client)._annotations

    with store.lock():
        service.repair_fallbacks(store)

    placements = store.load_chapter(0).segments[0].meta["epub_annotations"]["placements"]
    assert [(item["id"], item["status"]) for item in placements] == [
        ("name", "aligned"),
        ("note", "aligned"),
    ]
    assert len(client.calls) == 2


@pytest.mark.parametrize("failure", ["payment", "invalid_response"])
def test_failed_repair_is_bounded_and_retried_on_a_later_run(store, failure):
    chapter = annotated_chapter()
    save_book(store, [chapter])
    original = store.load_chapter(0).model_dump()

    def fail(*_):
        if failure == "payment":
            raise ProviderRequestError(
                provider="fake",
                operation="annotation.align",
                error_type="http_error",
                status_code=402,
                reason=None,
                resumable=True,
            )
        return '{"id":"note","abstain":true}'

    client = FakeClient(handler=fail)
    service = Orchestrator(repair_config(), client)._annotations
    with store.lock():
        service.repair_fallbacks(store)
    assert len(client.calls) == 1
    assert store.load_chapter(0).model_dump() == original

    client.handler = repair_response
    with store.lock():
        service.repair_fallbacks(store)
    assert len(client.calls) == 2
    placements = store.load_chapter(0).segments[0].meta["epub_annotations"]["placements"]
    assert all(item["status"] == "aligned" for item in placements)


def test_interrupted_repair_resumes_without_repeating_persisted_successes(store):
    save_book(store, [annotated_chapter(0), annotated_chapter(1)])
    calls = 0

    def interrupt_second(messages, *args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return repair_response(messages, *args)

    client = FakeClient(handler=interrupt_second)
    service = Orchestrator(repair_config(), client)._annotations
    with pytest.raises(KeyboardInterrupt), store.lock():
        service.repair_fallbacks(store)
    assert (
        store.load_chapter(0).segments[0].meta["epub_annotations"]["placements"][1]["status"]
        == "aligned"
    )
    assert (
        store.load_chapter(1).segments[0].meta["epub_annotations"]["placements"][1]["status"]
        == "fallback"
    )

    store.close()
    reopened = type(store)(store.run_dir)
    resumed_client = FakeClient(handler=repair_response)
    try:
        with reopened.lock():
            Orchestrator(repair_config(), resumed_client)._annotations.repair_fallbacks(reopened)
        assert len(resumed_client.calls) == 1
        assert (
            reopened.load_chapter(1).segments[0].meta["epub_annotations"]["placements"][1]["status"]
            == "aligned"
        )
    finally:
        reopened.close()


@pytest.mark.parametrize("skip_reason", ["disabled", "stale_target", "incomplete", "no_fallback"])
def test_repair_skips_disabled_stale_incomplete_and_successful_placements(store, skip_reason):
    chapter = annotated_chapter()
    config = repair_config()
    if skip_reason == "disabled":
        config.pipeline.annotation_alignment = False
    elif skip_reason == "stale_target":
        previous_target = chapter.segments[1].target
        assert previous_target is not None
        chapter.segments[1].target = previous_target + "后来。"
    elif skip_reason == "incomplete":
        chapter.segments[1].target = None
    else:
        chapter.segments[0].meta["epub_annotations"]["placements"][1]["status"] = "aligned"
    save_book(store, [chapter])
    original = store.load_chapter(0).model_dump()
    client = FakeClient(handler=lambda *_: pytest.fail("Skipped placements must not make calls"))
    with store.lock():
        Orchestrator(config, client)._annotations.repair_fallbacks(store)
    assert not client.calls
    assert store.load_chapter(0).model_dump() == original


def test_translation_defers_repair_until_all_chapters_finish(store, monkeypatch):
    save_book(store, [annotated_chapter(0), annotated_chapter(1)])
    store.set_chapter_status(1, "pending")
    client = FakeClient(handler=repair_response)
    translation = Orchestrator(repair_config(), client)._translation
    monkeypatch.setattr(translation._titles, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(translation, "translate_chapter", lambda *args, **kwargs: kwargs["done"])

    with store.lock():
        translation.run(store, book_synopsis="", only_chapter=0)
    assert not client.calls

    store.set_chapter_status(1, "done")
    with store.lock():
        translation.run(store, book_synopsis="")
    assert len(client.calls) == 2
