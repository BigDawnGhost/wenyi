"""Inference-aware resume and recoverable usage publication contracts."""

from __future__ import annotations

import json
from dataclasses import replace
from unittest.mock import patch

import pytest
from wenyi_core.config import Config
from wenyi_core.llm.configuration import LLMConfig
from wenyi_core.llm.limits import RequestStopped
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.llm.usage import UsageSample
from wenyi_core.models.glossary import GlossaryTerm
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.pipeline.review.review_checkpoint import ReviewTraceStore
from wenyi_core.pipeline.review.review_workflow import ReviewService
from wenyi_core.pipeline.run.runtime import PipelineRuntime
from wenyi_core.review.run_store import ReviewRunStore
from wenyi_core.storage.file import FileStorage

from tests.fake_llm import METERED_TOTAL_TOKENS, MeteredFakeClient, routing_handler
from tests.pipeline_fixtures import require_file_storage
from tests.sample_data import write_sample_txt


def _config(tmp_path):
    return Config.from_dict(
        {
            "llm": {"preset": "fake"},
            "language": {"source": "ja", "target": "zh"},
            "pipeline": {
                "review": False,
                "polish": False,
                "book_understanding": False,
                "review_agent_loop": False,
                "review_fix_loop": False,
                "review_autofix": False,
            },
            "paths": {"state_dir": str(tmp_path / "state")},
        }
    )


@pytest.mark.parametrize("fresh_process", [False, True])
def test_ledger_journal_recovers_between_book_and_review_writes(
    tmp_path, monkeypatch, fresh_process
):
    config = _config(tmp_path)
    client = FakeClient()
    runtime = PipelineRuntime(config, client)
    store = FileStorage(str(tmp_path / "run"))
    debug = ReviewRunStore(store.run_dir)
    client.usage.record(
        "strong",
        UsageSample(
            prompt_tokens=7,
            completion_tokens=3,
            total_tokens=10,
            cache_hit_tokens=1,
            cache_write_tokens=3,
            uncached_tokens=2,
            cache_write_1h_tokens=3,
        ),
        "review.verify",
        provider="provider-id",
        model="model-id",
    )
    # FileStorage publishes usage through the wrapped RunStore writer.
    original_write = store._run._write_json
    failed = [False]

    def write(path, data):
        if (
            path == str(tmp_path / "run" / "reviews" / debug.review_id / "usage.json")
            and not failed[0]
        ):
            failed[0] = True
            raise OSError("simulated interrupted review ledger write")
        original_write(path, data)

    monkeypatch.setattr(store._run, "_write_json", write)
    with pytest.raises(OSError):
        runtime.usage.flush(store, scope="review", review=debug)
    book_usage = store.load_usage()
    assert book_usage is not None
    assert book_usage["totals"]["total_tokens"] == 10
    assert debug.load_usage() is None
    assert (tmp_path / "run" / "usage-pending.json").exists()
    if fresh_process:
        runtime = PipelineRuntime(config, FakeClient())
    # Repeating the flush in the same process cannot replay the increment.
    runtime.usage.flush(store, scope="review", review=debug)
    review_usage = debug.load_usage()
    book_usage = store.load_usage()
    assert review_usage is not None
    assert book_usage is not None
    assert review_usage["totals"]["total_tokens"] == 10
    assert book_usage["by_model"]["model-id"]["total_tokens"] == 10
    runtime.usage.flush(store, scope="review", review=debug)
    review_usage = debug.load_usage()
    assert review_usage is not None
    assert review_usage["totals"]["calls"] == 1
    # A fresh invocation retains cumulative totals without attributing old calls to new models.
    PipelineRuntime(config, FakeClient()).usage.flush(store, scope="resume", review=debug)
    book_usage = store.load_usage()
    assert book_usage is not None
    assert book_usage["totals"]["calls"] == 1
    for usage in (book_usage, review_usage):
        totals = usage["totals"]
        assert totals["cache_hit_tokens"] == 1
        assert totals["cache_write_tokens"] == 3
        assert totals["uncached_tokens"] == 2
        assert totals["cache_unknown_tokens"] == 1
        assert totals["cache_write_1h_tokens"] == 3
        assert totals["cache_write_1h_reported_calls"] == 1
        assert totals["cache_write_5m_reported_calls"] == 0


