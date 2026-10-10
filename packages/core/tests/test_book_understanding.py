"""Offline regressions for complete, resumable book-understanding results."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
from wenyi_core.agents.analyzer import Analyzer
from wenyi_core.agents.synopsis import Synopsizer
from wenyi_core.config import Config
from wenyi_core.glossary.store import GlossaryTerm
from wenyi_core.ingest.models import Chapter, Document, Segment
from wenyi_core.llm.limits import RequestCancelled, RequestStopped
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.pipeline.preparation import PreparationService
from wenyi_core.storage.file import FileStorage


def _selected_config():
    return Config.from_dict({"llm": {"preset": "fake"}, "pipeline": {"terminology_context": True}})


def _service(tmp_path, chapters=None, *, terminology=True):
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
        config=_selected_config() if terminology else Config.from_dict({"llm": {"preset": "fake"}}),
        synopsizer=Mock(),
        analyzer=Mock(),
        flush_usage=Mock(),
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
        ["Opening digest.", "Ending digest."], "Restrained.", glossary=[]
    )


def test_empty_chapter_does_not_require_a_digest(tmp_path):
    service, store, runtime = _service(
        tmp_path,
        [Chapter(index=0), Chapter(index=1, segments=[Segment(index=0, source="Body.")])],
    )
    assert service.ensure_understanding(store) == "Whole-book synopsis."
    runtime.synopsizer.digest_chapter.assert_called_once_with("Body.")
    runtime.synopsizer.book_synopsis.assert_called_once_with(
        ["Chapter digest."], "Restrained.", glossary=[]
    )


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
        ["Chapter digest.", "Revised ending."], "Restrained.", glossary=[]
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
        ["Chapter digest.", "Chapter digest."], "Restrained.", glossary=[]
    )


def test_chapter_digest_covers_all_blocks_and_merges_in_order():
    agent = Synopsizer(FakeClient(), _selected_config())
    agent._ask_text = Mock(side_effect=["First part.", "Middle part.", "Final part.", "Complete."])
    source = "a" * 8000 + "b" * 8000 + "LAST-SOURCE-IDENTITY"
    assert agent.digest_chapter(source) == "Complete."
    prompts = [call.args[1] for call in agent._ask_text.call_args_list]
    for block, prompt in zip([source[:8000], source[8000:16000], source[16000:]], prompts):
        assert block in prompt
    assert prompts[3].index("First part.") < prompts[3].index("Middle part.")
    assert prompts[3].index("Middle part.") < prompts[3].index("Final part.")
    assert "original source spelling" in agent._ask_text.call_args.args[0]


@pytest.mark.parametrize("responses", [["First.", ""], ["First.", "Last.", ""]])
def test_chapter_map_or_merge_failure_never_publishes_partial_digest(responses):
    agent = Synopsizer(FakeClient(), _selected_config())
    agent._ask_text = Mock(side_effect=responses)
    assert agent.digest_chapter("a" * 8000 + "ENDING") == ""
    assert agent._ask_text.call_count == len(responses)


def test_chapter_recursive_reduction_preserves_every_group():
    agent = Synopsizer(FakeClient(), _selected_config())
    agent._ask_text = Mock(side_effect=["A" * 7000, "B" * 7000, "a", "b", "All."])
    assert agent.digest_chapter("x" * 16000) == "All."
    assert agent._ask_text.call_count == 5
    assert "\na\nb" in agent._ask_text.call_args.args[1]


@pytest.mark.parametrize("output_length", [7000, 13000])
def test_noncontracting_or_oversized_outputs_stop_without_looping(output_length):
    agent = Synopsizer(FakeClient(), _selected_config())
    agent._ask_text = Mock(return_value="x" * output_length)
    assert agent.digest_chapter("a" * 16000) == ""
    assert agent._ask_text.call_count <= 4


def test_live_terms_replace_static_characters_even_when_empty():
    agent = Analyzer(FakeClient(), _selected_config())
    analysis = {
        "narration": "Source-supported first person.",
        "characters": [{"source": "Alice", "target": "Stale", "note": "Obsolete"}],
    }
    assert "Stale" in agent.style_brief(analysis)
    assert "Stale" not in agent.style_brief(analysis, terms=[])
    live = GlossaryTerm(source="Alice", target="Current", type="person", note="Live note")
    brief = agent.style_brief(analysis, terms=[live])
    assert "Current" in brief and "Live note" not in brief
    assert "Stale" not in brief and "Obsolete" not in brief
    assert analysis["narration"] in brief


def test_synopsis_receives_authoritative_terms_without_replacing_digest_substrings():
    agent = Synopsizer(FakeClient(), _selected_config())
    agent._ask_text = Mock(return_value="Synopsis.")
    term = GlossaryTerm(source="Ann", target="安", type="person", note="Confirmed")
    assert agent.book_synopsis(["Ann meets Anna."], "Style.", glossary=[term]) == "Synopsis."
    system, prompt = agent._ask_text.call_args.args
    assert "Ann meets Anna." in prompt
    assert '["Ann", "安", "person"]' in prompt
    assert "Confirmed" not in prompt
    assert "only the authoritative glossary" in system


def test_synopsis_cache_tracks_the_model_configuration(tmp_path):
    service, store, runtime = _service(tmp_path)
    service.ensure_understanding(store)
    runtime.synopsizer.book_synopsis.reset_mock()
    runtime.config.llm = Config.from_dict({"llm": {"preset": "deepseek"}}).llm
    service.ensure_understanding(store)
    runtime.synopsizer.book_synopsis.assert_called_once()


def test_glossary_change_invalidates_synopsis_cache(tmp_path):
    service, store, runtime = _service(tmp_path)
    service.ensure_understanding(store)
    term = GlossaryTerm(source="Citadel", target="城堡", type="place", note="Location")
    store.upsert_term(term, chapter=0)
    runtime.synopsizer.reset_mock()
    service.ensure_understanding(store)
    runtime.synopsizer.digest_chapter.assert_not_called()
    runtime.synopsizer.book_synopsis.assert_called_once_with(
        ["Chapter digest.", "Chapter digest."], "Restrained.", glossary=store.all_terms()
    )
    runtime.analyzer.style_brief.assert_called_with(store.load_analysis(), terms=store.all_terms())


@pytest.mark.parametrize("change", ["version", "source"])
def test_full_coverage_cache_requires_version_and_source_hash(tmp_path, change):
    service, store, runtime = _service(tmp_path)
    service.ensure_understanding(store)
    chapter = store.load_chapter(0)
    if change == "version":
        chapter.meta["source_digest_version"] = 1
    else:
        chapter.segments[0].source = "Changed opening."
    store.save_chapter(chapter)
    runtime.synopsizer.reset_mock()
    service.ensure_understanding(store)
    if change == "version":
        runtime.synopsizer.digest_chapter.assert_not_called()
        assert store.load_chapter(0).meta["source_digest_version"] == 2
    else:
        runtime.synopsizer.digest_chapter.assert_called_once_with(chapter.segments[0].source)


def test_disabled_long_chapter_uses_one_legacy_source_sample():
    agent = Synopsizer(FakeClient(), Config())
    agent._ask_text = Mock(return_value="Digest.")
    assert agent.digest_chapter("a" * 8000 + "UNSAMPLED-ENDING") == "Digest."
    agent._ask_text.assert_called_once()
    assert "a" * 8000 in agent._ask_text.call_args.args[1]
    assert "UNSAMPLED-ENDING" not in agent._ask_text.call_args.args[1]


def test_disabled_legacy_v1_cache_survives_routing_and_glossary_changes(tmp_path):
    service, store, runtime = _service(tmp_path, terminology=False)
    policy = runtime.config.language_policy("analysis")
    digests = ["Opening digest.", "Ending digest."]
    for index, digest in enumerate(digests):
        chapter = store.load_chapter(index)
        chapter.segments[0].target = "Saved target."
        chapter.meta.update(
            source_digest=digest,
            source_digest_policy=policy.task_fingerprint("chapter_digest"),
        )
        store.save_chapter(chapter)
    inputs = {
        "language_policy": policy.task_fingerprint("book_synopsis"),
        "chapters": list(enumerate(digests)),
        "style": "Restrained.",
        "source_lang": runtime.config.source_lang,
        "target_lang": runtime.config.target_lang,
    }
    store.save_analysis(
        {
            "book_synopsis": "Legacy synopsis.",
            "book_synopsis_meta": {
                "version": 1,
                "inputs_sha256": hashlib.sha256(
                    json.dumps(inputs, ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest(),
            },
        }
    )
    runtime.config.llm = Config.from_dict({"llm": {"preset": "deepseek"}}).llm
    store.upsert_term(GlossaryTerm(source="Alice", target="Current", type="person"))
    assert service.ensure_understanding(store) == "Legacy synopsis."
    runtime.synopsizer.digest_chapter.assert_not_called()
    runtime.synopsizer.book_synopsis.assert_not_called()
    runtime.analyzer.style_brief.assert_called_once_with(store.load_analysis())
    assert all(store.load_chapter(i).segments[0].target == "Saved target." for i in range(2))


def test_disabled_analysis_ignores_supplied_digests_and_live_style_names():
    agent = Analyzer(FakeClient(), Config())
    agent._ask_json = Mock(return_value={})
    agent.analyze("Original samples.", chapter_digests=["UNUSED-DIGEST"])
    assert "UNUSED-DIGEST" not in agent._ask_json.call_args.args[1]
    assert "Original samples." in agent._ask_json.call_args.args[1]
    assert "Legacy" in agent.style_brief(
        {"characters": [{"source": "Alice", "target": "Legacy"}]}, terms=[]
    )


def test_disabled_synopsis_has_no_glossary_appendix_or_style_truncation():
    agent = Synopsizer(FakeClient(), Config())
    agent._ask_text = Mock(return_value="Synopsis.")
    style = "x" * 5000 + "STYLE-ENDING"
    agent.book_synopsis(
        ["Alice arrives."], style, glossary=[GlossaryTerm(source="Alice", target="Current")]
    )
    prompt = agent._ask_text.call_args.args[1]
    assert style in prompt
    assert "authoritative mappings" not in prompt
    assert "Current" not in prompt


def test_selected_analysis_uses_digest_context_and_resumes_paid_result(tmp_path):
    service, store, runtime = _service(tmp_path)
    document = Document(
        source_lang="en",
        target_lang="zh",
        fmt="text",
        chapters=[store.load_chapter(0), store.load_chapter(1)],
    )
    runtime.analyzer.analyze.return_value = {"style_guide": "Source-supported."}
    result = service._analyze_document(store, document, ["Full digest."], "source-hash")
    assert service._analyze_document(store, document, ["Full digest."], "source-hash") == result
    runtime.analyzer.analyze.assert_called_once_with(
        service.sample_text(document), chapter_digests=["Full digest."]
    )
    agent = Analyzer(FakeClient(), _selected_config())
    agent._ask_json = Mock(return_value={})
    agent.analyze("Source prose.", chapter_digests=["Full digest."])
    assert "Full digest." in agent._ask_json.call_args.args[1]
