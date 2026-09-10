"""Inference-aware resume and recoverable usage publication contracts."""

import json

import pytest

from tests.fake_llm import routing_handler
from tests.sample_data import write_sample_txt
from trans_novel.config import Config
from trans_novel.llm.configuration import LLMConfig
from trans_novel.llm.providers.fake import FakeClient
from trans_novel.llm.usage import UsageSample
from trans_novel.pipeline.orchestrator import Orchestrator
from trans_novel.pipeline.review_workflow import ReviewService
from trans_novel.pipeline.runstore import RunStore
from trans_novel.pipeline.runtime import PipelineRuntime
from trans_novel.review.run_store import ReviewRunStore


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
    store = RunStore(str(tmp_path / "run"))
    debug = ReviewRunStore(store.run_dir)
    client.usage.record(
        "strong",
        UsageSample(prompt_tokens=7, completion_tokens=3, total_tokens=10),
        "review.verify",
        provider="provider-id",
        model="model-id",
    )
    original_write = store._write_json
    failed = [False]

    def write(path, data):
        if (
            path == str(tmp_path / "run" / "reviews" / debug.review_id / "usage.json")
            and not failed[0]
        ):
            failed[0] = True
            raise OSError("simulated interrupted review ledger write")
        original_write(path, data)

    monkeypatch.setattr(store, "_write_json", write)
    with pytest.raises(OSError):
        runtime.flush_usage(store, scope="review", review=debug)
    book_usage = store.load_usage()
    assert book_usage is not None
    assert book_usage["totals"]["total_tokens"] == 10
    assert debug.load_usage() is None
    assert (tmp_path / "run" / "usage-pending.json").exists()
    if fresh_process:
        runtime = PipelineRuntime(config, FakeClient())
    # Repeating the flush in the same process cannot replay the increment.
    runtime.flush_usage(store, scope="review", review=debug)
    review_usage = debug.load_usage()
    book_usage = store.load_usage()
    assert review_usage is not None
    assert book_usage is not None
    assert review_usage["totals"]["total_tokens"] == 10
    assert book_usage["by_model"]["model-id"]["total_tokens"] == 10
    runtime.flush_usage(store, scope="review", review=debug)
    review_usage = debug.load_usage()
    assert review_usage is not None
    assert review_usage["totals"]["calls"] == 1
    # A fresh invocation retains cumulative totals without attributing old calls to new models.
    PipelineRuntime(config, FakeClient()).flush_usage(store, scope="resume", review=debug)
    book_usage = store.load_usage()
    assert book_usage is not None
    assert book_usage["totals"]["calls"] == 1


def test_review_fingerprint_only_tracks_reachable_inference(tmp_path):
    config = _config(tmp_path)
    first = ReviewService(PipelineRuntime(config, FakeClient()))._review_config_snapshot()
    changed = config.model_copy(deep=True)
    raw = changed.llm.model_dump()
    raw["models"]["alternate"] = {"provider": "default", "model": "alternate"}
    raw["routes"]["translation.body"] = {"model": "alternate"}
    raw["routes"]["review.verify"] = {"model": "alternate"}  # Disabled by review_agent_loop.
    changed.llm = LLMConfig.model_validate(raw)
    changed.pipeline.review_concurrency = 1
    same = ReviewService(PipelineRuntime(changed, FakeClient()))._review_config_snapshot()
    assert same == first
    raw["routes"]["review.scan"] = {"model": "alternate"}
    changed.llm = LLMConfig.model_validate(raw)
    assert ReviewService(PipelineRuntime(changed, FakeClient()))._review_config_snapshot() != first


def test_evidence_trace_is_reused_only_under_the_same_model(tmp_path):
    from tests.test_review_agent import TestReviewAgentLoop
    from trans_novel.agents.review_loop import _ActionLoop

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
    _ActionLoop(first, config, TestReviewAgentLoop()._evidence(), debug).run(**arguments)
    assert len(first.calls) == 1
    _ActionLoop(first, config, TestReviewAgentLoop()._evidence(), debug).run(**arguments)
    assert len(first.calls) == 1
    config.llm.models["default_strong"] = config.llm.models["default_strong"].model_copy(
        update={"model": "different-verifier"}
    )
    second = FakeClient(handler=lambda *args: response, config=config.llm)
    _ActionLoop(second, config, TestReviewAgentLoop()._evidence(), debug).run(**arguments)
    assert len(second.calls) == 1
    assert second.calls[0]["model"] == "different-verifier"


def test_completed_translation_is_kept_and_changed_review_model_gets_new_run(tmp_path):
    source = tmp_path / "novel.txt"
    write_sample_txt(str(source))
    config = _config(tmp_path)
    first = Orchestrator(config, FakeClient(handler=routing_handler))
    store = first.run(str(source))
    initial = first.run_review(str(source))
    translated = {path.name: path.read_bytes() for path in (tmp_path / "state").rglob("ch*.json")}
    assert translated
    second_client = FakeClient(handler=routing_handler)
    second = Orchestrator(config, second_client)
    cached = second.run_review(str(source))
    assert cached["review_dir"] == initial["review_dir"]
    assert second_client.calls == []
    raw = config.llm.model_dump()
    raw["models"]["editor"] = {"provider": "default", "model": "new-editor"}
    raw["routes"]["review.scan"] = {"model": "editor"}
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
    assert json.loads(open(store.manifest_path).read())["target_lang"] == "zh"