def test_fixer_protocol_revisions_invalidate_old_autofix_plans(tmp_path, monkeypatch):
    from wenyi_core.llm import operations, routing
    from wenyi_core.llm.operations import OPERATIONS
    from wenyi_core.llm.routing import inference_snapshot
    from wenyi_core.pipeline.autofix.autofix_plan import prepare_identity

    config = _config(tmp_path)
    assert OPERATIONS["review.fix"].protocol_version == 2
    assert OPERATIONS["autofix.fix"].protocol_version == 2
    debug = ReviewRunStore(str(tmp_path / "run"))
    current = inference_snapshot(config.llm, ("autofix.verify", "autofix.fix"))
    registry = dict(OPERATIONS)
    registry["autofix.fix"] = replace(registry["autofix.fix"], protocol_version=1)
    with monkeypatch.context() as context:
        context.setattr(routing, "OPERATIONS", registry)
        context.setattr(operations, "OPERATIONS", registry)
        old = inference_snapshot(config.llm, ("autofix.verify", "autofix.fix"))
    assert old != current
    debug.write_json("autofix/plan.json", {"inference": old})
    with pytest.raises(ValueError, match="Autofix planning models changed"):
        prepare_identity(debug, config.llm)


def test_review_fingerprint_only_tracks_reachable_inference(tmp_path):
    config = _config(tmp_path)
    first = ReviewService(PipelineRuntime(config, FakeClient()))._review_config_snapshot()
    changed = config.model_copy(deep=True)
    raw = changed.llm.model_dump()
    raw["models"]["fake/alternate"] = {}
    raw["routes"]["translation.body"] = {"model": "fake/alternate"}
    raw["routes"]["review.verify"] = {"model": "fake/alternate"}  # Disabled by review_agent_loop.
    changed.llm = LLMConfig.model_validate(raw)
    changed.pipeline.review_concurrency = 1
    same = ReviewService(PipelineRuntime(changed, FakeClient()))._review_config_snapshot()
    assert same == first
    raw["routes"]["review.scan"] = {"model": "fake/alternate"}
    changed.llm = LLMConfig.model_validate(raw)
    assert ReviewService(PipelineRuntime(changed, FakeClient()))._review_config_snapshot() != first


@pytest.mark.parametrize(
    "changes",
    [
        {"target": "new translation"},
        {"type": "person"},
        {"aliases": ["another spelling"]},
        {"reading": "new pronunciation"},
        {"gender": "female"},
        {"note": "new evidence"},
        {"first_chapter": 2},
        {"status": "conflict"},
    ],
)
def test_review_glossary_fingerprint_tracks_prompt_and_evidence_fields(changes):
    term = GlossaryTerm(source="Absent from this chapter", target="original")
    fingerprint = ReviewService._review_glossary_fingerprint
    assert fingerprint([term]) != fingerprint([replace(term, **changes)])


def test_review_glossary_fingerprint_tracks_entry_order():
    terms = [GlossaryTerm(source=source, target=source) for source in ("Zebra", "Apple")]
    fingerprint = ReviewService._review_glossary_fingerprint
    assert fingerprint(terms) != fingerprint(list(reversed(terms)))


def _review_with_absent_term(tmp_path, interrupted):
    """Persist a completed or genuinely interrupted Review with two singleton chunks."""
    source = tmp_path / "novel.txt"
    source.write_text(
        "堀北は静かな部屋で長い手紙を読みました。\n\n堀北は翌日の朝に友人と話をしました。\n",
        encoding="utf-8",
    )
    config = _config(tmp_path)
    config.segment.max_tokens_per_batch = 1
    config.pipeline.review_concurrency = 1
    store = require_file_storage(
        Orchestrator(config, FakeClient(handler=routing_handler)).run(str(source))
    )
    term = GlossaryTerm(source="Unused", target="无关术语")
    store.upsert_term(term)
    scans = 0

    def handler(messages, tier, json_mode):
        nonlocal scans
        if "translation reviewer" in messages[0]["content"]:
            scans += 1
            assert "Unused → 无关术语" in messages[-1]["content"]
            if interrupted and scans == 2:
                raise KeyboardInterrupt("after the first full-glossary chunk")
        return routing_handler(messages, tier, json_mode)

    client = MeteredFakeClient(handler=handler)
    orchestrator = Orchestrator(config, client)
    if interrupted:
        with pytest.raises(KeyboardInterrupt, match="first full-glossary chunk"):
            orchestrator.run_review(str(source))
        result = store.load_latest_review_result()
        assert result is not None
        assert result["status"] == "interrupted"
    else:
        result = orchestrator.run_review(str(source))["review_result"]
    assert scans == 2
    return source, config, store, term, result["review_id"]


