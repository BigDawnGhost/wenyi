"""Strict knowledge contracts and privacy-safe schema diagnostics without paid calls."""

import json

import pytest
from wenyi_core.agents.terminology import (
    MERGE_INPUT_BUDGET,
    TerminologyAgent,
    TerminologyKnowledgeError,
)
from wenyi_core.glossary.evidence_models import KnowledgeResult
from wenyi_core.glossary.store import GlossaryTerm
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.pipeline.terminology import TerminologyService

from tests.test_terminology_agent import PASSAGES, TERM, agent
from tests.test_terminology_service import book_store


@pytest.mark.parametrize(
    "response,field,code",
    [
        ({"note": None}, "note", "string_type"),
        ({"note": {"PRIVATE_KEY": "PRIVATE_VALUE"}}, "note", "string_type"),
        ({"reading": ["PRIVATE_VALUE"]}, "reading", "string_type"),
        ({"gender": False}, "gender", "string_type"),
        ({"aliases": "PRIVATE_VALUE"}, "aliases", "list_type"),
        ({"aliases": [None]}, "aliases[0]", "string_type"),
        ({"evidence_refs": "PRIVATE_REF"}, "evidence_refs", "list_type"),
        ({"evidence_refs": [23]}, "evidence_refs[0]", "string_type"),
        ({"PRIVATE_KEY": "PRIVATE_VALUE"}, "<extra>", "extra_forbidden"),
        ([{"PRIVATE_KEY": "PRIVATE_VALUE"}], "$", "model_type"),
    ],
)
def test_schema_failures_identify_only_known_paths_and_codes(response, field, code):
    worker = agent(response)
    with pytest.raises(TerminologyKnowledgeError) as caught:
        worker.enrich(TERM, PASSAGES)
    diagnostic = caught.value.diagnostic()
    assert diagnostic["operation"] == "terminology.evidence"
    assert diagnostic["reason"] == "invalid_fields"
    assert {"field": field, "code": code} in diagnostic["issues"]
    assert diagnostic["issue_count"] >= 1
    assert f"{field}:{code}" in str(caught.value)
    assert "PRIVATE" not in str(caught.value) + json.dumps(diagnostic)
    assert len(worker.client.calls) == 1


def test_schema_diagnostics_bound_details_but_keep_total_error_count():
    with pytest.raises(TerminologyKnowledgeError) as caught:
        agent({"aliases": [{"PRIVATE_KEY": "PRIVATE_VALUE"}] * 30}).enrich(TERM, PASSAGES)
    diagnostic = caught.value.diagnostic()
    assert diagnostic["issue_count"] == 30
    assert len(diagnostic["issues"]) == 8
    assert "PRIVATE" not in json.dumps(diagnostic)


@pytest.mark.parametrize("task", ["evidence", "merge"])
@pytest.mark.parametrize("field,limit", [("note", 4000), ("reading", 256), ("gender", 256)])
def test_overlength_fields_use_safe_schema_diagnostics(task, field, limit):
    worker = agent(
        {
            field: "PRIVATE_" + "x" * limit,
            "evidence_refs": ["p1" if task == "evidence" else "group:0"],
        }
    )
    with pytest.raises(TerminologyKnowledgeError) as caught:
        if task == "evidence":
            worker.enrich(TERM, PASSAGES)
        else:
            worker.merge(TERM, [KnowledgeResult(note="Supported", evidence_refs=["source-ref"])])
    assert caught.value.diagnostic()["issues"] == [{"field": field, "code": "string_too_long"}]
    assert "PRIVATE" not in str(caught.value) + json.dumps(caught.value.diagnostic())


def test_provider_extra_field_is_diagnosed_before_caching_or_publication(tmp_path):
    store, chapters = book_store(tmp_path)

    class Handler:
        fail = True

        def __call__(self, messages, tier, json_mode):
            request = json.loads(messages[-1]["content"].split("\n", 1)[1])
            result = {
                "note": "Supported ordinary metadata",
                "evidence_refs": [request["passages"][0]["reference"]],
            }
            if self.fail:
                result["PRIVATE_KEY"] = "PRIVATE_VALUE"
            return json.dumps(result)

    handler = Handler()
    client = FakeClient(handler=handler)
    worker = TerminologyAgent(client, agent({}).config)
    service = TerminologyService(store, worker, chapters)
    term = GlossaryTerm("Beth", "贝丝")
    with pytest.raises(TerminologyKnowledgeError):
        service.admit(term)
    assert store.get_term("Beth") is None
    assert store.list_artifacts("terminology/requests/evidence/") == []
    keys = store.list_artifacts("terminology/diagnostics/evidence/")
    assert len(keys) == 1
    diagnostic = store.read_artifact(keys[0])
    assert diagnostic["issues"] == [{"field": "<extra>", "code": "extra_forbidden"}]
    events = store.list_events(event_type="terminology_knowledge_validation_failed")
    assert len(events) == 1
    assert "PRIVATE" not in json.dumps(diagnostic) + json.dumps(events)
    handler.fail = False
    assert service.admit(term) == "inserted"
    cached = store.read_artifact(store.list_artifacts("terminology/requests/evidence/")[0])
    assert cached["note"] == "Supported ordinary metadata"
    record = store.read_artifact(store.list_artifacts("terminology/entries/")[0])
    assert "PRIVATE" not in json.dumps(cached) + json.dumps(record)
    calls = len(client.calls)
    service.admit(term)
    assert len(client.calls) == calls


@pytest.mark.parametrize("task", ["evidence", "merge"])
def test_request_supplies_exact_schema_and_empty_value_rules(task):
    worker = agent(
        {"note": "Supported note", "evidence_refs": ["p1" if task == "evidence" else "group:0"]}
    )
    if task == "evidence":
        worker.enrich(TERM, PASSAGES)
    else:
        worker.merge(TERM, [KnowledgeResult(note="Supported note", evidence_refs=["c0:s0"])])
    request = json.loads(worker.client.calls[0][0][-1]["content"].split("\n", 1)[1])
    schema = request["response_schema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == set(KnowledgeResult.model_fields)
    for field in ("note", "reading", "gender"):
        assert schema["properties"][field]["type"] == "string"
        assert schema["properties"][field]["maxLength"] == (4000 if field == "note" else 256)
    for field in ("aliases", "evidence_refs"):
        assert schema["properties"][field]["type"] == "array"
    assert "never null" in request["response_rules"]
    assert "without a wrapper" in request["response_rules"]
    if task == "merge":
        assert schema["properties"]["aliases"]["const"] == []
    assert len(worker.client.calls) == 1


def test_merge_budget_counts_the_schema_payload_before_calling_model():
    worker = agent({})
    payload = {
        "term": {"source": "", "target": "安", "type": "term"},
        "results": [{"reference": "group:0", "note": "", "reading": "", "gender": ""}],
    }
    overhead = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    term = GlossaryTerm(source="X" * (MERGE_INPUT_BUDGET - overhead), target="安")
    payload["term"]["source"] = term.source
    assert len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"))) == MERGE_INPUT_BUDGET
    with pytest.raises(ValueError, match="merge input exceeds budget"):
        worker.merge(term, [KnowledgeResult()])
    assert worker.client.calls == []
