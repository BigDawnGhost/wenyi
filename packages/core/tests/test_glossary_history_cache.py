"""Offline historical mapping cache regressions using temporary file storage."""

import json
from dataclasses import replace

import pytest
from wenyi_core.config import Config
from wenyi_core.glossary.extractor import GlossaryExtractor, TranslatedSegmentEvidence
from wenyi_core.glossary.store import GlossaryTerm
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.pipeline.terminology import TerminologyService

from tests.test_terminology_service import EvidenceAgent, book_store

REQUESTS = "glossary/history/requests/"
DIAGNOSTICS = "glossary/history/diagnostics/"


def extractor(response):
    client = FakeClient(handler=lambda m, t, j: json.dumps({"terms": response}))
    return GlossaryExtractor(client, Config(source_lang="en", target_lang="zh")), client


def candidates():
    terms = [
        GlossaryTerm("PRIVATE_Alice", "PRIVATE_proposal"),
        GlossaryTerm("New", "新"),
        GlossaryTerm("PRIVATE_Carl", "PRIVATE_guess"),
    ]
    evidence = TranslatedSegmentEvidence(
        0, 7, "PRIVATE_source", "PRIVATE_target 历史译法 PRIVATE_resolution 另一译法"
    )
    return terms, {terms[0].source: evidence, terms[2].source: evidence}


@pytest.mark.parametrize("resolved", [True, False])
def test_positive_and_negative_cache_preserve_order_and_live_terms(tmp_path, resolved):
    store, _ = book_store(tmp_path)
    terms, occurrences = candidates()
    response = [
        {"source": "foreign", "target": "ignored"},
        {"source": terms[2].source, "target": "  "},
    ]
    if resolved:
        response.append({"source": terms[0].source, "target": "  历史译法  "})
    first, client = extractor(response)
    result, aligned, deferred = first._align_with_first_occurrences(terms, occurrences, store=store)
    assert [term.source for term in result] == ([terms[0].source, "New"] if resolved else ["New"])
    assert (aligned, deferred) == (int(resolved), 2 - int(resolved))
    assert len(client.calls) == 1
    keys = store.list_artifacts(REQUESTS)
    assert len(keys) == 1
    assert store.read_artifact(keys[0]) == {
        "schema_version": 1,
        "resolved": {terms[0].source: "历史译法"} if resolved else {},
    }
    live = [replace(term, note="Live metadata") for term in terms]
    resumed, resumed_client = extractor([])
    replay, replay_aligned, replay_deferred = resumed._align_with_first_occurrences(
        live, occurrences, store=store
    )
    assert (replay_aligned, replay_deferred) == (aligned, deferred)
    assert all(term.note == "Live metadata" for term in replay)
    assert next(term for term in replay if term.source == "New") is live[1]
    assert resumed_client.calls == []


def test_deferred_audit_is_count_only_and_not_repeated(tmp_path, caplog):
    store, _ = book_store(tmp_path)
    terms, occurrences = candidates()
    first, _ = extractor([{"source": terms[0].source, "target": "PRIVATE_resolution"}])
    first._align_with_first_occurrences(terms, occurrences, store=store)
    keys = store.list_artifacts(DIAGNOSTICS)
    assert len(keys) == 1
    diagnostic = store.read_artifact(keys[0])
    assert diagnostic == {
        "schema_version": 1,
        "request_key": store.list_artifacts(REQUESTS)[0],
        "operation": "glossary.align_history",
        "reason": "unresolved",
        "candidate_count": 2,
        "aligned_count": 1,
        "deferred_count": 1,
        "deferred_indices": [1],
    }
    events = store.list_events(event_type="glossary_history_alignment_deferred")
    assert len(events) == 1
    assert "PRIVATE" not in json.dumps(diagnostic)
    assert "PRIVATE" not in json.dumps(events)
    assert "PRIVATE" not in caplog.text
    assert caplog.records
    caplog.clear()
    resumed, client = extractor([])
    resumed._align_with_first_occurrences(terms, occurrences, store=store)
    assert client.calls == []
    assert caplog.records == []
    assert store.list_artifacts(DIAGNOSTICS) == keys
    assert store.list_events(event_type="glossary_history_alignment_deferred") == events


