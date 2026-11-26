"""Full-book scale regressions for navigation glossary entries and Book Bible CJK mining.

Long EPUBs store thousands of chapter titles as ``navigation`` glossary entries. They must
reach prompts only when the title occurs in the request text and never drive entity-only
consumers (batch risk, usage notes, synopsis mappings). Book Bible digest mining must keep
7-12 character CJK proper nouns intact and skip longer prose runs instead of slicing them.
"""

from __future__ import annotations

import json
from unittest.mock import Mock

from wenyi_core.agents.batch_risk import assess_batch_risk
from wenyi_core.agents.book_bible import _mine_japanese_spans, mine_bible_candidates
from wenyi_core.agents.glossary_note import GlossaryNoteAgent
from wenyi_core.config import Config
from wenyi_core.glossary.prompt_scope import (
    fit_terms_to_budget,
    prompt_terms,
    scope_navigation_terms,
)
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.models.document import Chapter, Document, Segment
from wenyi_core.models.glossary import TYPE_NAVIGATION, GlossaryTerm
from wenyi_core.pipeline.prepare.book_understanding import BookUnderstandingService
from wenyi_core.pipeline.run.runtime_usage import UsageCoordinator
from wenyi_core.pipeline.translate.glossary_notes import GlossaryNotes
from wenyi_core.storage.file import FileStorage

from tests.glossary_fixtures import book_store


def _nav(source: str, target: str) -> GlossaryTerm:
    return GlossaryTerm(source=source, target=target, type=TYPE_NAVIGATION)


def _terms() -> list[GlossaryTerm]:
    return [
        GlossaryTerm(source="Alice", target="爱丽丝", type="person"),
        _nav("The Copper Arch", "铜拱门"),
        GlossaryTerm(source="Lantern Guild", target="灯笼行会", type="organization"),
        _nav("Side Trail", "支路"),
        _nav("Night Market", "夜市"),
    ]


# --- prompt scoping ------------------------------------------------------------------------


def test_scope_navigation_terms_keeps_entities_and_only_matching_titles_in_order():
    terms = _terms()
    text = "Alice walked the Side Trail toward The Copper Arch."
    assert [t.source for t in scope_navigation_terms(terms, text)] == [
        "Alice",
        "The Copper Arch",
        "Lantern Guild",
        "Side Trail",
    ]
    assert [t.source for t in scope_navigation_terms(terms, "")] == ["Alice", "Lantern Guild"]
    entities = [t for t in terms if t.type != TYPE_NAVIGATION]
    assert scope_navigation_terms(entities, "anything") is entities


def test_prompt_terms_all_mode_omits_unrelated_navigation_titles():
    terms = _terms()
    text = "Alice reached the Night Market."
    expected = ["Alice", "Lantern Guild", "Night Market"]
    assert [t.source for t in prompt_terms(terms, text, budget=0, mode="all")] == expected
    assert [t.source for t in prompt_terms(terms, text, budget=10_000, mode="all")] == expected
    assert [t.source for t in prompt_terms(terms, text, budget=0, mode="relevant")] == [
        "Alice",
        "Night Market",
    ]


def test_fit_terms_to_budget_backfills_only_non_navigation_prefix():
    terms = _terms()
    text = "They argued at the Side Trail."
    assert [t.source for t in fit_terms_to_budget(terms, text, max_tokens=10_000)] == [
        "Alice",
        "Lantern Guild",
        "Side Trail",
    ]
    assert [t.source for t in fit_terms_to_budget(terms, "", max_tokens=10_000)] == [
        "Alice",
        "Lantern Guild",
    ]


# --- batch risk ----------------------------------------------------------------------------


def test_batch_risk_ignores_navigation_titles_for_glossary_misses():
    sources = ["威尔推门而入。", "他转身看着来人。"]
    drafts = ["Bill pushed the door open.", "He turned and looked at Jingxiang."]
    entity = [
        GlossaryTerm(source="威尔", target="Will", type="term"),
        GlossaryTerm(source="京襄", target="Jingxiang", type="place"),
    ]
    flagged = assess_batch_risk(
        sources, drafts, source_lang="zh", target_lang="en", glossary_terms=entity
    )
    codes = {(a.index, a.code) for a in flagged.alerts}
    assert (0, "glossary_miss") in codes

    navigation = [_nav(t.source, t.target) for t in entity]
    report = assess_batch_risk(
        sources, drafts, source_lang="zh", target_lang="en", glossary_terms=navigation
    )
    assert not any(a.code == "glossary_miss" for a in report.alerts)


# --- glossary notes ------------------------------------------------------------------------


