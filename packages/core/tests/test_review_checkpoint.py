"""Check the trace adapter against existing round-scoped files and events."""

import json
from pathlib import Path

from wenyi_core.pipeline.review_checkpoint import ReviewCheckpoint, ReviewTraceStore
from wenyi_core.review.contracts import ReviewTrace
from wenyi_core.review.run_store import ReviewRunStore
from wenyi_core.review.session import ReviewRoundResult, ReviewSessionState


def test_trace_port_uses_existing_round_paths_and_event_scope(tmp_path):
    store = ReviewRunStore(str(tmp_path))
    snapshot = {"agent_id": "arbiter/term-安", "status": "running", "turns": []}
    relative = "agents/arbiter-term.json"
    with store.round_scope(2):
        store.write_json(relative, snapshot)
        trace: ReviewTrace = ReviewTraceStore(store)
        assert trace.load(snapshot["agent_id"]) == snapshot
        snapshot["status"] = "finished"
        trace.save(snapshot["agent_id"], snapshot)
        trace.log_event("review_agent_finished", agent_id=snapshot["agent_id"])
        assert store.load_json(relative) == snapshot
    with store.round_scope(3):
        assert ReviewTraceStore(store).load(snapshot["agent_id"]) is None
    events = [
        json.loads(line) for line in Path(store.run_dir, "events.jsonl").read_text().splitlines()
    ]
    assert len(events) == 1
    assert events[0]["review_round"] == 2
    assert events[0]["event"] == "review_agent_finished"
    assert Path(store.run_dir, "rounds/002/agents/arbiter-term.json").is_file()


def test_checkpoint_phase_fields_and_history_identity(tmp_path):
    store = ReviewRunStore(str(tmp_path))
    checkpoint = ReviewCheckpoint(store)
    patch = {"patch_id": "p1", "chapter": 0, "index": 0, "status": "provisional"}
    latest = ReviewRoundResult([], [], [], [], [], 0)
    state = ReviewSessionState(
        target_overrides={(0, 0): "译文"},
        seen_overlays={"before", "after"},
        patch_records=[patch],
        active_patches={(0, 0): patch},
        latest=latest,
    )
    checkpoint.save(state, 2, phase="scan_done", latest=latest)
    scan = store.load_checkpoint()
    assert scan is not None
    assert scan["next_round"] == 2
    assert scan["latest_issues"] == []
    restored = checkpoint.restore("baseline", 3)
    assert restored.latest == latest
    assert restored.state.active_patches[(0, 0)] is restored.state.patch_records[0]
    assert checkpoint.restore("baseline", 1).latest is None
    checkpoint.save(state, 2)
    completed = store.load_checkpoint()
    assert completed is not None
    assert completed["next_round"] == 3
    assert set(completed) == {
        "phase",
        "next_round",
        "target_overrides",
        "seen_overlays",
        "patch_records",
        "active_patches",
        "fix_failures",
        "blocked_issues",
        "round_summaries",
        "clean_streak",
        "fix_rounds",
    }
