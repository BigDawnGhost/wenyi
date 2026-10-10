"""Offline discovery filtering, safe auditing, and downstream admission regressions."""

import json

import pytest
from wenyi_core.agents.terminology import TerminologyAgent
from wenyi_core.config import Config
from wenyi_core.glossary.evidence_models import DiscoveryResult, SourcePassage
from wenyi_core.glossary.store import GlossaryTerm
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.pipeline.terminology import TerminologyService

from tests.fake_llm import MeteredFakeClient
from tests.test_terminology_agent import FakeClient
from tests.test_terminology_pipeline import FamilyHandler, inputs
from tests.test_terminology_service import book_store

PASSAGES = [
    SourcePassage(reference="c0:s0:0-30", chapter=0, segment=0, source="Ann met Bob's mother."),
    SourcePassage(reference="c0:s1:0-20", chapter=0, segment=1, source="Anna greeted Carl."),
]


def candidate(source="Ann", refs=None, **fields):
    return {
        "source": source,
        "target": "译名",
        "evidence_refs": ["p1"] if refs is None else refs,
        **fields,
    }


def discover(data, *, existing=()):
    client = FakeClient(data)
    agent = TerminologyAgent(client, Config(source_lang="en", target_lang="zh"))
    result = agent.discover(PASSAGES, list(existing))
    assert len(client.calls) == 1  # Rejections never cause an automatic correction call.
    return result


def rejection_rows(result):
    return [item.model_dump() for item in result.rejections]


def rejected(collection, index, reason):
    return {"collection": collection, "index": index, "reason": reason}


def test_mixed_terms_keep_input_order_and_require_exact_cited_source_membership():
    proposals = [
        candidate(),
        {"source": "Bob", "target": "PRIVATE_MISSING"},
        candidate("Bob", []),
        candidate("Bob", ["p99"]),
        candidate("PRIVATE_ABSENT"),
        candidate("Carl"),  # Present in the input, but not in the cited passage.
        candidate("Ann", ["p2"]),  # Ann must not match the substring in Anna.
        candidate("Bob", target=42),
        candidate("Bob", [PASSAGES[0].reference]),
        candidate("Anna", ["p2"]),
        candidate("Carl", [PASSAGES[1].reference + " "]),
        candidate("Carl", ["P2"]),
        candidate("Carl", ["p1", "p99"]),
    ]
    result = discover({"terms": proposals}, existing=[GlossaryTerm("Ann", "权威译名")])
    assert [(term.source, term.target) for term in result.terms] == [
        ("Ann", "权威译名"),
        ("Bob", "译名"),
        ("Anna", "译名"),
    ]
    assert rejection_rows(result) == [
        rejected("terms", 1, "missing"),
        rejected("terms", 2, "missing"),
        rejected("terms", 3, "outside_input"),
        rejected("terms", 4, "source_absent"),
        rejected("terms", 5, "source_absent"),
        rejected("terms", 6, "source_absent"),
        rejected("terms", 7, "invalid_fields"),
        rejected("terms", 10, "outside_input"),
        rejected("terms", 11, "outside_input"),
        rejected("terms", 12, "outside_input"),
    ]


def test_all_rejected_is_audited_and_invalid_duplicate_cannot_hide_later_valid_term():
    proposals = [candidate(refs=[]), None, candidate("PRIVATE_ABSENT")]
    result = discover({"terms": proposals})
    assert result.terms == []
    assert rejection_rows(result) == [
        rejected("terms", 0, "missing"),
        rejected("terms", 1, "invalid_fields"),
        rejected("terms", 2, "source_absent"),
    ]
    later = discover({"terms": [*proposals, candidate()]})
    assert [term.source for term in later.terms] == ["Ann"]
    assert rejection_rows(later) == rejection_rows(result)


@pytest.mark.parametrize(
    "data",
    [None, [], {"terms": {}}],
)
def test_invalid_envelopes_still_stop(data):
    with pytest.raises(ValueError):
        discover(data)


