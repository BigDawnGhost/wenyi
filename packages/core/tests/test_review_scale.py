"""Review scale behaviors: navigation scoping, prompt shrinking and evidence parity."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from wenyi_core.agents.reviewer import Reviewer, ReviewOutputError
from wenyi_core.config import Config
from wenyi_core.glossary.notes import Fact
from wenyi_core.glossary.store import (
    normalize_match_text,
    source_matches_text,
    term_match_sources,
)
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.models.document import Chapter, Document, Segment
from wenyi_core.models.glossary import TYPE_NAVIGATION, TYPE_TERM, GlossaryTerm
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.pipeline.review.review_chunks import ReviewChunkService
from wenyi_core.review.evidence import BookEvidenceIndex
from wenyi_core.storage.file import FileStorage

from tests.review_fixtures import _chapter


def _review_config(models: dict | None = None, routes: dict | None = None) -> Config:
    llm: dict = {"preset": "fake"}
    if models is not None:
        llm["models"] = models
    if routes is not None:
        llm["routes"] = routes
    return Config.from_dict({"llm": llm, "language": {"source": "en", "target": "zh"}})


def _review_answer(alerts: list | None = None) -> str:
    payload: dict = {"issues": [], "reviewed_segments": 2, "complete": True}
    if alerts is not None:
        payload["glossary_alerts"] = alerts
    return json.dumps(payload, ensure_ascii=False)


def test_review_prompt_scopes_navigation_terms_to_chunk_sources():
    """Matching navigation titles stay; unrelated ones and only those are dropped."""
    terms = [
        GlossaryTerm("Section 1", "第一节", type=TYPE_NAVIGATION),
        GlossaryTerm("Chapter 99: Unrelated", "无关的第九十九节", type=TYPE_NAVIGATION),
        GlossaryTerm("May", "梅", type="person"),
        GlossaryTerm("Ann", "安", type="person"),
    ]
    prompts = []

    def handler(messages, tier, json_mode):
        prompts.append(messages[-1]["content"])
        return _review_answer()

    result = Reviewer(FakeClient(handler=handler), _review_config()).review_result(
        ["Section 1 begins.", "May arrived."], ["第一节开始了。", "梅到了。"], terms
    )

    assert result.issues == []
    user = prompts[0]
    assert "Section 1 → 第一节" in user
    assert "Chapter 99: Unrelated" not in user
    # Non-navigation terms are never scoped away while within budget.
    assert "May → 梅" in user
    assert "Ann → 安" in user


def test_review_prompt_shrinks_oversized_glossary_and_keeps_note_alignment():
    """A tight context window drops unmatched overflow while note_ids follow the final prompt."""
    config = _review_config(
        models={"fake/fake-review": {"context_window": 1500, "max_output_tokens": 200}},
        routes={"review.scan": {"model": "fake/fake-review"}},
    )
    alpha_fact = Fact(
        text="In court, address Alpha as Judge Alpha.",
        when=["court"],
        refs=["c0:s0:0-10"],
    )
    beta_fact = Fact(
        text="In court, address Beta as Judge Beta.",
        when=["court"],
        refs=["c0:s1:0-10"],
    )
    terms = [
        # Matches the chunk but is far too large to fit; must be dropped by shrinking.
        GlossaryTerm("Alpha", "甲" * 3000, note=alpha_fact.line()),
        GlossaryTerm("Beta", "贝塔", note=beta_fact.line()),
        *[GlossaryTerm(f"Filler{i:03d}", f"填充{i:03d}") for i in range(40)],
    ]
    prompts = []

    def handler(messages, tier, json_mode):
        prompts.append(messages[-1]["content"])
        return _review_answer([{"index": 1, "note_id": 0, "detail": "法庭称谓需核对。"}])

    result = Reviewer(FakeClient(handler=handler), config).review_result(
        ["Alpha arrived in court.", "Beta spoke in court."],
        ["阿尔法出庭了。", "贝塔在法庭上发言。"],
        terms,
    )

    user = prompts[0]
    assert "Beta → 贝塔" in user
    assert "- Alpha →" not in user
    assert "Judge Alpha" not in user
    assert "Filler020" not in user
    # The kept term's note check is the only note row the model can cite.
    assert "Judge Beta" in user
    assert '"applies_to": [1]' in user
    assert result.glossary_alerts == [
        {
            "index": 1,
            "term": "Beta",
            "stored_target": "贝塔",
            "note": "In court, address Beta as Judge Beta.",
            "detail": "法庭称谓需核对。",
        }
    ]


def test_review_raises_actionable_error_when_payload_alone_exceeds_context_window():
    config = _review_config(
        models={"fake/fake-tiny": {"context_window": 60, "max_output_tokens": 20}},
        routes={"review.scan": {"model": "fake/fake-tiny"}},
    )
    reviewer = Reviewer(FakeClient(handler=lambda *_args: "{}"), config)

    with pytest.raises(ValueError, match="even after shrinking the glossary"):
        reviewer.review_result(
            ["A fairly long review paragraph that cannot fit a tiny window."],
            ["一段完全放不进极小窗口的审校文本。"],
            [GlossaryTerm("Ann", "安")],
        )


def _parity_index() -> BookEvidenceIndex:
    chapters = [
        _chapter(
            0,
            [
                ("Ann arrived.", "安到了。"),
                ("Anna left the city.", "安娜离开了城市。"),
                ("长安城的清晨。", "长安的清晨。"),
                ("CAFÉ Olivia ordered éclair.", "咖啡欧丽维亚点了闪电泡芙。"),
                ("Ann and Annie spoke.", "安和安妮谈过。"),
                ("A side trail begins.", "一条支路开始了。"),
            ],
        ),
        _chapter(1, [("ＡＮＮ returned.", "安回来了。"), ("End.", "结束。")]),
    ]
    terms = [
        GlossaryTerm("Ann", "安", aliases=["Annie"], type="person"),
        GlossaryTerm("长安", "长安"),
        GlossaryTerm("Café", "咖啡店"),
    ]
    return BookEvidenceIndex(chapters, terms, {})


def _expected_matches(index: BookEvidenceIndex, keys: list[str]):
    """The pre-change behavior: match every key against raw segment sources."""
    return tuple(
        segment
        for segment in index.segments
        if any(source_matches_text(key, segment.source) for key in keys)
    )


def _keys_for(index: BookEvidenceIndex, query: str) -> list[str]:
    term, _ambiguous = index.canonical_term(query)
    return term_match_sources(term) if term is not None else [query.strip()]


@pytest.mark.parametrize(
    "query",
    ["Ann", "Annie", "长安", "Café", "trail", "city", "Anna"],
)
def test_term_occurrences_match_source_matches_text_exactly(query):
    index = _parity_index()
    assert index._normalized_sources is None  # Normalization is lazy, not eager.

    canonical, occurrences, ambiguous = index._occurrences(query)

    assert ambiguous == []
    assert canonical
    assert occurrences == _expected_matches(index, _keys_for(index, query))
    result = index.term_occurrences({"term": query, "selectors": ["first"], "context_radius": 0})
    assert result["ok"] is True
    assert result["total_matches"] == len(occurrences)
    # The cache normalized every source exactly once, with identical results.
    assert index._normalized_sources == tuple(
        normalize_match_text(segment.source) for segment in index.segments
    )


def test_concurrent_queries_on_one_index_still_match_exactly():
    queries = ["Ann", "Annie", "长安", "Café", "trail", "city", "Anna"]
    baseline = _parity_index()
    sequential = {query: baseline._occurrences(query) for query in queries}

    # Fresh cold-start index so every worker races into _get_normalized_sources() and
    # _occurrence_cache simultaneously rather than hitting a pre-warmed cache.
    index = _parity_index()
    assert index._normalized_sources is None
    barrier = threading.Barrier(len(queries))

    def run(query):
        barrier.wait()
        canonical, occurrences, ambiguous = index._occurrences(query)
        public = index.term_occurrences(
            {"term": query, "selectors": ["first", "middle", "last"], "context_radius": 0}
        )
        return canonical, occurrences, ambiguous, public

    with ThreadPoolExecutor(max_workers=len(queries)) as pool:
        concurrent = dict(zip(queries, pool.map(run, queries)))

    assert index._normalized_sources is not None
    for query in queries:
        canonical, occurrences, ambiguous, public = concurrent[query]
        assert (canonical, occurrences, ambiguous) == sequential[query]
        assert public["ok"] is True
        assert public["total_matches"] == len(occurrences)
        assert public["total_matches"] == len(_expected_matches(index, _keys_for(index, query)))


def test_review_loop_exhaust_fallback_clears_notes_for_empty_glossary(monkeypatch):
    """When preflight exhausts its 4 tighten iterations and falls back to the base render,
    last_render holds the final candidate while user is the base render; reviewer must
    recompute notes for effective_terms=[] and reject note_id=0.
    """
    from wenyi_core.agents import reviewer as reviewer_mod
    from wenyi_core.ingest.tokens import count_tokens
    from wenyi_core.llm import preflight as preflight_mod

    exact_note = "\n".join(
        Fact(
            text=f"Rule {i}: confirm register and speaker seniority carefully. " + "detail " * 25,
            when=["Alpha"],
            refs=[f"c0:s0:{i}-{i + 5}"],
        ).line()
        for i in range(4)
    )
    terms = [GlossaryTerm("Alpha", "阿尔法", type="person", note=exact_note)]
    sources = ["Alpha spoke softly."]
    targets = ["阿尔法轻声说道。"]

    probe_prompts: list[str] = []

    def _probe(messages, tier, json_mode):
        probe_prompts.append(messages[0]["content"] + "\n" + messages[1]["content"])
        return json.dumps({"issues": [], "reviewed_segments": 1, "complete": True})

    Reviewer(FakeClient(handler=_probe), _review_config()).review_result(sources, targets, [])
    base_tokens = count_tokens(probe_prompts[0])
    Reviewer(FakeClient(handler=_probe), _review_config()).review_result(sources, targets, terms)
    noted_overhead = count_tokens(probe_prompts[1]) - base_tokens
    assert noted_overhead > 100

    # Keep glossary_room positive yet strictly below Alpha+note, and have fit_terms_to_budget
    # keep returning [Alpha] so all 4 tighten iterations overflow and execute preflight's
    # for...else fallback (returning base_user while last_render still holds Alpha's render).
    fit_calls = 0

    def _always_pick_alpha(cands, text, *, max_tokens):
        nonlocal fit_calls
        fit_calls += 1
        return list(cands)

    monkeypatch.setattr(preflight_mod, "fit_terms_to_budget", _always_pick_alpha)
    note_check_arg_counts: list[int] = []
    real_note_checks = reviewer_mod._note_checks

    def _spy_note_checks(t, s):
        note_check_arg_counts.append(len(t))
        return real_note_checks(t, s)

    monkeypatch.setattr(reviewer_mod, "_note_checks", _spy_note_checks)

    tight_window = base_tokens + 40 + 50
    cfg = _review_config(
        models={"fake/default": {"context_window": tight_window, "max_output_tokens": 50}},
        routes={"review.scan": {"model": "fake/default"}},
    )
    seen_prompts: list[str] = []

    def _bogus_alert(messages, tier, json_mode):
        seen_prompts.append(messages[-1]["content"])
        return json.dumps(
            {
                "issues": [],
                "reviewed_segments": 1,
                "complete": True,
                "glossary_alerts": [{"index": 0, "note_id": 0, "detail": "hallucinated"}],
            }
        )

    with pytest.raises(ReviewOutputError, match="invalid_glossary_alert"):
        Reviewer(FakeClient(handler=_bogus_alert), cfg).review_result(sources, targets, terms)
    # 4 shrink iterations exhausted -> preflight fell back to base_user, and reviewer's else
    # branch recomputed _note_checks([], sources) as the final call (len == 0 after 4 len == 1).
    assert fit_calls == 4
    assert note_check_arg_counts[-1] == 0 and note_check_arg_counts[-2] == 1
    assert seen_prompts and "Paragraph-specific glossary checks" not in seen_prompts[0]


def test_review_multi_paragraph_overflow_bisects_automatically():
    """Multi-paragraph context overflow raises ReviewOutputError('context_overflow') so
    ReviewChunkService bisects the chunk; a single oversized paragraph still raises ValueError.
    """
    from wenyi_core.ingest.tokens import count_tokens

    p1 = "word " * 140
    p2 = "text " * 140
    probe: list[str] = []

    def _probe(messages, tier, json_mode):
        probe.append(messages[0]["content"] + "\n" + messages[1]["content"])
        return json.dumps({"issues": [], "reviewed_segments": 1, "complete": True})

    Reviewer(FakeClient(handler=_probe), _review_config()).review_result([p1], [p1], [])
    one_para_tokens = count_tokens(probe[0])

    # Window comfortably fits 1 paragraph (+ 50 output reserve) but overflows 2 paragraphs.
    cfg = _review_config(
        models={"fake/default": {"context_window": one_para_tokens + 120, "max_output_tokens": 50}},
        routes={"review.scan": {"model": "fake/default"}},
    )
    seen_counts: list[int] = []

    def handler(messages, tier, json_mode):
        user = messages[-1]["content"]
        n = user.count("Source:")
        seen_counts.append(n)
        return json.dumps({"issues": [], "reviewed_segments": n, "complete": True})

    client = FakeClient(handler=handler)
    reviewer = Reviewer(client, cfg)
    with pytest.raises(ReviewOutputError, match="context_overflow"):
        reviewer.review_result([p1, p2], [p1, p2], [])

    segs = [
        Segment(index=0, source=p1, target=p1),
        Segment(index=1, source=p2, target=p2),
    ]
    chunk_service = ReviewChunkService(cfg, client, reviewer)
    issues = chunk_service.review_chapter(segs, [], chapter_index=0)
    assert issues == []
    assert seen_counts == [1, 1]

    # A singleton that still overflows surfaces the actionable ValueError directly.
    tiny_cfg = _review_config(
        models={"fake/default": {"context_window": 60, "max_output_tokens": 10}},
        routes={"review.scan": {"model": "fake/default"}},
    )
    with pytest.raises(ValueError, match="exceeds context_window"):
        Reviewer(FakeClient(), tiny_cfg).review_result([p1], [p1], [])


def test_review_legacy_epub_nav_titles_scoped_in_memory_and_preserve_resume_fingerprint(tmp_path):
    """Existing EPUB projects that stored chapter titles as plain type='term' keep their
    persisted glossary fingerprint for skip/resume while ReviewService.run_session classifies
    exact unannotated nav pairs as TYPE_NAVIGATION in-memory without mutating glossary.db.
    """
    from wenyi_core.storage.run_store import STATUS_DONE

    store = FileStorage(str(tmp_path / "state"))
    try:
        manifest = store.stage_document(
            Document(
                title="Long Novel",
                source_lang="zh",
                target_lang="en",
                fmt="epub",
                chapters=[
                    Chapter(
                        index=0,
                        title="第1章 启程",
                        segments=[
                            Segment(index=0, source="第1章 启程", target="Chapter 1: Departure"),
                            Segment(index=1, source="蓝斯出发了。", target="Lance set out."),
                        ],
                    )
                ],
                meta={
                    "epub_template": True,
                    "toc_entries": [
                        {
                            "entry_id": "nav:0",
                            "title": "第1章 启程",
                            "title_translated": "Chapter 1: Departure",
                        },
                        {
                            "entry_id": "nav:1",
                            "title": "第2章 重逢",
                            "title_translated": "Chapter 2: Reunion",
                        },
                        {
                            "entry_id": "nav:2",
                            "title": "铜拱门",
                            "title_translated": "The Copper Arch",
                        },
                    ],
                },
            ),
            source_hash="b" * 64,
        )
        manifest["chapters"][0]["status"] = STATUS_DONE
        store.save_manifest(manifest)
        # Legacy auto-inserted navigation titles (type='term', no metadata, first_chapter=None).
        store.upsert_term(GlossaryTerm("第1章 启程", "Chapter 1: Departure", type=TYPE_TERM))
        store.upsert_term(GlossaryTerm("第2章 重逢", "Chapter 2: Reunion", type=TYPE_TERM))
        # User-annotated entity sharing a chapter title name must stay TYPE_TERM.
        store.upsert_term(
            GlossaryTerm("铜拱门", "The Copper Arch", type=TYPE_TERM, note="Landmark in ch 1")
        )
        # Ordinary extracted entity must stay person.
        store.upsert_term(GlossaryTerm("蓝斯", "Lance", type="person"), chapter=0)

        cfg = Config.from_dict(
            {
                "llm": {"preset": "fake"},
                "language": {"source": "zh", "target": "en"},
                "pipeline": {
                    "review_agent_loop": False,
                    "review_conflict_arbitration": False,
                    "review_fix_loop": False,
                    "review_concurrency": 1,
                },
            }
        )
        prompts_seen: list[str] = []

        def _handler(messages, tier, json_mode):
            prompts_seen.append(messages[-1]["content"])
            return json.dumps({"issues": [], "reviewed_segments": 2, "complete": True})

        client = FakeClient(handler=_handler)
        review_svc = Orchestrator(cfg, client)._review

        # session_terms stays raw so persisted legacy review fingerprints keep matching.
        raw_session = review_svc.session_terms(store)
        assert {t.source: t.type for t in raw_session} == {
            "第1章 启程": TYPE_TERM,
            "第2章 重逢": TYPE_TERM,
            "铜拱门": TYPE_TERM,
            "蓝斯": "person",
        }
        # In-memory classification tags only unannotated legacy nav pairs.
        classified = review_svc._classify_legacy_navigation_terms(
            store.load_manifest(), raw_session
        )
        assert {t.source: t.type for t in classified} == {
            "第1章 启程": TYPE_NAVIGATION,
            "第2章 重逢": TYPE_NAVIGATION,
            "铜拱门": TYPE_TERM,
            "蓝斯": "person",
        }

        # Run 1 scans the chapter: matching ch 1 nav title + entities appear; unrelated ch 2 title omitted.
        review_svc.run_session(store, raw_session)
        assert len(prompts_seen) == 1
        assert "第1章 启程 → Chapter 1: Departure" in prompts_seen[0]
        assert "第2章 重逢" not in prompts_seen[0]
        assert "铜拱门 → The Copper Arch" in prompts_seen[0]
        assert "蓝斯 → Lance" in prompts_seen[0]

        # Stored DB rows remain untouched, and a second run skips via the preserved legacy fingerprint.
        assert store.get_term("第1章 启程").type == TYPE_TERM
        assert store.get_term("第2章 重逢").type == TYPE_TERM
        review_svc.run_session(store, review_svc.session_terms(store))
        assert len(prompts_seen) == 1
    finally:
        store.close()
