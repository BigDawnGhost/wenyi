"""Characterize every durable conversation boundary and replay without disk I/O."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import pytest
from wenyi_core.agents.review_actions import ReviewActionLoop
from wenyi_core.config import Config
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.review.evidence import BookEvidenceIndex


class StopAfterWrite(BaseException):
    """Simulate process interruption after a successful atomic write."""


class MemoryTrace:
    """Retain snapshots by value, just as serialization does at each durable write."""

    def __init__(self, stop_after: int | None = None):
        self.data: dict[str, dict[str, Any]] = {}
        self.snapshots: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.stop_after = stop_after

    def load(self, agent_id: str) -> dict[str, Any] | None:
        return deepcopy(self.data.get(agent_id))

    def save(self, agent_id: str, snapshot: dict[str, Any]) -> None:
        self.data[agent_id] = deepcopy(snapshot)
        self.snapshots.append(deepcopy(snapshot))
        if len(self.snapshots) == self.stop_after:
            raise StopAfterWrite()

    def log_event(self, event: str, **data: Any) -> None:
        self.events.append({"event": event, **deepcopy(data)})


class MemoryEvidence:
    """Return a small citable payload and count executions separately from LLM requests."""

    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(deepcopy(request))
        return {"request_id": request["request_id"], "ok": True, "ref": "book:style_guide"}

    def evidence_refs(self, value: Any) -> set[str]:
        return BookEvidenceIndex.evidence_refs(value)


REQUEST = {
    "action": "request_evidence",
    "requests": [{"request_id": "style-1", "tool": "style_guide", "arguments": {}}],
    "complete": False,
}
FINAL = {"action": "final", "decision": "Keep 安", "complete": True}


def _client() -> FakeClient:
    return FakeClient(
        handler=lambda messages, tier, json_mode: json.dumps(
            REQUEST if len(messages) == 2 else FINAL, ensure_ascii=False
        )
    )


def _run(client: FakeClient, trace: MemoryTrace, evidence: MemoryEvidence, *, max_rounds: int = 1):
    config = Config.from_dict(
        {"llm": {"preset": "fake"}, "pipeline": {"review_agent_max_evidence_rounds": max_rounds}}
    )
    return ReviewActionLoop(client, config, evidence, trace).run(
        agent_id="replay-test",
        system="System instruction",
        user="Review 安",
        stage="review.verify",
        allowed_refs={"ch0:text0:seg0"},
        validate_final=lambda data, refs: {"decision": data["decision"], "refs": sorted(refs)},
    )


def test_conversation_saves_requests_raw_parsed_evidence_and_final_in_order():
    client, trace, evidence = _client(), MemoryTrace(), MemoryEvidence()
    result, reason = _run(client, trace, evidence)
    assert not reason
    assert result == {"decision": "Keep 安", "refs": ["book:style_guide", "ch0:text0:seg0"]}
    boundaries = []
    for snapshot in trace.snapshots:
        turn = snapshot["turns"][-1] if snapshot["turns"] else {}
        boundaries.append(
            (
                snapshot["status"],
                turn.get("turn"),
                turn.get("status"),
                tuple(key for key in ("raw_response", "parsed", "evidence_results") if key in turn),
            )
        )
    assert boundaries == [
        ("running", None, None, ()),
        ("running", 1, "requesting", ()),
        ("running", 1, "responded", ("raw_response",)),
        ("running", 1, "responded", ("raw_response", "parsed")),
        ("running", 1, "responded", ("raw_response", "parsed", "evidence_results")),
        ("running", 2, "requesting", ()),
        ("running", 2, "responded", ("raw_response",)),
        ("running", 2, "responded", ("raw_response", "parsed")),
        ("finished", 2, "responded", ("raw_response", "parsed")),
    ]
    first_messages = [
        {"role": "system", "content": "System instruction"},
        {"role": "user", "content": "Review 安"},
    ]
    results = [{"request_id": "style-1", "ok": True, "ref": "book:style_guide"}]
    second_messages = [
        *first_messages,
        {"role": "assistant", "content": json.dumps(REQUEST, ensure_ascii=False)},
        {
            "role": "user",
            "content": "[Evidence tool results (JSON)]\n"
            + json.dumps(results, ensure_ascii=False, indent=2)
            + "\nEvidence rounds are exhausted. The next response must use action=final; do not request more evidence.",
        },
    ]
    assert [call["messages"] for call in client.calls] == [first_messages, second_messages]
    assert [call["operation"] for call in client.calls] == ["review.verify", "review.verify"]
    assert evidence.calls == REQUEST["requests"]
    assert [event["event"] for event in trace.events] == [
        "review_evidence_supplied",
        "review_agent_finished",
    ]


@pytest.mark.parametrize("boundary", range(1, 10))
def test_resume_from_every_durable_boundary_matches_uninterrupted_output(boundary):
    baseline_client, baseline_trace = _client(), MemoryTrace()
    expected = _run(baseline_client, baseline_trace, MemoryEvidence())
    client, trace, evidence = _client(), MemoryTrace(stop_after=boundary), MemoryEvidence()
    with pytest.raises(StopAfterWrite):
        _run(client, trace, evidence)
    trace.stop_after = None
    resumed_client = _client()
    assert _run(resumed_client, trace, evidence) == expected
    assert trace.data == baseline_trace.data
    assert [call["messages"] for call in client.calls + resumed_client.calls] == [
        call["messages"] for call in baseline_client.calls
    ]
    assert evidence.calls == REQUEST["requests"]


def test_protocol_fallback_is_saved_and_reused_without_repeating_requests():
    trace, evidence = MemoryTrace(), MemoryEvidence()
    client = FakeClient(handler=lambda *args: '{"action":"final","complete":false}')
    assert _run(client, trace, evidence) == (None, "final_not_complete")
    saved = deepcopy(trace.data)
    assert _run(client, trace, evidence) == (None, "final_not_complete")
    assert trace.data == saved
    assert len(client.calls) == 1
    assert trace.snapshots[-1]["status"] == "fallback"


@pytest.mark.parametrize(
    ("evidence_request", "reason"),
    [
        (
            {"request_id": "style-1", "tool": "book_context", "arguments": {"chapter": 1}},
            "duplicate_evidence_request_id",
        ),
        (
            {"request_id": "style-2", "tool": "style_guide", "arguments": {}},
            "duplicate_evidence_request",
        ),
    ],
)
def test_replayed_requests_still_reserve_ids_and_signatures(evidence_request, reason):
    trace, evidence = MemoryTrace(stop_after=5), MemoryEvidence()
    with pytest.raises(StopAfterWrite):
        _run(_client(), trace, evidence, max_rounds=2)
    trace.stop_after = None
    duplicate = {"action": "request_evidence", "requests": [evidence_request], "complete": False}
    client = FakeClient(handler=lambda *args: json.dumps(duplicate))
    assert _run(client, trace, evidence, max_rounds=2) == (None, reason)
    assert evidence.calls == REQUEST["requests"]
    assert len(client.calls) == 1