@pytest.mark.parametrize(
    "change", ["source", "target", "proposal", "binding", "prompt", "policy", "routing"]
)
def test_cache_binds_historical_inputs_and_rendered_prompts(tmp_path, monkeypatch, change):
    store, _ = book_store(tmp_path)
    terms, occurrences = candidates()
    first, _ = extractor([])
    first._align_with_first_occurrences(terms, occurrences, store=store)
    resumed, client = extractor([])
    if change in {"source", "target"}:
        occurrences = {
            source: replace(evidence, **{change: "Changed historical text"})
            for source, evidence in occurrences.items()
        }
    elif change == "proposal":
        terms[0] = replace(terms[0], target="Changed proposal")
    elif change == "binding":
        manifest = store.load_manifest()
        manifest["source_sha256"] = "a" * 64
        store.save_manifest(manifest)
    elif change == "policy":
        resumed = GlossaryExtractor(client, Config(source_lang="en", target_lang="ja"))
    elif change == "routing":
        name = resumed.config.llm.tiers["fast"]
        profile = resumed.config.llm.models[name]
        resumed.config.llm.models[name] = profile.model_copy(
            update={"model": profile.model + "-changed"}
        )
    else:
        render = resumed.render
        monkeypatch.setattr(resumed, "render", lambda *a, **kw: render(*a, **kw) + "\nChanged")
    resumed._align_with_first_occurrences(terms, occurrences, store=store)
    assert len(client.calls) == 1
    assert len(store.list_artifacts(REQUESTS)) == 2


@pytest.mark.parametrize(
    "corruption",
    [
        {"schema_version": 99, "resolved": {}},
        {"schema_version": 1, "resolved": []},
        {"schema_version": 1, "resolved": {"foreign": "mapping"}},
        {"schema_version": 1, "resolved": {"PRIVATE_Alice": ""}},
        {"schema_version": 1, "resolved": {"PRIVATE_Alice": "Unattested proposal"}},
    ],
)
def test_corrupt_cache_fails_without_calling_provider(tmp_path, corruption):
    store, _ = book_store(tmp_path)
    terms, occurrences = candidates()
    first, _ = extractor([])
    first._align_with_first_occurrences(terms, occurrences, store=store)
    store.write_artifact(store.list_artifacts(REQUESTS)[0], corruption)
    resumed, client = extractor([])
    with pytest.raises(ValueError):
        resumed._align_with_first_occurrences(terms, occurrences, store=store)
    assert client.calls == []