@pytest.mark.parametrize("interrupted", [False, True])
def test_full_glossary_review_reuses_completed_results_and_pending_chunks(tmp_path, interrupted):
    source, config, store, _term, review_id = _review_with_absent_term(tmp_path, interrupted)
    initial_usage = store.load_usage()
    assert initial_usage is not None
    metadata = store.read_artifact(f"reviews/{review_id}/rounds/metadata.json")
    assert isinstance(metadata, dict)
    assert metadata["config"]["review_glossary_policy"] == "full"
    assert metadata["config"]["review_glossary_reading_source"] == "ja"

    client = MeteredFakeClient(handler=routing_handler)
    resumed = Orchestrator(config, client).run_review(str(source))
    assert resumed["review_result"]["review_id"] == review_id
    assert len(client.calls) == (1 if interrupted else 0)
    for call in client.calls:
        assert call["operation"] == "review.scan"
        assert "Unused → 无关术语" in call["messages"][-1]["content"]
        assert "翌日" in call["messages"][-1]["content"]
    usage = store.load_usage()
    assert usage is not None
    assert usage["totals"]["calls"] == initial_usage["totals"]["calls"] + len(client.calls)

    # A third invocation must neither re-request cached work nor merge its usage twice.
    cached_client = MeteredFakeClient(handler=routing_handler)
    cached = Orchestrator(config, cached_client).run_review(str(source))
    assert cached["review_result"]["review_id"] == review_id
    assert cached_client.calls == []
    assert store.load_usage() == usage


@pytest.mark.parametrize("interrupted", [False, True])
@pytest.mark.parametrize(
    "change",
    ["policy", "reading_missing", "reading_old", "target", "aliases", "note", "insert", "delete"],
)
def test_full_glossary_changes_invalidate_completed_and_interrupted_reviews(
    tmp_path, interrupted, change
):
    source, config, store, term, review_id = _review_with_absent_term(tmp_path, interrupted)
    original_chapter = store.load_chapter(0)
    if change in {"policy", "reading_missing", "reading_old"}:
        key = f"reviews/{review_id}/rounds/metadata.json"
        metadata = store.read_artifact(key)
        assert isinstance(metadata, dict)
        # A pre-policy run must not reuse chunks or initial traces under the new policy.
        if change == "policy":
            metadata["config"].pop("review_glossary_policy")
        elif change == "reading_missing":
            metadata["config"].pop("review_glossary_reading_source", None)
        else:
            metadata["config"]["review_glossary_reading_source"] = "all"
        store.write_artifact(key, metadata)
    elif change == "target":
        store.resolve_term(term.source, "新译名")
    elif change == "aliases":
        store.upsert_term(replace(term, aliases=["Another absent spelling"]))
    elif change == "note":
        store.upsert_term(replace(term, note="Updated evidence"))
    elif change == "insert":
        store.upsert_term(GlossaryTerm(source="Another unused term", target="新增术语"))
    else:
        assert store.delete_term(term.source)

    client = FakeClient(handler=routing_handler)
    new = Orchestrator(config, client).run_review(str(source))
    assert new["review_result"]["review_id"] != review_id
    assert len(client.calls) == 2
    assert {call["operation"] for call in client.calls} == {"review.scan"}
    assert store.read_artifact(f"reviews/{review_id}/rounds/metadata.json") is not None
    assert store.load_chapter(0) == original_chapter
    for call in client.calls:
        prompt = call["messages"][-1]["content"]
        if change == "delete":
            assert "Unused →" not in prompt
        else:
            assert f"Unused → {'新译名' if change == 'target' else '无关术语'}" in prompt
        if change == "aliases":
            assert "Another absent spelling" in prompt
        if change == "insert":
            assert "Another unused term → 新增术语" in prompt