def test_glossary_notes_never_process_navigation_titles(tmp_path):
    store, chapters = book_store(tmp_path)
    store.upsert_term(GlossaryTerm("Alice", "艾丽丝", type="person"))
    store.upsert_term(_nav("Carl", "卡尔"))
    client = FakeClient(lambda *_: '{"updates":[]}')
    config = Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {"preset": "fake"},
            "pipeline": {"glossary_note_workers": 1},
        }
    )
    service = GlossaryNotes(GlossaryNoteAgent(client, config), store)

    assert service.maintain(0, 0, chapters[0].text_segments) == {"Alice"}
    assert client.calls
    for call in client.calls:
        assert "卡尔" not in call["messages"][-1]["content"]
    assert store.get_term("Carl").note == ""


# --- book understanding --------------------------------------------------------------------


def test_book_understanding_skips_navigation_titles_in_synopsis_mappings(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("Temporary source text.", encoding="utf-8")
    store = FileStorage(str(tmp_path / "state"))
    store.init_from_document(
        Document(
            source_lang="en",
            target_lang="zh",
            fmt="text",
            source_path=str(source),
            chapters=[Chapter(index=0, segments=[Segment(index=0, source="Opening.")])],
        )
    )
    store.upsert_term(GlossaryTerm("Alice", "爱丽丝", type="person"))
    store.upsert_term(_nav("Night Market", "夜市"))
    config = Config.from_dict({"llm": {"preset": "fake"}})
    synopsizer, analyzer = Mock(), Mock()
    synopsizer.digest_chapter.return_value = "Alice opens the Night Market."
    synopsizer.book_synopsis.return_value = "Whole-book synopsis."
    analyzer.style_brief.return_value = "Restrained."
    service = BookUnderstandingService(config, synopsizer, analyzer, Mock(spec=UsageCoordinator))

    assert service.ensure_understanding(store) == "Whole-book synopsis."
    glossary = synopsizer.book_synopsis.call_args.kwargs["glossary"]
    assert [term.source for term in glossary] == ["Alice"]
    saved = json.dumps(store.load_analysis(), ensure_ascii=False)
    assert "夜市" not in saved

    # Resume reuses the synopsis; navigation entries never enter the mapping snapshot.
    synopsizer.reset_mock()
    assert service.ensure_understanding(store) == "Whole-book synopsis."
    synopsizer.book_synopsis.assert_not_called()


# --- Book Bible CJK spans -----------------------------------------------------------------

_CLUB = "特拉福买家俱乐部"
_PROSE = "银色列车穿过漫长黑夜驶向遥远北方港口"


def test_bible_digest_keeps_eight_char_proper_noun_intact():
    digests = [f"Ash joins the {_CLUB} ({_CLUB}) and meets its founders."]
    corpus = "\n".join(
        [
            f"那天晚上，{_CLUB}的会员都来了。",
            f"他第一次走进{_CLUB}的大门。",
            f"{_CLUB}在城东。",
        ]
    )
    sources = {c.source for c in mine_bible_candidates(digests, corpus, source_lang="zh")}
    assert _CLUB in sources
    assert "特拉福买家俱" not in sources
    assert "乐部" not in sources


def test_bible_digest_skips_long_prose_runs_instead_of_slicing():
    assert len(_PROSE) > 12
    digests = [f"The chapter opens: {_PROSE}."]
    corpus = f"{_PROSE}。\n{_PROSE}。\n他们沉默不语。"
    candidates = mine_bible_candidates(digests, corpus, source_lang="zh")
    assert not any(c.from_digest for c in candidates)


def test_japanese_digest_spans_keep_long_kanji_names_and_skip_prose():
    found = _mine_japanese_spans(f"特拉福買家倶楽部 and {_PROSE}")
    assert "特拉福買家倶楽部" in found
    assert not any(s in _PROSE for s in found)


# --- BibleSeed, post_normalize, alias notes & type promotion -------------------------------


def test_bible_seed_excludes_navigation_terms_from_fingerprint_and_prompts(tmp_path):
    from wenyi_core.agents.book_bible import BibleResult
    from wenyi_core.pipeline.translate.bible_seed import BibleSeedService

    store = FileStorage(str(tmp_path / "state"))
    try:
        manifest = store.stage_document(
            Document(
                source_lang="zh",
                target_lang="en",
                fmt="epub",
                chapters=[Chapter(index=0, segments=[Segment(index=0, source="洛邱来了。")])],
            ),
            source_hash="a" * 64,
        )
        store.save_manifest(manifest)
        store.upsert_term(GlossaryTerm("洛邱", "Luo Qiu", type="person"))
        # Pre-existing nav stub matching a compiled Bible term: promotion must also set
        # first_chapter=0 so it does not enter locked_user_terms and break resume caching.
        store.upsert_term(_nav("优夜", "You Ye"))
        agent = Mock()
        agent.compile_bible.return_value = BibleResult(
            terms=(GlossaryTerm("优夜", "You Ye", type="person", first_chapter=0),),
            variants_by_source={},
            candidate_count=1,
            divergent_count=0,
        )
        agent.pretranslate_quotes.return_value = {}
        agent.compile_voice_exemplars.return_value = ()
        cfg = Config.from_dict(
            {"llm": {"preset": "fake"}, "language": {"source": "zh", "target": "en"}}
        )
        service = BibleSeedService(agent, cfg)
        service.ensure_seeded(
            store,
            digests=["洛邱和优夜。"],
            source_corpus="洛邱来了。",
            book_synopsis="Syn",
            style="S",
        )
        assert agent.compile_bible.call_count == 1
        promoted = store.get_term("优夜")
        assert promoted.type == "person"
        assert promoted.first_chapter == 0

        # Simulate TitleTranslationService inserting EPUB nav titles with first_chapter=None.
        store.upsert_term(_nav("第1章 买与卖", "Chapter 1: Buying and Selling"))
        store.upsert_term(_nav("第2章 新手", "Chapter 2: Novice"))

        # Resume must hit the glossary_bible.json cache instead of recompiling.
        service.ensure_seeded(
            store,
            digests=["洛邱和优夜。"],
            source_corpus="洛邱来了。",
            book_synopsis="Syn",
            style="S",
        )
        assert agent.compile_bible.call_count == 1
    finally:
        store.close()


def test_glossary_notes_alias_matching_not_blocked_by_navigation_title(tmp_path):
    store, chapters = book_store(tmp_path)
    store.upsert_term(GlossaryTerm("Carl Vance", "卡尔·万斯", type="person", aliases=["Carl"]))
    store.upsert_term(_nav("Carl", "卡尔"))
    client = FakeClient(lambda *_: '{"updates":[]}')
    config = Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {"preset": "fake"},
            "pipeline": {"glossary_note_workers": 1},
        }
    )
    service = GlossaryNotes(GlossaryNoteAgent(client, config), store)
    # Chapter 0 mentions "Carl"; the navigation title must not claim the alias and suppress notes.
    assert service.maintain(0, 0, chapters[0].text_segments) == {"Carl Vance"}