@pytest.mark.parametrize("failure", ["provider", "json"])
def test_failed_requests_are_not_negative_cache_entries(tmp_path, failure):
    store, _ = book_store(tmp_path)
    terms, occurrences = candidates()

    def fail(messages, tier, json_mode):
        if failure == "provider":
            raise RuntimeError("Injected offline provider failure")
        return "{not valid json"

    client = FakeClient(handler=fail)
    first = GlossaryExtractor(client, Config(source_lang="en", target_lang="zh"))
    with pytest.raises((RuntimeError, ValueError)):
        first._align_with_first_occurrences(terms, occurrences, store=store)
    assert len(client.calls) == 1
    assert store.list_artifacts(REQUESTS) == []
    assert store.list_artifacts(DIAGNOSTICS) == []
    resumed, client = extractor([{"source": terms[0].source, "target": "历史译法"}])
    result, aligned, deferred = resumed._align_with_first_occurrences(
        terms, occurrences, store=store
    )
    assert (aligned, deferred) == (1, 1)
    assert result[0].target == "历史译法"
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "response,safe",
    [
        ('{"terms":[{"source":"PRIVATE_Alice","target":"历史译法"}]', True),
        ('{"terms":[{"source":"PRIVATE_Alice","target":"历史', False),
    ],
)
def test_repair_cannot_synthesize_a_cacheable_historical_mapping(tmp_path, response, safe):
    store, _ = book_store(tmp_path)
    terms, occurrences = candidates()
    client = FakeClient(handler=lambda *args: response)
    first = GlossaryExtractor(client, Config(source_lang="en", target_lang="zh"))
    if safe:
        _, aligned, _ = first._align_with_first_occurrences(terms, occurrences, store=store)
        assert aligned == 1
        assert store.list_artifacts(REQUESTS)
    else:
        with pytest.raises(ValueError, match="JSON repair"):
            first._align_with_first_occurrences(terms, occurrences, store=store)
        assert store.list_artifacts(REQUESTS) == []
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "targets",
    [["Unattested proposal"], ["历史译法", "另一译法"], ["历史译法", "历史译法"]],
)
def test_only_unambiguous_attested_mappings_are_accepted(tmp_path, targets):
    store, _ = book_store(tmp_path)
    terms, occurrences = candidates()
    first, client = extractor([{"source": terms[0].source, "target": target} for target in targets])
    result, aligned, deferred = first._align_with_first_occurrences(terms, occurrences, store=store)
    accepted = targets == ["历史译法", "历史译法"]
    assert (aligned, deferred) == (int(accepted), 2 - int(accepted))
    assert [term.source for term in result] == ([terms[0].source, "New"] if accepted else ["New"])
    assert len(client.calls) == 1
    assert store.read_artifact(store.list_artifacts(REQUESTS)[0])["resolved"] == (
        {terms[0].source: "历史译法"} if accepted else {}
    )


def test_pre_and_posttranslation_share_storage_cache(tmp_path, monkeypatch):
    store, _ = book_store(tmp_path)
    term = GlossaryTerm("Alice", "候选")
    evidence = TranslatedSegmentEvidence(0, 7, "Alice waited.", "爱丽丝等待。")
    first, _ = extractor([{"source": "Alice", "target": "爱丽丝"}])
    first._align_with_first_occurrences([term], {"Alice": evidence}, store=store)
    resumed, client = extractor([])
    monkeypatch.setattr(resumed, "extract", lambda *args: [replace(term)])
    summary = resumed.extract_and_store(
        store, "Alice returned.", "爱丽丝回来。", 1, history=[evidence], before=(1, 3)
    )
    assert summary["history_aligned"] == 1
    assert store.get_term("Alice").target == "爱丽丝"
    assert client.calls == []


def test_prepare_batch_defers_uncertain_terms_without_touching_saved_targets(tmp_path):
    store, chapters = book_store(tmp_path)
    chapters[0].segments[0].target = "Earlier saved translation 爱丽丝"
    store.save_chapter(chapters[0])
    store.upsert_term(GlossaryTerm("Beth", "贝丝"))
    agent = EvidenceAgent()
    first, client = extractor([{"source": "Alice", "target": "爱丽丝"}])
    service = TerminologyService(store, agent, chapters)
    evidence = TranslatedSegmentEvidence(
        0, 7, chapters[0].segments[0].source, chapters[0].segments[0].target
    )
    service.prepare_batch(
        1, chapters[1].segments, extractor=first, history=[evidence], before=(1, 3)
    )
    assert store.get_term("Alice").target == "爱丽丝"
    assert store.get_term("Carl") is None
    assert store.get_term("Beth").note
    assert store.load_chapter(0).segments[0].target == "Earlier saved translation 爱丽丝"
    assert len(client.calls) == 1
    # Repeat the identical historical request independently of admission's known-term filter.
    resumed, client = extractor([])
    resumed._align_with_first_occurrences(
        [GlossaryTerm("Alice", "艾丽丝"), GlossaryTerm("Carl", "卡尔")],
        {"Alice": evidence, "Carl": evidence},
        store=store,
    )
    assert client.calls == []
    assert store.load_chapter(0).segments[0].target == "Earlier saved translation 爱丽丝"
