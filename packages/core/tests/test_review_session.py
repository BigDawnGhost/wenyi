"""Whole-book checkpoint and accounting interruption contracts."""

import json
import re
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import pytest
from wenyi_core.ingest.models import Segment
from wenyi_core.llm.retrying import ProviderRequestError
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.review.run_store import ReviewRunStore

from tests.fake_llm import METERED_TOTAL_TOKENS, MeteredFakeClient
from tests.review_fixtures import autofix_config as _config
from tests.review_fixtures import autofix_store as _store
from tests.review_fixtures import fix_json as _fix_json
from tests.review_fixtures import review_json as _review_json


def _project(directory):
    config = _config(str(directory / "state"))
    config.pipeline.review_autofix = False
    config.pipeline.review_agent_loop = False
    config.pipeline.review_conflict_arbitration = False
    config.pipeline.review_fix_loop = True
    config.pipeline.review_clean_confirmations = 2

    def handler(messages, tier, json_mode):
        system, user = messages[0]["content"], messages[-1]["content"]
        if "translation reviewer" in system:
            issues = (
                []
                if "影子修订译文。" in user
                else [
                    {
                        "index": 0,
                        "type": "missing",
                        "detail": "Missing meaning",
                        "suggestion": "Restore it",
                    }
                ]
            )
            return _review_json(user, issues)
        if "cautious revision editor" in system:
            return _fix_json(user, "影子修订译文。")
        raise AssertionError(system)

    client = MeteredFakeClient(handler=handler)
    return Orchestrator(config, client), _store(str(directory)), client


@pytest.mark.parametrize("boundary", ["scan_done", "scan_usage", "round_done"])
@pytest.mark.parametrize("after", [False, True])
def test_review_recovers_checkpoint_and_usage_boundaries(tmp_path, boundary, after):
    baseline, baseline_store, baseline_client = _project(tmp_path / "baseline")
    expected = baseline._review.run_session(baseline_store, [])
    orch, store, first_client = _project(tmp_path / "interrupted")
    formal = Path(store.chapter_path(0)).read_bytes()
    manifest = Path(store.manifest_path).read_bytes()
    stopped = False
    save = ReviewRunStore.save_checkpoint
    flush = orch._runtime.flush_usage

    def checkpoint(debug, value):
        nonlocal stopped
        if not stopped and value["phase"] == boundary:
            stopped = True
            if after:
                save(debug, value)
            raise KeyboardInterrupt()
        return save(debug, value)

    def usage(*args, **kwargs):
        nonlocal stopped
        if not stopped and boundary == "scan_usage" and kwargs.get("scope") == "review":
            stopped = True
            if after:
                flush(*args, **kwargs)
            raise KeyboardInterrupt()
        return flush(*args, **kwargs)

    with (
        patch.object(ReviewRunStore, "save_checkpoint", checkpoint),
        patch.object(orch._runtime, "flush_usage", side_effect=usage),
        pytest.raises(KeyboardInterrupt),
    ):
        orch._review.run_session(store, [])
    assert stopped
    resumed, _, second_client = _project(tmp_path / "interrupted")
    actual = resumed._review.run_session(store, [])
    assert actual.issues == expected.issues
    assert actual.changes == expected.changes
    assert actual.result["termination"] == expected.result["termination"]
    assert actual.result["summary"] == expected.result["summary"]
    assert actual.usage["totals"] == expected.usage["totals"]
    assert [(c["operation"], c["messages"]) for c in first_client.calls + second_client.calls] == [
        (c["operation"], c["messages"]) for c in baseline_client.calls
    ]
    assert Path(store.chapter_path(0)).read_bytes() == formal
    assert Path(store.manifest_path).read_bytes() == manifest


