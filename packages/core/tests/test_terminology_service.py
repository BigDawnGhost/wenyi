"""Offline admission and recovery tests using the real file adapter."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from wenyi_core.config import Config
from wenyi_core.glossary.evidence_models import (
    DiscoveryResult,
    KnowledgeResult,
)
from wenyi_core.glossary.extractor import GlossaryExtractor, TranslatedSegmentEvidence
from wenyi_core.glossary.store import GlossaryTerm
from wenyi_core.ingest.models import Chapter, Document, Segment
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.pipeline.terminology import TerminologyService
from wenyi_core.storage.file import FileStorage


class EvidenceAgent:
    language_policy = SimpleNamespace(fingerprint="test-policy-v1")

    def __init__(self):
        self.config = Config(source_lang="en", target_lang="zh")
        self.calls = []
        self.fail_evidence = False
        self.empty_evidence = False

    def discover(self, passages, existing):
        self.calls.append(("discover", tuple(p.reference for p in passages)))
        return DiscoveryResult(
            terms=[GlossaryTerm("Alice", "艾丽丝"), GlossaryTerm("Carl", "卡尔")],
        )

    def enrich(self, term, passages):
        self.calls.append(("evidence", tuple(p.reference for p in passages)))
        if self.fail_evidence:
            raise RuntimeError("Injected evidence interruption")
        if self.empty_evidence:
            return KnowledgeResult()
        return KnowledgeResult(
            note=f"{term.target}：根据全书原文确认的人物信息。",
            evidence_refs=[p.reference for p in passages],
        )

    def merge(self, term, results):
        self.calls.append(("merge", len(results)))
        return KnowledgeResult(
            note=results[0].note,
            evidence_refs=list(
                dict.fromkeys(ref for item in results for ref in item.evidence_refs)
            ),
        )


def book_store(tmp_path):
    chapters = [
        Chapter(index=0, segments=[Segment(index=7, source="Alice waited for Carl, her uncle.")]),
        Chapter(
            index=1,
            segments=[
                Segment(index=3, source="Beth was Alice's mother."),
                Segment(index=9, source="Carl was Beth's elder brother."),
            ],
        ),
    ]
    store = FileStorage(str(tmp_path / "run"))
    source = tmp_path / "source.txt"
    source.write_text("\n".join(s.source for chapter in chapters for s in chapter.segments))
    store.init_from_document(
        Document(
            title="Invented family",
            source_lang="en",
            target_lang="zh",
            fmt="text",
            source_path=str(source),
            chapters=chapters,
        )
    )
    return store, chapters


def test_admission_precedes_publication_and_reuses_successful_requests(tmp_path):
    store, chapters = book_store(tmp_path)
    agent = EvidenceAgent()
    term = GlossaryTerm("Alice", "艾丽丝", note="Local-only observation")
    assert TerminologyService(store, agent, chapters).admit(term, 0) == "inserted"
    assert "全书" in store.get_term("Alice").note
    count = len(agent.calls)
    resumed = TerminologyService.for_store(store, agent)
    resumed.admit(replace(term, note="Shorter local note"), 0)
    assert len(agent.calls) == count
    assert "全书" in store.get_term("Alice").note


def test_evidence_only_protocol_does_not_reuse_prior_experiment_caches(tmp_path):
    store, chapters = book_store(tmp_path)
    chapters[0].segments[0].target = "Already saved translation."
    store.save_chapter(chapters[0])
    before = store.load_chapter(0)
    agent = EvidenceAgent()
    term = GlossaryTerm("Alice", "艾丽丝")
    previous = TerminologyService(store, agent, chapters)
    previous.identity["version"] = 3
    previous.admit(term)
    old_requests = {
        key: store.read_artifact(key) for key in store.list_artifacts("terminology/requests/")
    }
    count = len(agent.calls)
    current = TerminologyService(store, agent, chapters)
    assert current.identity["version"] == 4
    assert current._current_record("Alice") is None
    current.admit(term)
    assert len(agent.calls) > count
    count = len(agent.calls)
    current.admit(term)
    assert len(agent.calls) == count
    assert all(store.read_artifact(key) == value for key, value in old_requests.items())
    assert store.load_chapter(0) == before


def test_unresolved_history_defers_new_term_but_keeps_confirmed_terms_and_saved_targets(tmp_path):
    store, chapters = book_store(tmp_path)
    previous = chapters[0]
    previous.segments[0].target = "Already saved translation."
    store.save_chapter(previous)
    before = store.load_chapter(0).model_dump()
    store.upsert_term(GlossaryTerm("Carl", "人工译名", note="Manual note"))
    service = TerminologyService(store, EvidenceAgent(), chapters)
    extractor = GlossaryExtractor(
        FakeClient(handler=lambda *args: '{"terms":[]}'), Config(source_lang="en", target_lang="zh")
    )
    history = [
        TranslatedSegmentEvidence(
            chapter=0,
            segment=previous.segments[0].index,
            source=previous.segments[0].source,
            target=previous.segments[0].target,
        )
    ]
    service.prepare_batch(
        1, chapters[1].segments, extractor=extractor, history=history, before=(1, 0)
    )
    assert store.get_term("Alice") is None
    assert store.get_term("Carl").target == "人工译名"
    assert store.get_term("Carl").note == "Manual note"
    assert store.load_chapter(0).model_dump() == before
    assert store.list_events(event_type="glossary_history_alignment_deferred")


def test_empty_groups_are_cached_as_reviewed_but_not_published_as_knowledge(tmp_path):
    store, chapters = book_store(tmp_path)
    agent = EvidenceAgent()
    service = TerminologyService(store, agent, chapters)
    agent.empty_evidence = True
    assert service.admit(GlossaryTerm("Alice", "艾丽丝")) == "unresolved"
    assert store.get_term("Alice") is None
    assert store.list_artifacts("terminology/requests/evidence/")
    count = len(agent.calls)
    assert service.admit(GlossaryTerm("Alice", "艾丽丝")) == "unresolved"
    assert len(agent.calls) == count
    agent.empty_evidence = False
    agent.fail_evidence = True
    with pytest.raises(RuntimeError, match="interruption"):
        service.admit(GlossaryTerm("Carl", "卡尔"))
    agent.fail_evidence = False
    service.admit(GlossaryTerm("Carl", "卡尔"))
    assert store.get_term("Carl") is not None


def test_all_groups_complete_before_admission_and_merge_results_are_cached(tmp_path):
    store, _ = book_store(tmp_path)
    chapters = [Chapter(index=0, segments=[Segment(index=0, source="Alice " + "text " * 6000)])]
    agent = EvidenceAgent()
    service = TerminologyService(store, agent, chapters)
    service.admit(GlossaryTerm("Alice", "艾丽丝"))
    assert sum(kind == "evidence" for kind, _ in agent.calls) >= 3
    assert any(kind == "merge" for kind, _ in agent.calls)
    entries = store.list_artifacts("terminology/entries/")
    record = store.read_artifact(entries[0])
    assert record["status"] == "published"
    assert sum(location[3] for location in record["locations"].values()) == len(
        chapters[0].segments[0].source
    )
    count = len(agent.calls)
    service.admit(GlossaryTerm("Alice", "艾丽丝"))
    assert len(agent.calls) == count


def test_manual_changes_remain_protected_across_more_than_one_refresh(tmp_path):
    store, chapters = book_store(tmp_path)
    agent = EvidenceAgent()
    service = TerminologyService(store, agent, chapters)
    term = GlossaryTerm("Alice", "艾丽丝")
    service.admit(term)
    store.upsert_term(replace(store.get_term("Alice"), note="Human-approved note"))
    service.admit(term)
    service.admit(term)
    assert store.get_term("Alice").note == "Human-approved note"
    assert store.read_artifact(store.list_artifacts("terminology/entries/")[0])[
        "protected_fields"
    ] == ["note"]


def test_publication_recovers_after_glossary_write_interruption(tmp_path, monkeypatch):
    store, chapters = book_store(tmp_path)
    agent = EvidenceAgent()
    original = store.upsert_term

    def interrupted(term, chapter=None):
        original(term, chapter)
        raise RuntimeError("after glossary commit")

    monkeypatch.setattr(store, "upsert_term", interrupted)
    with pytest.raises(RuntimeError, match="after glossary"):
        TerminologyService(store, agent, chapters).admit(GlossaryTerm("Alice", "艾丽丝"))
    count = len(agent.calls)
    key = store.list_artifacts("terminology/entries/")[0]
    assert store.read_artifact(key)["status"] == "prepared"
    monkeypatch.setattr(store, "upsert_term", original)
    TerminologyService(store, agent, chapters).admit(GlossaryTerm("Alice", "艾丽丝"))
    assert len(agent.calls) == count
    assert store.read_artifact(key)["status"] == "published"
    assert store.read_artifact(key)["protected_fields"] == []


def test_source_and_policy_changes_invalidate_evidence(tmp_path):
    store, chapters = book_store(tmp_path)
    agent = EvidenceAgent()
    term = GlossaryTerm("Alice", "艾丽丝")
    TerminologyService(store, agent, chapters).admit(term)
    count = len(agent.calls)
    chapters[1].segments[0].source += " Alice later returned."
    TerminologyService(store, agent, chapters).admit(term)
    assert len(agent.calls) > count
    count = len(agent.calls)
    agent.language_policy = SimpleNamespace(fingerprint="test-policy-v2")
    TerminologyService(store, agent, chapters).admit(term)
    assert len(agent.calls) > count


def test_different_candidate_mapping_never_replaces_established_target(tmp_path):
    store, chapters = book_store(tmp_path)
    agent = EvidenceAgent()
    service = TerminologyService(store, agent, chapters)
    service.admit(GlossaryTerm("Alice", "艾丽丝"))
    assert service.admit(GlossaryTerm("Alice", "另一译名")) == "conflict"
    service.admit(GlossaryTerm("Alice", "另一译名"))
    assert store.get_term("Alice").target == "艾丽丝"
    assert len(store.open_conflicts()) == 1


@pytest.mark.parametrize("change", ["target", "alias", "delete"])
def test_identity_changes_during_collection_do_not_publish_stale_evidence(
    tmp_path, monkeypatch, change
):
    store, chapters = book_store(tmp_path)
    store.upsert_term(GlossaryTerm("Alice", "艾丽丝"))
    agent = EvidenceAgent()
    original = agent.enrich

    def edit_during_collection(term, passages):
        if change == "target":
            store.resolve_term("Alice", "人工译名")
        elif change == "alias":
            store.upsert_term(replace(store.get_term("Alice"), aliases=["Ace"]))
        else:
            store.delete_term("Alice")
        return original(term, passages)

    monkeypatch.setattr(agent, "enrich", edit_during_collection)
    service = TerminologyService(store, agent, chapters)
    with pytest.raises(ValueError, match="identity changed"):
        service.admit(GlossaryTerm("Alice", "艾丽丝"))
    assert store.list_artifacts("terminology/entries/") == []
    current = store.get_term("Alice")
    if change == "delete":
        assert current is None
    else:
        assert current.note == ""
        assert current.target == ("人工译名" if change == "target" else "艾丽丝")
        assert current.aliases == (["Ace"] if change == "alias" else [])


def test_known_terms_refresh_after_identity_edit_even_if_discovery_omits_them(
    tmp_path, monkeypatch
):
    store, chapters = book_store(tmp_path)
    agent = EvidenceAgent()
    service = TerminologyService(store, agent, chapters)
    service.admit(GlossaryTerm("Alice", "艾丽丝"))
    store.resolve_term("Alice", "人工译名")
    assert service._current_record("Alice") is None
    resumed = TerminologyService.for_store(store, agent)
    assert resumed._current_record("Alice") is None
    monkeypatch.setattr(agent, "discover", lambda *args, **kwargs: DiscoveryResult())
    extractor = GlossaryExtractor(FakeClient(), Config(source_lang="en", target_lang="zh"))
    count = len(agent.calls)
    resumed.prepare_batch(0, chapters[0].segments, extractor=extractor)
    assert len(agent.calls) > count
    assert store.get_term("Alice").note.startswith("人工译名")
    assert resumed._current_record("Alice") is not None
    count = len(agent.calls)
    resumed.prepare_batch(0, chapters[0].segments, extractor=extractor)
    assert len(agent.calls) == count


def test_pretranslation_admits_terms_and_returns_none(tmp_path):
    store, chapters = book_store(tmp_path)
    agent = EvidenceAgent()
    extractor = GlossaryExtractor(FakeClient(), Config(source_lang="en", target_lang="zh"))
    service = TerminologyService(store, agent, chapters)
    assert service.prepare_batch(0, chapters[0].segments, extractor=extractor) is None
    assert store.get_term("Alice") and store.get_term("Carl")
    count = len(agent.calls)
    resumed = TerminologyService.for_store(store, agent)
    assert resumed.prepare_batch(0, chapters[0].segments, extractor=extractor) is None
    assert len(agent.calls) == count + 1  # Admission changed the discovery's glossary input.
    assert agent.calls[-1][0] == "discover"
    resumed.prepare_batch(0, chapters[0].segments, extractor=extractor)
    assert len(agent.calls) == count + 1


def test_discovery_cache_tracks_the_actual_glossary_input(tmp_path):
    store, chapters = book_store(tmp_path)
    agent = EvidenceAgent()
    service = TerminologyService(store, agent, chapters)
    passages = service.index.for_segments(0, chapters[0].segments)
    service._discover(passages)
    store.upsert_term(GlossaryTerm("Alice", "人工译名", aliases=["Ace"]))
    service._discover(passages)
    assert [kind for kind, _ in agent.calls] == ["discover", "discover"]
    service._discover(passages)
    assert [kind for kind, _ in agent.calls] == ["discover", "discover"]


def test_alias_suggestions_cannot_recreate_removed_or_competing_aliases(tmp_path):
    store, chapters = book_store(tmp_path)
    store.upsert_term(GlossaryTerm("Carl", "卡尔"))
    store.upsert_term(GlossaryTerm("Alice", "艾丽丝"))
    agent = EvidenceAgent()
    original = agent.enrich

    def proposed_aliases(term, passages):
        return original(term, passages).model_copy(update={"aliases": ["Carl"]})

    agent.enrich = proposed_aliases
    service = TerminologyService(store, agent, chapters)
    service.admit(GlossaryTerm("Alice", "艾丽丝", aliases=["Removed alias"]))
    assert store.get_term("Alice").aliases == []
    record = store.read_artifact(store.list_artifacts("terminology/entries/")[0])
    assert record["knowledge"]["aliases"] == ["Carl"]


def test_a_valid_empty_evidence_group_does_not_discard_supported_groups(tmp_path):
    store, _ = book_store(tmp_path)
    chapters = [Chapter(index=0, segments=[Segment(index=0, source="Alice " + "text " * 6000)])]
    agent = EvidenceAgent()
    original = agent.enrich

    def only_supported(term, passages):
        if not any("Alice" in passage.source for passage in passages):
            agent.calls.append(("empty", tuple(p.reference for p in passages)))
            return KnowledgeResult()
        return original(term, passages)

    agent.enrich = only_supported
    service = TerminologyService(store, agent, chapters)
    assert service.admit(GlossaryTerm("Alice", "艾丽丝")) == "inserted"
    assert any(kind == "empty" for kind, _ in agent.calls)
    record = store.read_artifact(store.list_artifacts("terminology/entries/")[0])
    assert sum(location[3] for location in record["locations"].values()) == len(
        chapters[0].segments[0].source
    )


@pytest.mark.parametrize("source", ["brother", "sister", "Brother"])
def test_source_attested_words_use_general_admission_without_special_filtering(tmp_path, source):
    store, _ = book_store(tmp_path)
    chapters = [Chapter(index=0, segments=[Segment(index=0, source=f"Her {source} arrived.")])]
    agent = EvidenceAgent()
    service = TerminologyService(store, agent, chapters)
    assert service.admit(GlossaryTerm(source, "候选译文")) == "inserted"
    assert store.get_term(source).target == "候选译文"
    assert agent.calls