@pytest.mark.parametrize("interrupted", [False, True])
@pytest.mark.parametrize("change", ["style", "synopsis", "digest", "character_target", "unchanged"])
def test_review_guidance_controls_completed_and_interrupted_reuse(tmp_path, interrupted, change):
    source, config, store, _term, review_id = _review_with_absent_term(tmp_path, interrupted)
    analysis = store.load_analysis() or {}
    if change == "style":
        analysis["tone"] = "New restrained tone"
    elif change == "synopsis":
        analysis["book_synopsis"] = "New book context"
    elif change == "digest":
        chapter = store.load_chapter(0)
        chapter.meta["source_digest"] = "New chapter context"
        store.save_chapter(chapter)
    elif change == "character_target":
        # Target mappings are not effective style guidance.
        analysis["characters"] = [{"source": "A", "gender": "male", "target": "old"}]
        store.save_analysis(analysis)
        # Establish the baseline with the same effective character guidance.
        client = FakeClient(handler=routing_handler)
        baseline = Orchestrator(config, client).run_review(str(source))
        review_id = baseline["review_result"]["review_id"]
        analysis["characters"][0]["target"] = "new"
    store.save_analysis(analysis)

    client = FakeClient(handler=routing_handler)
    result = Orchestrator(config, client).run_review(str(source))["review_result"]
    invalidated = change in {"style", "synopsis", "digest"}
    assert (result["review_id"] != review_id) == invalidated
    expected_calls = 2 if invalidated else int(interrupted and change != "character_target")
    assert len(client.calls) == expected_calls


def test_evidence_trace_is_reused_only_under_the_same_model(tmp_path):
    from wenyi_core.agents.review_actions import ReviewActionLoop

    from tests.review_fixtures import review_evidence

    config = _config(tmp_path)
    debug = ReviewRunStore(str(tmp_path / "run"))
    response = json.dumps({"action": "final", "complete": True, "issues": []})
    first = FakeClient(handler=lambda *args: response)
    arguments = {
        "agent_id": "identity-test",
        "system": "system",
        "user": "user",
        "stage": "review.verify",
        "allowed_refs": set(),
        "validate_final": lambda value, refs: value,
    }
    ReviewActionLoop(first, config, review_evidence(), ReviewTraceStore(debug)).run(**arguments)
    assert len(first.calls) == 1
    ReviewActionLoop(first, config, review_evidence(), ReviewTraceStore(debug)).run(**arguments)
    assert len(first.calls) == 1
    route = config.llm.routes["review.verify"]
    config.llm.models["fake/different-verifier"] = config.llm.models[route.model]
    config.llm.routes["review.verify"] = route.model_copy(
        update={"model": "fake/different-verifier"}
    )
    second = FakeClient(handler=lambda *args: response, config=config.llm)
    ReviewActionLoop(second, config, review_evidence(), ReviewTraceStore(debug)).run(**arguments)
    assert len(second.calls) == 1
    assert second.calls[0]["model"] == "different-verifier"


def test_completed_translation_is_kept_and_changed_review_model_gets_new_run(tmp_path):
    source = tmp_path / "novel.txt"
    write_sample_txt(str(source))
    config = _config(tmp_path)
    first = Orchestrator(config, FakeClient(handler=routing_handler))
    store = require_file_storage(first.run(str(source)))
    initial = first.run_review(str(source))
    translated = {path.name: path.read_bytes() for path in (tmp_path / "state").rglob("ch*.json")}
    assert translated
    second_client = FakeClient(handler=routing_handler)
    second = Orchestrator(config, second_client)
    cached = second.run_review(str(source))
    assert cached["review_dir"] == initial["review_dir"]
    assert second_client.calls == []
    raw = config.llm.model_dump()
    raw["models"]["fake/new-editor"] = {}
    raw["routes"]["review.scan"] = {"model": "fake/new-editor"}
    config.llm = LLMConfig.model_validate(raw)
    third_client = FakeClient(handler=routing_handler)
    third = Orchestrator(config, third_client)
    third.run(str(source))
    assert third_client.calls == []
    new = third.run_review(str(source))
    assert new["review_dir"] != initial["review_dir"]
    assert {row["operation"] for row in third_client.calls} == {"review.scan"}
    assert {
        path.name: path.read_bytes() for path in (tmp_path / "state").rglob("ch*.json")
    } == translated
    assert json.loads(open(store.manifest_path, encoding="utf-8").read())["target_lang"] == "zh"


