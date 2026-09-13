"""Check the trace adapter against existing round-scoped files and events."""

import json
from pathlib import Path

from trans_novel.pipeline.review_checkpoint import ReviewTraceStore
from trans_novel.review.contracts import ReviewTrace
from trans_novel.review.run_store import ReviewRunStore


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
