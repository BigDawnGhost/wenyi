"""Offline protocol checks for source-grounded terminology tasks."""

import json

import pytest
from wenyi_core.agents.terminology import TerminologyAgent, TerminologyReferenceError
from wenyi_core.config import Config
from wenyi_core.glossary.evidence_models import KnowledgeResult, SourcePassage
from wenyi_core.glossary.store import GlossaryTerm


class FakeClient:
    def __init__(self, data):
        self.data = data
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return json.dumps(self.data)


def agent(data):
    return TerminologyAgent(FakeClient(data), Config(source_lang="en", target_lang="zh"))


PASSAGES = [
    SourcePassage(reference="c0:s0:0-24", chapter=0, segment=0, source="Ann is Bob's mother.")
]
TERM = GlossaryTerm("Ann", "安")


def test_enrich_complete_note_and_reference():
    worker = agent({"note": "安是鲍勃的母亲。", "evidence_refs": [PASSAGES[0].reference]})
    result = worker.enrich(TERM, PASSAGES)
    assert result.note == "安是鲍勃的母亲。"
    assert result.evidence_refs == [PASSAGES[0].reference]
    assert worker.client.calls[0][1]["operation"] == "terminology.evidence"


@pytest.mark.parametrize("refs", [[], ["invented"]])
def test_enrich_rejects_missing_or_foreign_evidence(refs):
    with pytest.raises(ValueError, match="evidence"):
        agent({"note": "secret raw response", "evidence_refs": refs}).enrich(TERM, PASSAGES)


@pytest.mark.parametrize(
    "fields,reason,received,unknown",
    [
        ({"note": "PRIVATE_NOTE"}, "missing", 0, 0),
        ({"note": "PRIVATE_NOTE", "evidence_refs": []}, "missing", 0, 0),
        (
            {"note": "PRIVATE_NOTE", "evidence_refs": ["PRIVATE_WRONG_REFERENCE"]},
            "outside_input",
            1,
            1,
        ),
        (
            {
                "note": "PRIVATE_NOTE",
                "evidence_refs": [PASSAGES[0].reference, "PRIVATE_WRONG_REFERENCE"],
            },
            "outside_input",
            2,
            1,
        ),
    ],
)
def test_reference_errors_distinguish_missing_from_foreign_without_exposing_response(
    fields, reason, received, unknown
):
    worker = agent(fields)
    with pytest.raises(TerminologyReferenceError) as caught:
        worker.enrich(TERM, PASSAGES)
    error = caught.value
    assert error.diagnostic() == {
        "operation": "terminology.evidence",
        "reason": reason,
        "field": "evidence_refs",
        "allowed_count": 1,
        "received_count": received,
        "unknown_count": unknown,
    }
    assert "terminology.evidence" in str(error)
    assert "PRIVATE" not in str(error) + json.dumps(error.diagnostic())
    assert len(worker.client.calls) == 1


def test_merge_reference_error_names_the_merge_operation():
    with pytest.raises(TerminologyReferenceError) as caught:
        agent({"note": "PRIVATE_NOTE", "evidence_refs": ["PRIVATE_WRONG_REFERENCE"]}).merge(
            TERM, [KnowledgeResult(note="Supported", evidence_refs=["r"])]
        )
    assert caught.value.diagnostic()["operation"] == "terminology.merge"
    assert caught.value.diagnostic()["field"] == "evidence_refs"
    assert "PRIVATE" not in str(caught.value)


@pytest.mark.parametrize("refs", [None, [], ["PRIVATE_BAD_REF"]])
def test_discovery_reference_errors_reject_only_the_candidate(refs):
    candidate = {"source": "Ann", "target": "安"}
    if refs is not None:
        candidate["evidence_refs"] = refs
    result = agent({"terms": [candidate]}).discover(PASSAGES, [])
    assert result.terms == []
    assert len(result.rejections) == 1
    assert result.rejections[0].model_dump() == {
        "collection": "terms",
        "index": 0,
        "reason": "outside_input" if refs else "missing",
    }
    assert "PRIVATE" not in json.dumps(result.rejections[0].model_dump())


def test_discovery_requires_actual_source_and_keeps_authoritative_target():
    data = {
        "terms": [{"source": "Ann", "target": "另名", "evidence_refs": [PASSAGES[0].reference]}]
    }
    assert agent(data).discover(PASSAGES, [TERM]).terms[0].target == "安"
    data["terms"][0]["source"] = "Anna"
    result = agent(data).discover(PASSAGES, [])
    assert result.terms == []
    assert result.rejections[0].reason == "source_absent"


def test_invalid_fields_do_not_expose_response():
    with pytest.raises(ValueError) as error:
        agent({"note": {"secret": "raw response"}}).enrich(TERM, PASSAGES)
    assert "raw response" not in str(error.value)


def test_invalid_merge_fields_identify_operation_field_and_type_without_private_values():
    worker = agent({"note": {"PRIVATE_KEY": "PRIVATE_VALUE"}, "evidence_refs": ["group:0"]})
    with pytest.raises(ValueError) as caught:
        worker.merge(TERM, [KnowledgeResult(note="Supported note", evidence_refs=["c0:s0"])])
    assert "operation=terminology.merge" in str(caught.value)
    assert "note:string_type" in str(caught.value)
    assert "PRIVATE" not in str(caught.value)