@pytest.mark.parametrize("failed_operation", ["review.verify", "review.fix"])
def test_provider_interrupt_resumes_review_and_publishes_once(tmp_path, failed_operation):
    config = _config(str(tmp_path / "state"))
    config.segment.max_tokens_per_batch = 1
    config.pipeline.review_clean_confirmations = 1
    store = _store(str(tmp_path))
    chapter = store.load_chapter(0)
    chapter.segments.append(Segment(index=1, source="Second source.", target="Second target."))
    store.save_chapter(chapter)
    formal = Path(store.chapter_path(0)).read_bytes()
    manifest = Path(store.manifest_path).read_bytes()
    attempts = Counter()

    def handler(messages, tier, json_mode):
        system, user = messages[0]["content"], messages[-1]["content"]
        if "translation reviewer" in system:
            issues = (
                []
                if "影子修订译文。" in user
                else [
                    {
                        "index": 0,
                        "type": "missing",
                        "detail": "Missing meaning",
                        "suggestion": "Restore it",
                    }
                ]
            )
            return _review_json(user, issues)
        operation = "review.verify" if "evidence-based review agent" in system else "review.fix"
        attempts[operation] += 1
        if operation == failed_operation and attempts[operation] == 2:
            raise ProviderRequestError(
                provider="default",
                operation=operation,
                error_type="http_503",
                status_code=503,
                reason="http_503",
                resumable=True,
            )
        if operation == "review.verify":
            return json.dumps(
                {
                    "action": "final",
                    "decisions": [
                        {"candidate_id": candidate, "verdict": "confirmed"}
                        for candidate in re.findall(r'"candidate_id":\s*"([^"]+)"', user)
                    ],
                    "complete": True,
                }
            )
        return _fix_json(user, "影子修订译文。")

    first_client = MeteredFakeClient(handler=handler)
    with pytest.raises(ProviderRequestError):
        Orchestrator(config, first_client)._review.run_session(store, [])
    interrupted = store.load_latest_review_result()
    assert interrupted is not None
    assert interrupted["status"] == "interrupted"
    assert interrupted["termination"] == "interrupted"
    assert Path(store.chapter_path(0)).read_bytes() == formal

    client = MeteredFakeClient(handler=handler)
    resumed = Orchestrator(config, client)
    outcome = resumed._review.run_session(store, [])
    assert outcome.result["review_id"] == interrupted["review_id"]
    assert outcome.result["termination"] == "clean_confirmed"
    assert outcome.issues == []
    calls = first_client.calls + client.calls
    assert Counter(call["operation"] for call in calls) == {
        "review.scan": 4,
        "review.verify": 2 + (failed_operation == "review.verify"),
        "review.fix": 2 + (failed_operation == "review.fix"),
    }
    assert len(outcome.changes) == 2
    assert outcome.usage["totals"]["calls"] == len(calls)
    assert outcome.usage["totals"]["total_tokens"] == len(calls) * METERED_TOTAL_TOKENS
    assert Path(store.chapter_path(0)).read_bytes() == formal

    fixed = resumed._review_autofix.run(store, outcome, [])
    assert fixed.result["autofix"]["applied_segment_count"] == 2
    assert fixed.result["autofix"]["status"] == "completed"
    published = Path(store.chapter_path(0)).read_bytes()
    usage = store.load_usage()
    assert usage is not None
    assert usage["totals"]["calls"] == len(calls)
    assert resumed._review_autofix.run(store, fixed, []).result == fixed.result
    assert Path(store.chapter_path(0)).read_bytes() == published
    assert Path(store.manifest_path).read_bytes() == manifest
    assert store.load_usage() == usage
    assert len(first_client.calls) + len(client.calls) == len(calls)


@pytest.mark.parametrize("change", ["style", "synopsis", "digest", "unchanged"])
def test_finished_fixer_trace_is_only_resumed_with_identical_guidance(tmp_path, change):
    orch, store, first_client = _project(tmp_path)
    save = ReviewRunStore.save_checkpoint

    def checkpoint(debug, value):
        if value["phase"] == "round_done":
            # The fixer trace is durable, but the overlay checkpoint is not.
            raise KeyboardInterrupt()
        return save(debug, value)

    with (
        patch.object(ReviewRunStore, "save_checkpoint", checkpoint),
        pytest.raises(KeyboardInterrupt),
    ):
        orch._review.run_session(store, [])
    previous = store.load_latest_review_result()
    assert previous is not None
    assert sum(c["operation"] == "review.fix" for c in first_client.calls) == 1
    trace = store.read_artifact(f"reviews/{previous['review_id']}/rounds/001/fixers/ch0-text0.json")
    assert trace is not None
    assert trace["status"] == "finished"

    resumed, _, client = _project(tmp_path)
    analysis = store.load_analysis() or {}
    if change == "style":
        analysis["tone"] = "Restrained"
    elif change == "synopsis":
        analysis["book_synopsis"] = "New synopsis"
    elif change == "digest":
        chapter = store.load_chapter(0)
        chapter.meta["source_digest"] = "New digest"
        store.save_chapter(chapter)
    store.save_analysis(analysis)

    outcome = resumed._review.run_session(store, [])
    assert (outcome.result["review_id"] != previous["review_id"]) == (change != "unchanged")
    assert sum(c["operation"] == "review.fix" for c in client.calls) == int(change != "unchanged")