def test_service_caches_filtered_results_safe_audit_and_usage_once(tmp_path, caplog):
    store, chapters = book_store(tmp_path)
    data = {
        "terms": [candidate("PRIVATE_ABSENT"), candidate("Alice")],
    }
    client = MeteredFakeClient(handler=lambda *args: json.dumps(data))
    agent = TerminologyAgent(client, Config(source_lang="en", target_lang="zh"))
    service = TerminologyService(
        store,
        agent,
        chapters,
        checkpoint_usage=lambda: store.save_usage(client.usage_summary()),
    )
    passages = service.index.for_segments(0, chapters[0].segments)
    result = service._discover(passages)
    assert [term.source for term in result.terms] == ["Alice"]
    rows = [
        rejected("terms", 0, "source_absent"),
    ]
    assert rejection_rows(result) == rows
    request_keys = store.list_artifacts("terminology/requests/discover/")
    assert len(request_keys) == 1
    cached = store.read_artifact(request_keys[0])
    assert cached["rejections"] == rows
    assert [term["source"] for term in cached["terms"]] == ["Alice"]
    diagnostic_keys = store.list_artifacts("terminology/diagnostics/discover/")
    assert diagnostic_keys == [
        request_keys[0].replace("terminology/requests/", "terminology/diagnostics/", 1)
    ]
    diagnostic = store.read_artifact(diagnostic_keys[0])
    assert diagnostic == {
        "schema_version": 1,
        "request_key": request_keys[0],
        "operation": "terminology.discover",
        "reason": "rejected_proposals",
        "accepted_terms_count": 1,
        "rejected_count": 1,
        "rejections": rows,
    }
    events = store.list_events(event_type="terminology_discovery_proposals_rejected")
    assert len(events) == 1
    assert "PRIVATE" not in json.dumps([diagnostic, events]) + caplog.text
    usage = store.load_usage()
    assert usage["totals"]["calls"] == 1
    resumed = TerminologyService.for_store(store, agent)
    again = resumed._discover(passages)
    assert again == result
    assert len(client.calls) == 1
    assert store.load_usage() == usage == client.usage_summary()
    assert store.list_events(event_type="terminology_discovery_proposals_rejected") == events
    assert store.list_artifacts("terminology/diagnostics/discover/") == diagnostic_keys


def test_service_caches_all_rejected_results_without_retrying_or_publishing(tmp_path):
    store, chapters = book_store(tmp_path)
    client = MeteredFakeClient(
        handler=lambda *args: json.dumps({"terms": [candidate("PRIVATE_ABSENT")]})
    )
    agent = TerminologyAgent(client, Config(source_lang="en", target_lang="zh"))
    service = TerminologyService(store, agent, chapters)
    passages = service.index.for_segments(0, chapters[0].segments)
    result = service._discover(passages)
    assert result.terms == []
    assert rejection_rows(result) == [rejected("terms", 0, "source_absent")]
    assert store.all_terms() == []
    assert store.list_artifacts("terminology/entries/") == []
    diagnostic = store.read_artifact(store.list_artifacts("terminology/diagnostics/discover/")[0])
    assert diagnostic["accepted_terms_count"] == 0
    assert diagnostic["rejected_count"] == 1
    assert service._discover(passages) == result
    assert len(client.calls) == 1
    assert len(store.list_events(event_type="terminology_discovery_proposals_rejected")) == 1


def test_legacy_discovery_cache_without_rejections_is_reused(tmp_path):
    store, chapters = book_store(tmp_path)
    client = MeteredFakeClient(handler=lambda *args: json.dumps({"terms": [candidate("Alice")]}))
    agent = TerminologyAgent(client, Config(source_lang="en", target_lang="zh"))
    service = TerminologyService(store, agent, chapters)
    passages = service.index.for_segments(0, chapters[0].segments)
    first = service._discover(passages)
    key = store.list_artifacts("terminology/requests/discover/")[0]
    legacy = store.read_artifact(key)
    legacy.pop("rejections", None)
    store.write_artifact(key, legacy)
    usage = client.usage_summary()
    result = TerminologyService.for_store(store, agent)._discover(passages)
    assert result.terms == first.terms
    assert result.rejections == DiscoveryResult().rejections == []
    assert len(client.calls) == 1
    assert client.usage_summary() == usage
    assert store.list_events(event_type="terminology_discovery_proposals_rejected") == []


def test_orchestrator_rejected_proposal_never_reaches_enrichment_or_formal_glossary(tmp_path):
    class MixedHandler(FamilyHandler):
        def __init__(self):
            super().__init__()
            self.enriched_sources = []

        def __call__(self, messages, tier, json_mode):
            system = messages[0]["content"]
            if system.startswith("You extract terminology knowledge"):
                payload = json.loads(messages[-1]["content"].split("\n", 1)[1])
                self.enriched_sources.append(payload["term"]["source"])
            response = super().__call__(messages, tier, json_mode)
            if system.startswith("You discover terminology"):
                data = json.loads(response)
                data["terms"].insert(0, candidate("PRIVATE_ABSENT"))
                return json.dumps(data)
            return response

    source, config, store = inputs(tmp_path)
    handler = MixedHandler()
    client = MeteredFakeClient(handler=handler)
    Orchestrator(config, client=client, storage=store, allow_terminology_context=True).run(
        str(source)
    )
    assert handler.translation_inputs
    assert store.pending_chapters() == []
    assert store.get_term("Alice") is not None
    assert store.get_term("PRIVATE_ABSENT") is None
    assert "PRIVATE_ABSENT" not in handler.enriched_sources
    assert store.list_events(event_type="terminology_discovery_proposals_rejected")
    assert store.load_usage()["totals"]["calls"] == client.usage_summary()["totals"]["calls"]