def test_invalid_json_does_not_expose_response():
    class InvalidClient(FakeClient):
        def complete(self, messages, **kwargs):
            return "secret raw response"

    worker = TerminologyAgent(InvalidClient(None), Config(source_lang="en", target_lang="zh"))
    with pytest.raises(ValueError, match="JSON") as error:
        worker.enrich(TERM, PASSAGES)
    assert "secret" not in str(error.value)


def test_merge_budget_is_explicit_and_never_calls_model_with_oversized_input():
    worker = agent({})
    with pytest.raises(ValueError, match="budget"):
        worker.merge(
            TERM, [KnowledgeResult(note="x" * 4000, evidence_refs=["r"]) for _ in range(7)]
        )
    assert worker.client.calls == []


def test_merge_cannot_erase_notes_or_introduce_references():
    inputs = [KnowledgeResult(note="supported", evidence_refs=["r"])]
    with pytest.raises(ValueError, match="note"):
        agent({}).merge(TERM, inputs)
    with pytest.raises(ValueError, match="evidence"):
        agent({"note": "merged", "evidence_refs": ["fake"]}).merge(TERM, inputs)


def test_discovery_aliases_are_resolved_only_when_trusted_and_unambiguous():
    passages = [SourcePassage(reference="r", chapter=0, segment=0, source="Ace arrived.")]
    data = {"terms": [{"source": "Ace", "target": "艾斯", "evidence_refs": ["r"]}]}
    ann = GlossaryTerm("Ann", "安", aliases=["Ace"])
    result = agent(data).discover(passages, [ann])
    assert result.terms[0].source == "Ann"
    assert result.terms[0].target == "安"
    result = agent(data).discover(passages, [ann, GlossaryTerm("Bob", "鲍勃", aliases=["Ace"])])
    assert result.terms[0].source == "Ace"


def test_empty_evidence_is_not_a_completed_result():
    worker = agent({})
    assert worker.enrich(TERM, []) == KnowledgeResult()
    assert worker.client.calls == []
    assert worker.enrich(TERM, PASSAGES) == KnowledgeResult()


def test_large_ledger_stays_out_of_repeated_pairwise_merge_requests():
    refs = [f"chapter:{i}:segment:0:0-100" for i in range(3000)]
    inputs = [
        KnowledgeResult(note="Short note", evidence_refs=refs),
        KnowledgeResult(note="Other note", evidence_refs=refs),
    ]
    assert len(json.dumps([item.model_dump() for item in inputs])) > 24000
    worker = agent({"note": "Merged note", "evidence_refs": ["group:0", "group:1"]})
    for _ in range(5):
        result = worker.merge(TERM, inputs)
        assert result.evidence_refs == refs
        inputs = [result, inputs[1]]
    requests = [json.dumps(messages) for messages, _ in worker.client.calls]
    assert all(len(request) < 24000 for request in requests)
    assert all(refs[0] not in request for request in requests)
    assert max(map(len, requests)) - min(map(len, requests)) < 100


def test_enrich_alias_must_occur_in_cited_not_merely_supplied_passages():
    passages = [
        *PASSAGES,
        SourcePassage(reference="other", chapter=0, segment=1, source="Ace arrived."),
    ]
    for alias in ("Invented", "Ace", "Anna"):
        with pytest.raises(ValueError, match="alias"):
            agent({"aliases": [alias], "evidence_refs": [PASSAGES[0].reference]}).enrich(
                TERM, passages
            )
    result = agent({"aliases": ["Ace"], "evidence_refs": ["other"]}).enrich(TERM, passages)
    assert result.aliases == ["Ace"]


@pytest.mark.parametrize("task", ["enrich", "merge"])
def test_oversized_output_note_is_rejected(task):
    refs = [PASSAGES[0].reference] if task == "enrich" else ["group:0"]
    worker = agent({"note": "x" * 4001, "evidence_refs": refs})
    with pytest.raises(ValueError, match="note:string_too_long"):
        if task == "enrich":
            worker.enrich(TERM, PASSAGES)
        else:
            worker.merge(TERM, [KnowledgeResult(note="short", evidence_refs=["r"])])


def test_merge_keeps_exact_alias_and_reference_union_locally():
    inputs = [
        KnowledgeResult(note="Supported", aliases=["Ace"], evidence_refs=["r"]),
        KnowledgeResult(aliases=["Ace", "Al"], evidence_refs=["s"]),
    ]
    worker = agent({"note": "Merged", "evidence_refs": ["group:0"]})
    result = worker.merge(TERM, inputs)
    assert result.aliases == ["Ace", "Al"]
    assert result.evidence_refs == ["r", "s"]
    worker.client.data["aliases"] = ["Invented"]
    with pytest.raises(ValueError, match="alias"):
        worker.merge(TERM, inputs)


@pytest.mark.parametrize("field", ["reading", "gender"])
def test_merge_rejects_unbounded_secondary_fields(field):
    worker = agent({})
    with pytest.raises(ValueError, match=f"{field}:string_too_long"):
        worker.merge(TERM, [KnowledgeResult(**{field: "x" * 257}, evidence_refs=["r"])])
    assert worker.client.calls == []