def _polish_config(tmp_path):
    config = _config(tmp_path)
    config.pipeline.polish = True
    config.segment.max_tokens_per_batch = 1
    return config


def _polish_source(tmp_path):
    source = tmp_path / "polish_resume.txt"
    source.write_text(
        "堀北は静かな部屋で長い手紙を読みました。\n\n堀北は翌日の朝に友人と話をしました。\n",
        encoding="utf-8",
    )
    return source


def _polish_handler(stopping_after):
    """Return a handler that raises a budget stop once N polish turns have succeeded."""
    polish_turns = {"count": 0}

    def handler(messages, tier, json_mode):
        system = messages[0]["content"]
        user = messages[-1]["content"]
        is_polish = (
            "prose editor" in system
            or "Polish the translations from your previous JSON response" in user
        )
        if is_polish:
            polish_turns["count"] += 1
            if polish_turns["count"] > stopping_after:
                raise RequestStopped("Model request budget exhausted")
        return routing_handler(messages, tier, json_mode)

    return handler


def test_budget_exhaustion_in_polish_propagates_and_resume_bills_once(tmp_path):
    """A budget stop inside the polisher aborts the run; resume replays only that batch."""
    config = _polish_config(tmp_path)
    source = _polish_source(tmp_path)
    store = FileStorage(str(tmp_path / "run"))
    first_client = MeteredFakeClient(handler=_polish_handler(1))
    interrupted = Orchestrator(config, first_client, storage=store)

    with (
        patch.object(interrupted._translation, "extract_batch_glossary", return_value={}),
        patch.object(interrupted._runtime.extractor, "extract_and_store", return_value={}),
    ):
        with pytest.raises(RequestStopped, match="budget exhausted"):
            interrupted.run(str(source))

    # Every dispatched call stays on the ledger, including the aborted polish request;
    # the second batch is incomplete, so its second segment is still untranslated.
    partial = store.load_usage()
    assert partial is not None
    assert partial["totals"]["calls"] == 5
    assert partial["totals"]["total_tokens"] == 5 * METERED_TOTAL_TOKENS
    assert {stage: row["calls"] for stage, row in partial["by_stage"].items()} == {
        "analysis.style": 1,
        "translation.body": 2,
        "polish.body": 2,
    }
    partial_chapter = store.load_chapter(0)
    assert partial_chapter.segments[0].target == "润0"
    assert partial_chapter.segments[1].target is None

    resumed_client = MeteredFakeClient(handler=routing_handler)
    resumed = Orchestrator(config, resumed_client, storage=store)
    with (
        patch.object(resumed._translation, "extract_batch_glossary", return_value={}),
        patch.object(resumed._runtime.extractor, "extract_and_store", return_value={}),
    ):
        result = require_file_storage(resumed.run(str(source)))

    # Resume replays only the interrupted batch plus the final title pass; the completed
    # analysis and first batch are never re-run, so each real call is billed exactly once.
    resume_ops = sorted(row["operation"] for row in resumed_client.calls)
    assert resume_ops == ["polish.body", "translation.body", "translation.title"]
    chapter = result.load_chapter(0)
    assert [segment.target for segment in chapter.segments] == ["润0", "润0"]
    usage = result.load_usage()
    assert usage is not None
    assert usage["totals"]["calls"] == 8
    assert usage["totals"]["total_tokens"] == 8 * METERED_TOTAL_TOKENS
    assert {stage: row["calls"] for stage, row in usage["by_stage"].items()} == {
        "analysis.style": 1,
        "translation.body": 3,
        "polish.body": 3,
        "translation.title": 1,
    }


def test_polish_configuration_error_propagates_without_fallback(tmp_path):
    """A polish routing ValueError aborts the run instead of keeping raw translations."""
    config = _polish_config(tmp_path)
    source = _polish_source(tmp_path)
    store = FileStorage(str(tmp_path / "run"))

    def failing_handler(messages, tier, json_mode):
        if tier == "polish.body":
            raise ValueError("Unknown model profile: fake/missing")
        return routing_handler(messages, tier, json_mode)

    orchestrator = Orchestrator(config, MeteredFakeClient(handler=failing_handler), storage=store)

    with pytest.raises(ValueError, match="Unknown model profile"):
        orchestrator.run(str(source))
