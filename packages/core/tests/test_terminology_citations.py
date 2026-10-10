"""Exact request-local citation labels with stable source identities at every boundary."""

import json
from dataclasses import asdict

import pytest
from wenyi_core.agents.terminology import TerminologyAgent, TerminologyReferenceError
from wenyi_core.config import Config
from wenyi_core.glossary.evidence_models import KnowledgeResult, SourcePassage
from wenyi_core.glossary.store import GlossaryTerm
from wenyi_core.i18n.policy.models import content_hash
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.pipeline.terminology import TerminologyService

from tests.test_terminology_service import book_store

PASSAGES = [
    SourcePassage(reference="c0:s29:0-789", chapter=0, segment=29, source="Alice arrived."),
    SourcePassage(
        reference="c2:s5:400-800",
        chapter=2,
        segment=5,
        start=400,
        source="Alice's older brother Carl also called Alice 'Ace'.",
    ),
]


def worker(data):
    config = Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {"preset": "fake"},
            "pipeline": {"terminology_context": True},
        }
    )
    return TerminologyAgent(FakeClient(handler=lambda *args: json.dumps(data)), config)


def payload(client):
    return json.loads(client.calls[0]["messages"][-1]["content"].split("\n", 1)[1])


def test_wire_citations_are_short_and_exact_while_stable_ids_remain_supplied():
    agent = worker({"note": "Supported description.", "evidence_refs": ["p1"]})
    before = [passage.model_dump() for passage in PASSAGES]
    result = agent.enrich(GlossaryTerm("Alice", "艾丽丝"), PASSAGES)
    request = payload(agent.client)
    assert [passage["reference"] for passage in request["passages"]] == ["p1", "p2"]
    assert [passage["source_reference"] for passage in request["passages"]] == [
        passage.reference for passage in PASSAGES
    ]
    assert "exact" in request["citation_rules"]
    assert result.evidence_refs == [PASSAGES[0].reference]
    assert [passage.model_dump() for passage in PASSAGES] == before


def test_mixed_explicit_labels_and_stable_refs_are_canonicalized_and_deduplicated():
    result = worker(
        {
            "note": "Supported description.",
            "evidence_refs": ["p2", PASSAGES[0].reference, "p1", PASSAGES[1].reference],
        }
    ).enrich(GlossaryTerm("Alice", "艾丽丝"), PASSAGES)
    assert result.evidence_refs == [PASSAGES[1].reference, PASSAGES[0].reference]


@pytest.mark.parametrize("reference,valid", [("p1", False), ("p2", True)])
def test_local_labels_do_not_relax_alias_source_evidence(reference, valid):
    agent = worker({"aliases": ["Ace"], "evidence_refs": [reference]})
    if valid:
        assert agent.enrich(GlossaryTerm("Alice", "艾丽丝"), PASSAGES).aliases == ["Ace"]
    else:
        with pytest.raises(ValueError, match="alias"):
            agent.enrich(GlossaryTerm("Alice", "艾丽丝"), PASSAGES)


def test_discovery_uses_the_same_local_citation_codec_and_original_chapter():
    agent = worker({"terms": [{"source": "Carl", "target": "卡尔", "evidence_refs": ["p2"]}]})
    result = agent.discover(PASSAGES, [])
    assert result.terms[0].source == "Carl"
    assert result.terms[0].first_chapter == 2
    assert payload(agent.client)["passages"][1]["reference"] == "p2"


@pytest.mark.parametrize("reference", ["p0", "p3", "P1", "p01", "p1 ", "c0:s29"])
def test_no_fuzzy_or_out_of_range_citation_is_accepted(reference):
    agent = worker({"note": "Supported description.", "evidence_refs": [reference]})
    with pytest.raises(TerminologyReferenceError) as caught:
        agent.enrich(GlossaryTerm("Alice", "艾丽丝"), PASSAGES)
    assert caught.value.reason == "outside_input"
    assert caught.value.unknown_count == 1


@pytest.mark.parametrize("references", [["p0"] * 11, ["UNKNOWN_PRIVATE_REFERENCE"] * 11])
def test_failed_reference_counts_are_not_reduced_by_deduplication(references):
    agent = worker({"note": "Supported description.", "evidence_refs": references})
    with pytest.raises(TerminologyReferenceError) as caught:
        agent.enrich(GlossaryTerm("Alice", "艾丽丝"), PASSAGES)
    assert caught.value.received_count == caught.value.unknown_count == 11
    assert "UNKNOWN_PRIVATE_REFERENCE" not in str(caught.value)