def test_upsert_promotes_navigation_stub_to_entity_type(tmp_path):
    store = FileStorage(str(tmp_path / "state"))
    try:
        assert store.upsert_term(_nav("长安", "Chang'an")) == "inserted"
        assert store.get_term("长安").type == TYPE_NAVIGATION
        assert store.get_term("长安").first_chapter is None
        # Promotion from an extracted/Bible entity updates both type and first_chapter.
        assert (
            store.upsert_term(GlossaryTerm("长安", "Chang'an", type="place"), chapter=3)
            == "unchanged"
        )
        promoted = store.get_term("长安")
        assert promoted.type == "place"
        assert promoted.first_chapter == 3
        # A subsequent navigation upsert with the same target never demotes an entity or clears first_chapter.
        assert store.upsert_term(_nav("长安", "Chang'an")) == "unchanged"
        still_entity = store.get_term("长安")
        assert still_entity.type == "place"
        assert still_entity.first_chapter == 3
        # Manual user promotion (first_chapter=None, chapter=None) stays user-locked (first_chapter=None).
        assert store.upsert_term(_nav("洛阳", "Luoyang")) == "inserted"
        assert store.upsert_term(GlossaryTerm("洛阳", "Luoyang", type="place")) == "unchanged"
        manual = store.get_term("洛阳")
        assert manual.type == "place"
        assert manual.first_chapter is None
        # Context-dependent merge branch also promotes navigation -> entity and first_chapter.
        from wenyi_core.models.glossary import STATUS_CONTEXT_DEPENDENT

        assert (
            store.upsert_term(
                GlossaryTerm("大人", "Lord", type=TYPE_NAVIGATION, status=STATUS_CONTEXT_DEPENDENT)
            )
            == "inserted"
        )
        assert (
            store.upsert_term(GlossaryTerm("大人", "My Lord", type="appellation"), chapter=4)
            == "unchanged"
        )
        merged = store.get_term("大人")
        assert merged.type == "appellation"
        assert merged.first_chapter == 4
        assert "Lord" in merged.target and "My Lord" in merged.target
    finally:
        store.close()


def test_glossary_extractor_excludes_navigation_terms_from_existing(tmp_path):
    from wenyi_core.agents.glossary_extractor import GlossaryExtractor

    store = FileStorage(str(tmp_path / "state"))
    try:
        store.upsert_term(GlossaryTerm("蓝斯", "Lance", type="person"), chapter=0)
        store.upsert_term(_nav("第1章 蓝斯的归来", "Chapter 1: Lance Returns"))
        cfg = Config.from_dict(
            {"llm": {"preset": "fake"}, "language": {"source": "zh", "target": "en"}}
        )
        extractor = GlossaryExtractor(FakeClient(), cfg)
        req = extractor.prepare_extraction(
            store,
            "第1章 蓝斯的归来\n蓝斯点头。",
            "Chapter 1: Lance Returns\nLance nodded.",
        )
        assert [t.source for t in req.all_existing] == ["蓝斯"]
        assert [t.source for t in req.existing] == ["蓝斯"]
    finally:
        store.close()
