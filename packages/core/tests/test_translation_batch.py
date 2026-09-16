"""Integration of token budgets and translation conversations across batch boundaries."""

import json

import pytest
from wenyi_core.agents.polisher import Polisher
from wenyi_core.agents.translator import Translator
from wenyi_core.config import Config
from wenyi_core.ingest.models import Segment
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.pipeline.review_chunks import ReviewChunkService
from wenyi_core.pipeline.translation_batch import (
    BatchPlan,
    TranslationBatchExecutor,
    resume_batches,
)


def _plan(sources, *, allow_empty=False):
    return BatchPlan.capture(
        3,
        5,
        [Segment(index=10 + i, source=s) for i, s in enumerate(sources)],
        [],
        "previous translation",
        "style",
        "synopsis",
        "digest",
        [[] for _ in sources],
        "next source",
        allow_empty_translations=allow_empty,
    )


def test_resume_and_review_pack_by_tokens_and_preserve_completed_blank():
    segments = [
        Segment(index=10, source="hello world", target=""),
        Segment(index=11, source="foo bar"),
        Segment(index=12, source="x"),
    ]
    assert [[s.index for s in b] for b in resume_batches(segments, 4)] == [[10], [11], [12]]
    assert [[s.index for s in b] for b in ReviewChunkService.pack_contiguous(segments, 4)] == [
        [10, 11],
        [12],
    ]


@pytest.mark.parametrize("invalid_continuation", [False, True])
def test_batch_polish_preserves_filtered_positions_and_fallback(invalid_continuation):
    def handler(messages, tier, json_mode):
        if len(messages) == 4:
            return json.dumps(
                {"polished": ["wrong count"] if invalid_continuation else ["ONE", "TWO"]}
            )
        if "literary translator" in messages[0]["content"]:
            return json.dumps({"translations": ["one", "two"]})
        return json.dumps({"polished": ["ONE", "123", "TWO"]})

    config = Config.from_dict({"llm": {"preset": "fake"}})
    client = FakeClient(handler=handler)
    executor = TranslationBatchExecutor(Translator(client, config), Polisher(client, config))
    plan = _plan(["alpha", "123", "beta"])
    result = executor.execute(plan, polish=True)

    assert result.targets == ("ONE", "123", "TWO")
    assert result.before_polish == ("one", "123", "two")
    assert plan.segment_indices == (10, 11, 12)
    assert [c["operation"] for c in client.calls] == ["translation.body", "polish.body"] + (
        ["polish.body"] if invalid_continuation else []
    )
    assert client.calls[1]["messages"][:2] == client.calls[0]["messages"]
    assert client.calls[1]["messages"][2]["role"] == "assistant"


def test_batch_passes_mineru_blank_allowance_to_translator():
    config = Config.from_dict({"llm": {"preset": "fake"}})
    client = FakeClient(handler=lambda *_: '{"translations": [""]}')
    executor = TranslationBatchExecutor(Translator(client, config), Polisher(client, config))
    result = executor.execute(_plan(["OCR artifact"], allow_empty=True), polish=False)
    assert result.targets == ("",)
    assert result.before_polish == (None,)
    assert len(client.calls) == 1


@pytest.mark.parametrize("backend", ["mineru", "babeldoc", "text"])
def test_chapter_service_limits_blank_allowance_and_resumes_without_retranslation(
    tmp_path, backend
):
    from wenyi_core.agents.translator import AlignmentError
    from wenyi_core.pipeline.orchestrator import Orchestrator
    from wenyi_core.pipeline.runstore import STATUS_PENDING

    from tests.fake_llm import routing_handler
    from tests.test_review_autofix import _config, _store

    store = _store(str(tmp_path))
    manifest = store.load_manifest()
    manifest["fmt"] = "text" if backend == "text" else "pdf"
    manifest["meta"] = {"babeldoc": True} if backend == "babeldoc" else {}
    manifest["chapters"][0]["status"] = STATUS_PENDING
    store.save_manifest(manifest)
    chapter = store.load_chapter(0)
    chapter.text_segments[0].target = None
    store.save_chapter(chapter)
    config = _config(str(tmp_path / "state"))
    config.pipeline.polish = False
    config.pipeline.annotation_alignment = False
    config.pipeline.align_retry_limit = 0

    def handler(messages, tier, json_mode):
        if "literary translator" in messages[0]["content"]:
            return '{"translations": [""]}'
        return routing_handler(messages, tier, json_mode)

    client = FakeClient(handler=handler)
    service = Orchestrator(config, client)._translation
    if backend != "mineru":
        with pytest.raises(AlignmentError):
            service.run(store, book_synopsis="")
        assert store.load_chapter(0).text_segments[0].target is None
        return

    service.run(store, book_synopsis="")
    assert store.load_chapter(0).text_segments[0].target == ""
    assert service.progress_counts(store, [0]) == (1, 1)
    store.set_chapter_status(0, STATUS_PENDING)
    resumed_client = FakeClient(handler=handler)
    Orchestrator(config, resumed_client)._translation.run(store, book_synopsis="")
    assert store.load_chapter(0).text_segments[0].target == ""
    assert store.pending_chapters() == []
    assert not [c for c in resumed_client.calls if c["operation"] == "translation.body"]