@pytest.mark.parametrize("references", [1, "p1", [None], [{"PRIVATE": "p1"}]])
def test_malformed_reference_values_fail_schema_validation_without_leaking_input(references):
    agent = worker({"note": "Supported description.", "evidence_refs": references})
    with pytest.raises(ValueError, match="Invalid terminology knowledge") as caught:
        agent.enrich(GlossaryTerm("Alice", "艾丽丝"), PASSAGES)
    assert "PRIVATE" not in str(caught.value)


@pytest.mark.parametrize("references", [["same", "same"], ["p2", "other"], ["", "other"]])
def test_ambiguous_or_empty_input_identities_fail_before_calling_the_model(references):
    passages = [
        passage.model_copy(update={"reference": reference})
        for passage, reference in zip(PASSAGES, references)
    ]
    agent = worker({"note": "Supported description.", "evidence_refs": ["p1"]})
    with pytest.raises(ValueError, match="citation"):
        agent.enrich(GlossaryTerm("Alice", "艾丽丝"), passages)
    assert agent.client.calls == []


def test_local_label_scope_resets_between_requests_without_joining_source_identities():
    agent = worker({"note": "Supported description.", "evidence_refs": ["p1"]})
    first = agent.enrich(GlossaryTerm("Alice", "艾丽丝"), PASSAGES[:1])
    second = agent.enrich(GlossaryTerm("Alice", "艾丽丝"), PASSAGES[1:])
    assert first.evidence_refs == [PASSAGES[0].reference]
    assert second.evidence_refs == [PASSAGES[1].reference]


def test_twenty_one_passages_and_eleven_local_citations_preserve_exact_source_coverage():
    passages = [
        SourcePassage(
            reference=f"c0:s{index}:0-400",
            chapter=0,
            segment=index,
            source=f"Alice appeared in invented passage {index}.",
        )
        for index in range(21)
    ]
    agent = worker(
        {
            "note": "Supported description.",
            "evidence_refs": [f"p{index}" for index in range(1, 22, 2)],
        }
    )
    result = agent.enrich(GlossaryTerm("Alice", "艾丽丝"), passages)
    assert result.evidence_refs == [passage.reference for passage in passages[::2]]
    assert len(payload(agent.client)["passages"]) == 21
    assert len(agent.client.calls) == 1  # No automatic paid correction request.


def test_cached_and_published_knowledge_uses_only_canonical_references(tmp_path):
    store, chapters = book_store(tmp_path)
    agent = worker({"note": "Supported description.", "evidence_refs": ["p1"]})
    service = TerminologyService(store, agent, chapters)
    service.admit(GlossaryTerm("Alice", "艾丽丝"))
    keys = store.list_artifacts("terminology/requests/evidence/")
    assert len(keys) == 1
    cached = store.read_artifact(keys[0])
    assert cached["evidence_refs"] == [service.index.passages[0].reference]
    entry = store.read_artifact(store.list_artifacts("terminology/entries/")[0])
    assert entry["knowledge"]["evidence_refs"] == cached["evidence_refs"]
    assert all(reference.startswith("c") for reference in entry["locations"])
    count = len(agent.client.calls)
    TerminologyService.for_store(store, agent).admit(GlossaryTerm("Alice", "艾丽丝"))
    assert len(agent.client.calls) == count


def test_existing_canonical_result_cache_is_reused_without_a_model_call(tmp_path):
    store, chapters = book_store(tmp_path)
    agent = worker(None)  # Any uncached model request would fail validation.
    service = TerminologyService(store, agent, chapters)
    term = GlossaryTerm("Alice", "艾丽丝")
    group = service.index.evidence_groups(term, store.all_terms())[0]
    # Existing request keys contain canonical domain inputs, not ephemeral wire labels.
    digest = content_hash([service.identity, [asdict(term), [p.model_dump() for p in group]]])
    key = f"terminology/requests/evidence/{digest}.json"
    saved = KnowledgeResult(note="Previously supported note.", evidence_refs=[group[0].reference])
    store.write_artifact(key, saved.model_dump())
    assert service.admit(term) == "inserted"
    assert agent.client.calls == []
    assert store.read_artifact(key) == saved.model_dump()
    assert store.get_term("Alice").note == "Previously supported note."
