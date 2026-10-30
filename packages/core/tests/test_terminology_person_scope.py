"""Only person entries use experimental discovery and whole-book evidence admission."""

import json
from dataclasses import replace

import pytest
from wenyi_core.glossary.evidence_models import DiscoveryResult
from wenyi_core.glossary.extractor import GlossaryExtractor
from wenyi_core.glossary.store import TYPE_PERSON, GlossaryTerm
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.pipeline.terminology import TerminologyService

from tests.fake_llm import MeteredFakeClient
from tests.storage_fixtures import require_value
from tests.terminology_fixtures import EvidenceAgent, FamilyHandler, book_store, inputs


@pytest.mark.parametrize(
    "term_type",
    [
        "term",
        "place",
        "organization",
        "technique",
        "item",
        "setting",
        "appellation",
        "honorific",
        "speech",
        "fixed_expression",
    ],
)
def test_nonperson_admission_uses_normal_glossary_without_evidence(tmp_path, term_type):
    store, chapters = book_store(tmp_path)
    agent = EvidenceAgent()
    agent.fail_evidence = True
    service = TerminologyService(store, agent, chapters)
    term = GlossaryTerm(
        "Alice",
        "已有译名",
        type=term_type,
        note="Local extraction note",
        aliases=["Local alias"],
        first_chapter=1,
    )

    assert service.admit(term, 1) == "inserted"
    assert store.get_term(term.source) == term
    assert service.admit(replace(term, target="不同建议"), 2) == "conflict"
    assert require_value(store.get_term(term.source)).target == term.target
    assert len(store.open_conflicts()) == 1
    assert agent.calls == []
    assert store.list_artifacts("terminology/") == []


@pytest.mark.parametrize(
    "stored_type,candidate_type",
    [(TYPE_PERSON, "term"), ("term", TYPE_PERSON)],
)
def test_stored_type_controls_evidence_scope(tmp_path, stored_type, candidate_type):
    store, chapters = book_store(tmp_path)
    store.upsert_term(GlossaryTerm("Alice", "艾丽丝", type=stored_type))
    agent = EvidenceAgent()
    service = TerminologyService(store, agent, chapters)

    service.admit(GlossaryTerm("Alice", "艾丽丝", type=candidate_type))

    current = require_value(store.get_term("Alice"))
    assert current.type == stored_type
    assert bool(agent.calls) is (stored_type == TYPE_PERSON)
    assert bool(current.note) is (stored_type == TYPE_PERSON)


def test_pretranslation_filters_nonpersons_before_history_and_refresh(tmp_path, monkeypatch):
    store, chapters = book_store(tmp_path)
    store.upsert_term(GlossaryTerm("Carl", "卡尔", type=TYPE_PERSON))
    store.upsert_term(GlossaryTerm("uncle", "叔叔", type="appellation", note="Local note"))
    agent = EvidenceAgent()
    monkeypatch.setattr(
        agent,
        "discover",
        lambda *args: DiscoveryResult(
            terms=[
                GlossaryTerm("Alice", "艾丽丝", type=TYPE_PERSON),
                GlossaryTerm("uncle", "叔叔", type="appellation"),
                GlossaryTerm("waited", "等待"),
            ]
        ),
    )
    extracted = []
    extractor = GlossaryExtractor(FakeClient(), agent.config)

    def first_occurrences(terms, *args):
        extracted.extend(terms)
        return {}

    monkeypatch.setattr(extractor, "_first_occurrences", first_occurrences)
    enriched = []
    original = agent.enrich

    def enrich(term, passages):
        enriched.append(term.source)
        return original(term, passages)

    monkeypatch.setattr(agent, "enrich", enrich)
    TerminologyService(store, agent, chapters).prepare_batch(
        0, chapters[0].segments, extractor=extractor, before=(0, 0)
    )

    assert [term.source for term in extracted] == ["Alice"]
    assert enriched == ["Alice", "Carl"]
    assert store.get_term("waited") is None
    assert require_value(store.get_term("uncle")).note == "Local note"


def test_mixed_pipeline_keeps_nonperson_backfill_without_enrichment(tmp_path):
    source, config, store = inputs(tmp_path)
    source.write_text(
        "# First\n\nAlice met Carl and Dora at the Tower with a sword.\n\n"
        "# Later\n\nBeth waited for Alice.",
        encoding="utf-8",
    )

    class MixedHandler(FamilyHandler):
        def __init__(self):
            super().__init__()
            self.enriched = []

        def __call__(self, messages, tier, json_mode):
            system, user = messages[0]["content"], messages[-1]["content"]
            if system.startswith("You extract terminology knowledge"):
                self.enriched.append(json.loads(user.split("\n", 1)[1])["term"])
            response = super().__call__(messages, tier, json_mode)
            if "pre-translation analyst" in system:
                data = json.loads(response)
                data["terms"] = [
                    {"source": "Tower", "target": "高塔", "type": "place", "note": "Seed note"}
                ]
                return json.dumps(data)
            if system.startswith("You discover terminology"):
                payload = json.loads(user.split("\n", 1)[1])
                refs = [p["reference"] for p in payload["passages"] if "sword" in p["source"]]
                data = json.loads(response)
                if refs:
                    data["terms"].append(
                        {"source": "sword", "target": "剑", "type": "item", "evidence_refs": refs}
                    )
                return json.dumps(data)
            if "terminology" in system and "extractor" in system:
                return json.dumps(
                    {
                        "terms": [
                            {
                                "source": "Tower",
                                "target": "高塔",
                                "type": "place",
                                "note": "Extracted note",
                            },
                            {
                                "source": "sword",
                                "target": "剑",
                                "type": "item",
                                "note": "Local note",
                            },
                            {"source": "Dora", "target": "朵拉", "type": "person"},
                        ]
                    }
                )
            return response

    handler = MixedHandler()
    client = MeteredFakeClient(handler=handler)
    orchestrator = Orchestrator(
        config, client=client, storage=store, allow_terminology_context=True
    )
    orchestrator.prepare(str(source))
    assert store.all_terms() == []
    assert handler.enriched == []
    orchestrator.run(str(source))

    assert handler.enriched
    assert all(term["type"] == TYPE_PERSON for term in handler.enriched)
    assert "Dora" in {term["source"] for term in handler.enriched}
    assert require_value(store.get_term("Tower")).note == "Extracted note"
    assert require_value(store.get_term("sword")).note == "Local note"
    assert require_value(store.get_term("Dora")).note.endswith("全书人物说明。")
    assert "sword →" not in handler.translation_inputs[0]
    assert "Tower →" not in handler.translation_inputs[0]
    assert store.pending_chapters() == []
    assert (
        require_value(store.load_usage())["totals"]["calls"]
        == client.usage_summary()["totals"]["calls"]
    )
    saved = store.load_chapter(0)
    usage = store.load_usage()
    resumed = MeteredFakeClient(handler=MixedHandler())
    Orchestrator(config, client=resumed, storage=store, allow_terminology_context=True).run(
        str(source)
    )
    assert resumed.calls == []
    assert store.load_chapter(0) == saved
    assert store.load_usage() == usage
