"""Offline regressions for complete, resumable book-understanding results."""

from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
from wenyi_core.agents.synopsis import Synopsizer
from wenyi_core.config import Config
from wenyi_core.ingest.models import Chapter, Document, Segment
from wenyi_core.llm.limits import RequestCancelled, RequestStopped
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.pipeline.preparation import PreparationService
from wenyi_core.storage.file import FileStorage


def _service(tmp_path, chapters=None):
    source = tmp_path / "source.txt"
    source.write_text("Temporary source text.", encoding="utf-8")
    store = FileStorage(str(tmp_path / "state"))
    store.init_from_document(
        Document(
            source_lang="en",
            target_lang="zh",
            fmt="text",
            source_path=str(source),
            chapters=chapters
            or [
                Chapter(index=0, segments=[Segment(index=0, source="Opening.")]),
                Chapter(index=1, segments=[Segment(index=0, source="Ending.")]),
            ],
        )
    )
    store.save_analysis({"style_guide": "Restrained."})
    runtime = SimpleNamespace(
        config=Config.from_dict({"llm": {"preset": "fake"}}),
        synopsizer=Mock(),
        analyzer=Mock(),
    )
    runtime.synopsizer.digest_chapter.return_value = "Chapter digest."
    runtime.synopsizer.book_synopsis.return_value = "Whole-book synopsis."
    runtime.analyzer.style_brief.return_value = "Restrained."
    return PreparationService(runtime), store, runtime


def test_book_synopsis_transport_failure_has_empty_fallback():
    def handler(messages, tier, json_mode):
        raise TimeoutError("provider unavailable")

    assert Synopsizer(FakeClient(handler=handler), Config()).book_synopsis(["Digest."], "") == ""


@pytest.mark.parametrize("stop", [RequestCancelled, RequestStopped])
@pytest.mark.parametrize("method", ["digest_chapter", "book_synopsis"])
def test_synopsis_does_not_swallow_cancellation_or_budget_stop(stop, method):
    client = Mock()
    client.complete.side_effect = stop("stop requested")
    args = ("Source chapter.",) if method == "digest_chapter" else (["Digest."], "")
    with pytest.raises(stop):
        getattr(Synopsizer(client, Config()), method)(*args)


def test_reduce_failure_does_not_omit_a_group_and_publish_the_rest():
    client = Mock()
    client.complete.side_effect = ["", "Later chapters.", "Incomplete book synopsis."]
    result = Synopsizer(client, Config()).book_synopsis(["a" * 7000, "b" * 7000], "")
    assert result == ""
    assert client.complete.call_count == 1


def test_failed_chapter_digest_requires_retry_and_preserves_successes(tmp_path):
    service, store, runtime = _service(tmp_path)
    runtime.synopsizer.digest_chapter.side_effect = lambda source: (
        "Opening digest." if source == "Opening." else ""
    )
    with pytest.raises(ValueError, match="Chapter digests.*1"):
        service.ensure_understanding(store)
    assert store.load_chapter(0).meta["source_digest"] == "Opening digest."
    assert not store.load_chapter(1).meta.get("source_digest")
    assert all(s.target is None for s in store.load_chapter(0).segments)
    runtime.synopsizer.book_synopsis.assert_not_called()

    runtime.synopsizer.digest_chapter.reset_mock(side_effect=True)
    runtime.synopsizer.digest_chapter.return_value = "Ending digest."
    assert service.ensure_understanding(store) == "Whole-book synopsis."
    runtime.synopsizer.digest_chapter.assert_called_once_with("Ending.")
    runtime.synopsizer.book_synopsis.assert_called_once_with(
        ["Opening digest.", "Ending digest."], "Restrained."
    )


def test_empty_chapter_does_not_require_a_digest(tmp_path):
    service, store, runtime = _service(
        tmp_path,
        [Chapter(index=0), Chapter(index=1, segments=[Segment(index=0, source="Body.")])],
    )
    assert service.ensure_understanding(store) == "Whole-book synopsis."
    runtime.synopsizer.digest_chapter.assert_called_once_with("Body.")
    runtime.synopsizer.book_synopsis.assert_called_once_with(["Chapter digest."], "Restrained.")


def test_book_failure_is_not_cached_and_resume_reuses_chapter_digests(tmp_path):
    service, store, runtime = _service(tmp_path)
    runtime.synopsizer.book_synopsis.return_value = ""
    assert service.ensure_understanding(store) == ""
    assert not store.load_analysis().get("book_synopsis")
    assert not store.load_analysis().get("book_synopsis_meta")
    runtime.synopsizer.digest_chapter.reset_mock()
    runtime.synopsizer.book_synopsis.return_value = "Complete synopsis."
    assert service.ensure_understanding(store) == "Complete synopsis."
    runtime.synopsizer.digest_chapter.assert_not_called()
    assert store.load_analysis()["style_guide"] == "Restrained."


def test_complete_cache_is_reused_without_sentence_punctuation(tmp_path):
    service, store, runtime = _service(tmp_path)
    runtime.synopsizer.digest_chapter.return_value = "A complete digest without punctuation"
    runtime.synopsizer.book_synopsis.return_value = "A complete synopsis without punctuation"
    assert service.ensure_understanding(store) == "A complete synopsis without punctuation"
    runtime.synopsizer.reset_mock()
    assert service.ensure_understanding(store) == "A complete synopsis without punctuation"
    runtime.synopsizer.digest_chapter.assert_not_called()
    runtime.synopsizer.book_synopsis.assert_not_called()


def test_changed_digest_invalidates_book_cache(tmp_path):
    service, store, runtime = _service(tmp_path)
    service.ensure_understanding(store)
    chapter = store.load_chapter(1)
    chapter.meta["source_digest"] = "Revised ending."
    store.save_chapter(chapter)
    runtime.synopsizer.reset_mock()
    runtime.synopsizer.book_synopsis.return_value = "Revised synopsis."
    assert service.ensure_understanding(store) == "Revised synopsis."
    runtime.synopsizer.book_synopsis.assert_called_once_with(
        ["Chapter digest.", "Revised ending."], "Restrained."
    )


def test_failed_regeneration_keeps_existing_analysis_but_does_not_inject_stale_synopsis(tmp_path):
    service, store, runtime = _service(tmp_path)
    service.ensure_understanding(store)
    runtime.analyzer.style_brief.return_value = "New style."
    runtime.synopsizer.book_synopsis.return_value = ""
    assert service.ensure_understanding(store) == ""
    assert store.load_analysis()["book_synopsis"] == "Whole-book synopsis."
    runtime.synopsizer.book_synopsis.return_value = "Revised synopsis."
    assert service.ensure_understanding(store) == "Revised synopsis."


def test_legacy_truncated_digest_and_unverified_synopsis_are_regenerated(tmp_path):
    service, store, runtime = _service(tmp_path)
    chapter = store.load_chapter(0)
    chapter.meta["source_digest"] = "Legacy unfinished sentence"
    store.save_chapter(chapter)
    chapter = store.load_chapter(1)
    chapter.meta["source_digest"] = "Legacy complete digest."
    store.save_chapter(chapter)
    store.save_analysis({"style_guide": "Restrained.", "book_synopsis": "Legacy synopsis."})
    assert service.ensure_understanding(store) == "Whole-book synopsis."
    assert runtime.synopsizer.digest_chapter.call_count == 2
    runtime.synopsizer.digest_chapter.assert_has_calls(
        [call("Opening."), call("Ending.")], any_order=True
    )
    runtime.synopsizer.book_synopsis.assert_called_once_with(
        ["Chapter digest.", "Chapter digest."], "Restrained."
    )
