"""Whole-book checkpoint and accounting interruption contracts."""

from pathlib import Path
from unittest.mock import patch

import pytest
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.review.run_store import ReviewRunStore
from wenyi_core.storage.file import FileStorage
from wenyi_core.storage.protocol import Storage

from tests.test_orchestrator import MeteredFakeClient, _fix_json, _review_json
from tests.test_review_autofix import _config, _store


def require_file_storage(store: Storage) -> FileStorage:
    """CLI/offline tests use the file backend; narrow Storage to FileStorage for path asserts."""
    if not isinstance(store, FileStorage):
        raise TypeError(f"expected FileStorage, got {type(store).__name__}")
    return store


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
