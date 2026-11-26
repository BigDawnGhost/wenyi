"""Scale and resume regressions for whole-book analysis and Book Bible caching.

Pins two invariants observed on a 1,479-chapter full-book run:
- ``analysis.style`` must bound the chapter-digest block to the synopsis/context budget
  instead of concatenating every digest into one request, while small digest sets render
  byte-for-byte as before so existing caches stay valid.
- The compiled Book Bible fingerprint must stay identical when a run resumes after some
  chapters (including cross-chapter repeated quotes) were already translated, so
  ``ensure_seeded`` hits its cache instead of recompiling and re-probing.
"""

from __future__ import annotations

import json
import re
from unittest.mock import Mock

import pytest
from wenyi_core.agents.analyzer import (
    Analyzer,
    _cap_digest,
    _representative_digest_block,
    _spread_line_indices,
)
from wenyi_core.config import Config
from wenyi_core.ingest.tokens import count_tokens
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.models.document import Chapter, Document, Segment
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.pipeline.prepare.preparation import PreparationService
from wenyi_core.pipeline.run.runtime import PipelineRuntime
from wenyi_core.pipeline.translate.quote_memory import QuoteMemory
from wenyi_core.storage.file import FileStorage

_DIGESTS_HEADER = "[Draft chapter digests: factual context, not naming authority]\n"


def test_small_digest_sets_render_byte_for_byte_as_before():
    agent = Analyzer(FakeClient(), Config())
    agent._ask_json = Mock(return_value={})
    digests = ["Opening digest.", "", "Closing digest."]
    agent.analyze("Sample prose.", chapter_digests=digests)
    user = agent._ask_json.call_args.args[1]
    # Books whose digests already fit keep the exact legacy rendering, empty entries included.
    assert user.endswith(
        "\n\n" + _DIGESTS_HEADER + "\n".join(f"[{i}] {digest}" for i, digest in enumerate(digests))
    )


def test_full_book_digests_stay_within_the_synopsis_token_budget():
    agent = Analyzer(FakeClient(), Config())
    agent._ask_json = Mock(return_value={})
    digests = [
        f"Chapter {i}: the pilgrim crosses ridge {i}, meets the river guard and moves on."
        for i in range(1479)
    ]
    unbounded = "\n".join(f"[{i}] {digest}" for i, digest in enumerate(digests))
    assert count_tokens(unbounded) > agent.config.pipeline.synopsis_reduce_tokens
    agent.analyze("Sample prose.", chapter_digests=digests)
    user = agent._ask_json.call_args.args[1]
    assert _DIGESTS_HEADER in user
    block = user.split(_DIGESTS_HEADER, 1)[1]
    assert count_tokens(block) <= agent.config.pipeline.synopsis_reduce_tokens
    labels = [int(label) for label in re.findall(r"^\[(\d+)\]", block, re.MULTILINE)]
    # Deterministic even spread: ascending labels anchored at the opening and closing chapters.
    assert labels == sorted(labels)
    assert labels[0] == 0 and labels[-1] == len(digests) - 1
    assert len(labels) < len(digests)
    # The same input always selects the same representative subset.
    twin = Analyzer(FakeClient(), Config())
    twin._ask_json = Mock(return_value={})
    twin.analyze("Sample prose.", chapter_digests=digests)
    assert twin._ask_json.call_args.args[1] == user


def test_single_oversized_digest_is_truncated_within_the_budget():
    cfg = Config.from_dict({"pipeline": {"synopsis_reduce_tokens": 300}})
    agent = Analyzer(FakeClient(), cfg)
    agent._ask_json = Mock(return_value={})
    huge = "The chronicler records yet another ridge crossing beyond the pass. " * 300
    assert count_tokens(huge) > 300
    agent.analyze("Sample prose.", chapter_digests=["", huge])
    user = agent._ask_json.call_args.args[1]
    assert _DIGESTS_HEADER in user
    block = user.split(_DIGESTS_HEADER, 1)[1]
    assert count_tokens(block) <= 300
    assert block.startswith("[1] The chronicler")


def test_analysis_digest_budget_is_clamped_to_a_small_context_window():
    cfg = Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {
                "preset": "fake",
                "models": {"fake/fake-small": {"context_window": 1400, "max_output_tokens": 200}},
                "routes": {"analysis.style": {"model": "fake/fake-small"}},
            },
        }
    )
    agent = Analyzer(FakeClient(), cfg)
    agent._ask_json = Mock(return_value={})
    digests = [
        f"Chapter {i} summary line about the long journey and its many stops." for i in range(120)
    ]
    agent.analyze("Short sample.", chapter_digests=digests)
    system, user = agent._ask_json.call_args.args
    assert _DIGESTS_HEADER in user
    block = user.split(_DIGESTS_HEADER, 1)[1]
    # Prompt plus output reserve fits the configured 1,400-token context window.
    assert count_tokens(system) + count_tokens(user) + 200 <= 1400
    labels = [int(label) for label in re.findall(r"^\[(\d+)\]", block, re.MULTILINE)]
    assert labels[0] == 0 and labels[-1] == len(digests) - 1


def test_cross_chapter_spans_include_canonical_for_resume_snapshots():
    quote = "山高月小，水落石出，此景难忘。"
    rendering = "The mountains are high and the moon is small."
    qm = QuoteMemory.from_chapters([(0, [f"「{quote}」"]), (1, [f"三藏再念道：「{quote}」"])])
    before = qm.cross_chapter_spans()
    assert before and quote in before[0]
    # Resume runs observe already-translated drafts first, which populates the canonical table.
    qm.observe((0, 0), f"「{quote}」", f"“{rendering}”")
    assert qm.cross_chapter_spans() == []
    # Whole-book snapshots for the Bible fingerprint must stay identical across resumes.
    assert qm.cross_chapter_spans(include_canonical=True) == before
    # Observed draft renderings stay authoritative over later pre-seeded quotes.
    qm.seed_canonical(before[0], "Wrong pre-seeded rendering.")
    refs = dict(qm.references_for_batch([f"三藏再念道：「{quote}」"]))
    assert "Wrong pre-seeded rendering." not in refs.values()
    assert f"“{rendering}”" in refs.values()


def test_bible_resume_reuses_cache_after_repeated_quote_chapter_interrupt(tmp_path, monkeypatch):
    quote = "山高月小，水落石出，此景难忘。"
    book_text = (
        f"第一章\n\n行者与三藏来到山谷。\n\n「{quote}」\n\n"
        f"第二章\n\n三藏再念道：「{quote}」\n\n行者点头。\n"
    )
    src = tmp_path / "resume_quotes.txt"
    src.write_text(book_text, encoding="utf-8")
    fail_second_chapter = True

    def handler(messages, operation, json_mode):
        sys_msg = messages[0]["content"]
        user_msg = messages[-1]["content"]
        if "pre-translation analyst" in sys_msg:
            return json.dumps({"style_guide": "Classical adventure style.", "characters": []})
        if "chapter digest writer" in sys_msg:
            return "行者 and 三藏 travel through the valley."
        if "whole-book synopsis writer" in sys_msg:
            return "A short journey of 行者 and 三藏."
        if "terminology prober" in sys_msg:
            keys = re.findall(r"^\d+\.\s+(.+?)\s+\|", user_msg, re.MULTILINE)
            return json.dumps({"renderings": {k: "" for k in keys}})
        if "Book Bible" in sys_msg:
            return json.dumps(
                {"terms": [{"source": "行者", "target": "Pilgrim", "type": "person"}]}
            )
        if "repeated" in sys_msg.lower() and "quote" in sys_msg.lower():
            return json.dumps({"quotes": {"0": "The mountains are high and the moon is small."}})
        if "literary translator" in sys_msg or "academic translator" in sys_msg:
            if fail_second_chapter and "行者点头" in user_msg:
                raise RuntimeError("simulated chapter 2 interrupt")
            nums = re.findall(r"^\[(\d+)\]", user_msg, re.MULTILINE)
            return json.dumps(
                {
                    "translations": [
                        (
                            '"The mountains are high and the moon is small."'
                            if f"[{n}]" in user_msg and quote in user_msg
                            else f"Translated paragraph {n}."
                        )
                        for n in nums
                    ]
                }
            )
        if "chapter title translator" in sys_msg:
            nums = re.findall(r"^\[(\d+)\]", user_msg, re.MULTILINE)
            return json.dumps({"titles": [f"Chapter {int(n) + 1}" for n in nums]})
        return json.dumps({})

    cfg = Config.from_dict(
        {
            "language": {"source": "zh", "target": "en"},
            "llm": {"preset": "fake"},
            "paths": {"state_dir": str(tmp_path / "state")},
            "pipeline": {
                "glossary_mode": "compiled",
                "polish": False,
                "review": False,
                "review_autofix": False,
                "chapter_concurrency": 1,
            },
        }
    )

    first = FakeClient(handler=handler)
    with pytest.raises(RuntimeError, match="simulated chapter 2 interrupt"):
        Orchestrator(cfg, client=first).run(str(src))
    # The interrupted first run already compiled (and paid for) the Book Bible.
    assert [c for c in first.calls if c["operation"] == "glossary.extract"]

    run_dir = next((tmp_path / "state").glob("*/targets/*"))
    store = FileStorage(str(run_dir), create=False)
    first_bible = store.read_artifact("glossary_bible.json")
    assert isinstance(first_bible, dict) and first_bible.get("fingerprint")

    # Resume: chapter 0 (with the repeated quote rendering) is already translated.
    spy_calls: list[dict] = []
    original = QuoteMemory.cross_chapter_spans

    def spy(self, *, max_items=15, include_canonical=False):
        spy_calls.append({"include_canonical": include_canonical, "max_items": max_items})
        return original(self, max_items=max_items, include_canonical=include_canonical)

    monkeypatch.setattr(QuoteMemory, "cross_chapter_spans", spy)
    fail_second_chapter = False
    resumed = FakeClient(handler=handler)
    Orchestrator(cfg, client=resumed).run(str(src))

    # The Book Bible cache hit: zero recompilation probes and the identical fingerprint.
    assert [c for c in resumed.calls if c["operation"] == "glossary.extract"] == []
    events = [event["event"] for event in store.list_events()]
    assert "glossary_bible_reused" in events
    assert store.read_artifact("glossary_bible.json")["fingerprint"] == first_bible["fingerprint"]
    # Whole-book snapshots request the canonical-inclusive span list on every run.
    assert spy_calls and all(call["include_canonical"] for call in spy_calls)
    for row in store.load_manifest()["chapters"]:
        assert all(seg.target for seg in store.load_chapter(row["index"]).text_segments)


def test_digest_sampler_and_cap_terminate_under_tiny_budgets():
    # Anchors fit (5 + 5 + 1 = 11 <= 11) while adding any interior item exceeds budget.
    assert _spread_line_indices([5, 5, 5, 5, 5], 11) == [0, 4]
    assert _spread_line_indices([9, 9, 9, 9, 9], 20) == [0, 4]
    # End-to-end tight budgets must terminate cleanly within budget.
    digests = ["The hero walks into the dark forest and meets a wolf there."] * 6
    for budget in (16, 20, 24, 40):
        block = _representative_digest_block(digests, budget)
        assert count_tokens(block) <= budget
    # Sub-character token cap (2-token CJK char with cap=1) terminates cleanly and skips empty.
    assert _cap_digest("龍测试", 1) == ""
    assert _representative_digest_block(["龍" * 20], 4) == ""


def test_preparation_analysis_cache_key_preserves_small_books_and_invalidates_clipped_books(
    tmp_path,
):
    small_doc = Document(
        title="Small",
        fmt="txt",
        source_lang="en",
        target_lang="zh",
        chapters=[Chapter(index=0, title="Ch 0", segments=[Segment(index=0, source="Sample.")])],
    )
    small_digests = ["Opening digest.", "Closing digest."]

    def _run_analysis(state_sub: str, digests: list[str], budget: int) -> tuple[FileStorage, int]:
        cfg = Config.from_dict(
            {
                "language": {"source": "en", "target": "zh"},
                "llm": {"preset": "fake"},
                "paths": {"state_dir": str(tmp_path / state_sub)},
                "pipeline": {"synopsis_reduce_tokens": budget},
            }
        )
        client = FakeClient()
        runtime = PipelineRuntime(cfg, client=client)
        prep = PreparationService(runtime)
        store = FileStorage(str(tmp_path / state_sub / "run"), create=True)
        prep._analyze_document(store, small_doc, digests, "src-hash")
        return store, sum(1 for c in client.calls if c["operation"] == "analysis.style")

    # Small book: changing synopsis_reduce_tokens keeps the exact same cache key (cache hit).
    _, calls_1 = _run_analysis("small", small_digests, 12000)
    _, calls_2 = _run_analysis("small", small_digests, 8000)
    assert calls_1 == 1 and calls_2 == 0

    # Large book exceeding budget: changing synopsis_reduce_tokens invalidates the cache key.
    big_digests = [
        f"Chapter {i}: pilgrim crosses ridge {i} and camps by the pass." for i in range(80)
    ]
    _, calls_3 = _run_analysis("big", big_digests, 400)
    _, calls_4 = _run_analysis("big", big_digests, 300)
    assert calls_3 == 1 and calls_4 == 1


# --- Book Bible checkpoint: interruptions after compile_bible must not lose paid results ------

_BIBLE_CORPUS = (
    "白石镇坐落在群山之间，镇上的居民世代以采石为生，清晨的雾气总是久久不散，"
    "石匠们背着工具沿着山路缓缓走向采石场，脚步声在空旷的山谷里回荡不息。\n"
    "「你又去白石了？」老石匠放下手中的凿子，抬头望着远方的山脊，语气里带着几分担忧，"
    "「那里的路越来越难走了，雨季一到，山上的碎石便会滚落下来。」\n"
    "多年以后，白石镇的采石场早已荒废，只有那条蜿蜒的山路还记得当年的喧闹，"
    "国主派来的使者站在镇口，望着空荡荡的街道，久久没有说话，只是轻轻叹了一口气。\n"
)
_BIBLE_QUOTES = ("山高月小，水落石出，此景难忘。",)


def _compile_bible_side_effect(digests, source_corpus, *, existing_terms=(), **_kwargs):
    """Mirror BookBibleAgent.compile_bible: sources already in the glossary are not re-mined."""
    from wenyi_core.agents.book_bible import BibleResult
    from wenyi_core.models.glossary import GlossaryTerm

    known = {t.source for t in existing_terms}
    if "白石" in known:
        return BibleResult(terms=(), variants_by_source={}, candidate_count=0, divergent_count=0)
    return BibleResult(
        terms=(GlossaryTerm("白石", "White Stone", type="place", first_chapter=0),),
        variants_by_source={"白石": ("White Stone", "White Rock")},
        candidate_count=3,
        divergent_count=1,
        conventions=("Render 国主 as 'the Sovereign'.",),
        divergent_titles=(("国主", ("the Sovereign", "the King")),),
    )


def _bible_store(path):
    from wenyi_core.models.glossary import GlossaryTerm

    store = FileStorage(str(path))
    manifest = store.stage_document(
        Document(
            source_lang="zh",
            target_lang="en",
            fmt="txt",
            chapters=[Chapter(index=0, segments=[Segment(index=0, source=_BIBLE_CORPUS)])],
        ),
        source_hash="b" * 64,
    )
    store.save_manifest(manifest)
    # A user-imported entry must survive and keep the fingerprint stable.
    store.upsert_term(GlossaryTerm("老石匠", "the Old Mason", type="person"))
    return store


def _bible_agent():
    agent = Mock()
    agent.compile_bible.side_effect = _compile_bible_side_effect
    agent.pretranslate_quotes.return_value = {_BIBLE_QUOTES[0]: "High peaks, a small moon."}
    agent.compile_voice_exemplars.return_value = (("白石镇坐落在群山之间。", "White Stone sits."),)
    return agent


def _seed(service, store):
    return service.ensure_seeded(
        store,
        digests=["白石 and 国主 appear."],
        source_corpus=_BIBLE_CORPUS,
        book_synopsis="Syn",
        style="S",
        repeated_quotes=_BIBLE_QUOTES,
    )


def _bible_outcome(service, variants, store):
    bible = store.read_artifact("glossary_bible.json")
    return {
        "variants": variants,
        "conventions": service.last_conventions,
        "divergent_titles": service.last_divergent_titles,
        "quote_targets": service.last_quote_targets,
        "voice_exemplars": service.last_voice_exemplars,
        "artifact": {k: v for k, v in bible.items() if k != "fingerprint"},
        "terms": sorted((t.source, t.target, t.first_chapter) for t in store.all_terms()),
    }


@pytest.mark.parametrize("failing_step", ["pretranslate_quotes", "compile_voice_exemplars"])
def test_bible_resume_after_post_compile_interrupt_matches_uninterrupted_run(
    tmp_path, failing_step
):
    from wenyi_core.pipeline.translate.bible_seed import BibleSeedService

    cfg = Config.from_dict(
        {"llm": {"preset": "fake"}, "language": {"source": "zh", "target": "en"}}
    )

    clean_store = _bible_store(tmp_path / "clean")
    try:
        clean_service = BibleSeedService(_bible_agent(), cfg)
        expected = _bible_outcome(clean_service, _seed(clean_service, clean_store), clean_store)
    finally:
        clean_store.close()
    assert expected["variants"] == {"白石": ("White Stone", "White Rock")}

    store = _bible_store(tmp_path / "resumed")
    try:
        agent = _bible_agent()
        step = getattr(agent, failing_step)
        paid_result = step.return_value
        step.side_effect = RuntimeError(f"simulated {failing_step} interrupt")
        with pytest.raises(RuntimeError, match="simulated"):
            _seed(BibleSeedService(agent, cfg), store)
        assert store.read_artifact("glossary_bible.json") is None
        assert store.read_artifact("glossary_bible.checkpoint.json")["variants_by_source"]
        quote_calls = agent.pretranslate_quotes.call_count

        # Resume in a fresh service, as a new process would.
        step.side_effect = None
        step.return_value = paid_result
        service = BibleSeedService(agent, cfg)
        resumed = _bible_outcome(service, _seed(service, store), store)
        assert resumed == expected
        assert agent.compile_bible.call_count == 1
        assert store.read_artifact("glossary_bible.checkpoint.json") is None
        if failing_step == "compile_voice_exemplars":
            # Quotes were already paid for before the voice call failed.
            assert agent.pretranslate_quotes.call_count == quote_calls == 1
        assert store.get_term("老石匠").first_chapter is None
        assert store.get_term("老石匠").target == "the Old Mason"

        # A completed Bible is reused without any further model call.
        again = BibleSeedService(agent, cfg)
        assert _bible_outcome(again, _seed(again, store), store) == expected
        assert agent.compile_bible.call_count == 1
        events = [event["event"] for event in store.list_events()]
        assert events.count("glossary_bible_compiled") == 1
        assert "glossary_bible_reused" in events
    finally:
        store.close()


def _bible_cfg():
    return Config.from_dict(
        {"llm": {"preset": "fake"}, "language": {"source": "zh", "target": "en"}}
    )


def _interrupt_voice(store):
    from wenyi_core.pipeline.translate.bible_seed import BibleSeedService

    agent = _bible_agent()
    agent.compile_voice_exemplars.side_effect = RuntimeError("simulated voice interrupt")
    with pytest.raises(RuntimeError, match="simulated"):
        _seed(BibleSeedService(agent, _bible_cfg()), store)
    return agent


def test_unfinished_bible_never_reaches_the_glossary(tmp_path):
    store = _bible_store(tmp_path / "s")
    try:
        _interrupt_voice(store)
        assert store.get_term("白石") is None
    finally:
        store.close()


def test_changed_inputs_after_interrupt_recompile_with_the_same_glossary(tmp_path):
    from wenyi_core.models.glossary import GlossaryTerm
    from wenyi_core.pipeline.translate.bible_seed import BibleSeedService

    store = _bible_store(tmp_path / "s")
    try:
        _interrupt_voice(store)
        # The user adds an unrelated entry, so the stale checkpoint no longer applies.
        store.upsert_term(GlossaryTerm("采石场", "the quarry", type="place"))
        agent = _bible_agent()
        variants = _seed(BibleSeedService(agent, _bible_cfg()), store)
        assert agent.compile_bible.call_count == 1
        assert variants == {"白石": ("White Stone", "White Rock")}
        assert store.get_term("白石").target == "White Stone"
    finally:
        store.close()


def test_bible_publication_finishes_after_a_stop_past_the_artifact(tmp_path, monkeypatch):
    from wenyi_core.pipeline.translate.bible_seed import BibleSeedService

    store = _bible_store(tmp_path / "s")
    try:
        monkeypatch.setattr(
            BibleSeedService,
            "_publish_seeds",
            staticmethod(lambda store, seeds: (_ for _ in ()).throw(RuntimeError("stop"))),
        )
        with pytest.raises(RuntimeError, match="stop"):
            _seed(BibleSeedService(_bible_agent(), _bible_cfg()), store)
        assert store.read_artifact("glossary_bible.json") is not None
        assert store.get_term("白石") is None
        monkeypatch.undo()

        agent = _bible_agent()
        _seed(BibleSeedService(agent, _bible_cfg()), store)
        agent.compile_bible.assert_not_called()
        assert store.get_term("白石").target == "White Stone"
        assert store.read_artifact("glossary_bible.checkpoint.json") is None
    finally:
        store.close()


def test_resumed_publication_records_a_navigation_conflict_once(tmp_path):
    from wenyi_core.models.glossary import TYPE_NAVIGATION, GlossaryTerm
    from wenyi_core.pipeline.translate.bible_seed import BibleSeedService

    store = _bible_store(tmp_path / "s")
    try:
        store.upsert_term(GlossaryTerm("白石", "The White Rock", type=TYPE_NAVIGATION))
        service = BibleSeedService(_bible_agent(), _bible_cfg())
        _seed(service, store)
        first = len(store.open_conflicts())
        assert first == 1
        # Repeating publication, as a resume after a stop would, adds nothing.
        BibleSeedService._publish_seeds(store, [GlossaryTerm("白石", "White Stone", type="place")])
        assert len(store.open_conflicts()) == first
    finally:
        store.close()


def test_bible_records_usage_before_each_checkpoint(tmp_path):
    from wenyi_core.pipeline.translate.bible_seed import BibleSeedService

    store = _bible_store(tmp_path / "s")
    try:
        order: list[str] = []
        original_write = store.write_artifact

        def write(key, value):
            order.append(f"write:{key}")
            return original_write(key, value)

        store.write_artifact = write
        BibleSeedService(_bible_agent(), _bible_cfg()).ensure_seeded(
            store,
            digests=["白石 and 国主 appear."],
            source_corpus=_BIBLE_CORPUS,
            book_synopsis="Syn",
            style="S",
            repeated_quotes=_BIBLE_QUOTES,
            checkpoint_usage=lambda: order.append("usage"),
        )
        checkpoint_writes = [i for i, e in enumerate(order) if e.endswith("checkpoint.json")]
        assert len(checkpoint_writes) == 2
        assert all(order[i - 1] == "usage" for i in checkpoint_writes)
    finally:
        store.close()


def test_completed_bible_reuse_keeps_later_glossary_edits(tmp_path):
    from wenyi_core.models.glossary import TYPE_NAVIGATION, GlossaryTerm
    from wenyi_core.pipeline.translate.bible_seed import BibleSeedService

    store = _bible_store(tmp_path / "s")
    try:
        store.upsert_term(GlossaryTerm("国主", "The Sovereign", type=TYPE_NAVIGATION))
        _seed(BibleSeedService(_bible_agent(), _bible_cfg()), store)
        assert store.get_term("白石") is not None
        # The user deletes a seeded entry after the Bible completed.
        store.delete_term("白石")
        _seed(BibleSeedService(_bible_agent(), _bible_cfg()), store)
        assert store.get_term("白石") is None
    finally:
        store.close()


def test_completed_bible_reuse_does_not_reopen_a_resolved_navigation_conflict(tmp_path):
    from wenyi_core.models.glossary import TYPE_NAVIGATION, GlossaryTerm
    from wenyi_core.pipeline.translate.bible_seed import BibleSeedService

    store = _bible_store(tmp_path / "s")
    try:
        store.upsert_term(GlossaryTerm("白石", "The White Rock", type=TYPE_NAVIGATION))
        _seed(BibleSeedService(_bible_agent(), _bible_cfg()), store)
        assert len(store.open_conflicts()) == 1
        store.mark_conflicts_resolved("白石")
        _seed(BibleSeedService(_bible_agent(), _bible_cfg()), store)
        assert store.open_conflicts() == []
    finally:
        store.close()


def test_bible_prompts_never_see_a_seed_that_only_opens_a_conflict(tmp_path):
    from wenyi_core.models.glossary import TYPE_NAVIGATION, GlossaryTerm
    from wenyi_core.pipeline.translate.bible_seed import BibleSeedService

    store = _bible_store(tmp_path / "s")
    try:
        store.upsert_term(GlossaryTerm("白石", "The White Rock", type=TYPE_NAVIGATION))
        agent = _bible_agent()
        _seed(BibleSeedService(agent, _bible_cfg()), store)
        for call in (agent.pretranslate_quotes, agent.compile_voice_exemplars):
            prompt_terms = call.call_args.kwargs["terms"]
            assert all(t.source != "白石" for t in prompt_terms)
    finally:
        store.close()


def test_bible_records_usage_before_the_final_artifact(tmp_path):
    from wenyi_core.pipeline.translate.bible_seed import BibleSeedService

    store = _bible_store(tmp_path / "s")
    try:
        order: list[str] = []
        original_write = store.write_artifact

        def write(key, value):
            order.append(f"write:{key}")
            return original_write(key, value)

        store.write_artifact = write
        BibleSeedService(_bible_agent(), _bible_cfg()).ensure_seeded(
            store,
            digests=["白石 and 国主 appear."],
            source_corpus=_BIBLE_CORPUS,
            book_synopsis="Syn",
            style="S",
            repeated_quotes=_BIBLE_QUOTES,
            checkpoint_usage=lambda: order.append("usage"),
        )
        final = order.index("write:glossary_bible.json")
        assert order[final - 1] == "usage"
    finally:
        store.close()


def _seed_with_style(service, store, style):
    return service.ensure_seeded(
        store,
        digests=["白石 and 国主 appear."],
        source_corpus=_BIBLE_CORPUS,
        book_synopsis="Syn",
        style=style,
        repeated_quotes=_BIBLE_QUOTES,
    )


def test_checkpoint_for_other_inputs_never_replays_a_finished_publication(tmp_path):
    from wenyi_core.pipeline.translate.bible_seed import BibleSeedService

    store = _bible_store(tmp_path / "s")
    try:
        _seed_with_style(BibleSeedService(_bible_agent(), _bible_cfg()), store, "S")
        store.delete_term("白石")
        # A run with other inputs stops during its voice pass, leaving its own checkpoint.
        agent = _bible_agent()
        agent.compile_voice_exemplars.side_effect = RuntimeError("simulated voice interrupt")
        with pytest.raises(RuntimeError, match="simulated"):
            _seed_with_style(BibleSeedService(agent, _bible_cfg()), store, "S2")
        checkpoint = store.read_artifact("glossary_bible.checkpoint.json")
        assert checkpoint is not None
        # Back on the original inputs, the finished Bible is reused as it was.
        _seed_with_style(BibleSeedService(_bible_agent(), _bible_cfg()), store, "S")
        assert store.get_term("白石") is None
        assert store.read_artifact("glossary_bible.checkpoint.json") == checkpoint
    finally:
        store.close()


def test_publication_matches_entries_by_normalized_key(tmp_path):
    from wenyi_core.models.glossary import GlossaryTerm
    from wenyi_core.pipeline.translate.bible_seed import BibleSeedService

    store = _bible_store(tmp_path / "s")
    try:
        store.upsert_term(GlossaryTerm("Ａｌｉｃｅ", "Alice", type="person"))
        BibleSeedService._publish_seeds(
            store, [GlossaryTerm("Alice", "Alice", type="person", first_chapter=0)]
        )
        assert store.get_term("Alice") is None
    finally:
        store.close()


def test_prompts_match_navigation_stubs_by_exact_source(tmp_path):
    from wenyi_core.agents.book_bible import BibleResult
    from wenyi_core.models.glossary import TYPE_NAVIGATION, GlossaryTerm
    from wenyi_core.pipeline.translate.bible_seed import BibleSeedService

    store = _bible_store(tmp_path / "s")
    try:
        store.upsert_term(GlossaryTerm("Ａｌｉｃｅ", "Alyce", type=TYPE_NAVIGATION))
        agent = _bible_agent()
        agent.compile_bible.side_effect = lambda *a, **k: BibleResult(
            terms=(GlossaryTerm("Alice", "Alice", type="person", first_chapter=0),),
            variants_by_source={},
            candidate_count=1,
            divergent_count=0,
        )
        _seed(BibleSeedService(agent, _bible_cfg()), store)
        # Publication inserts "Alice" beside the differently written stub, so prompts see it.
        assert store.get_term("Alice") is not None
        prompt_terms = agent.pretranslate_quotes.call_args.kwargs["terms"]
        assert any(t.source == "Alice" for t in prompt_terms)
    finally:
        store.close()


def test_saved_quotes_are_rerendered_when_the_prompt_glossary_changed(tmp_path):
    from wenyi_core.models.glossary import TYPE_NAVIGATION, GlossaryTerm
    from wenyi_core.pipeline.translate.bible_seed import BibleSeedService

    store = _bible_store(tmp_path / "s")
    try:
        store.upsert_term(GlossaryTerm("白石", "The White Rock", type=TYPE_NAVIGATION))
        agent = _interrupt_voice(store)
        assert agent.pretranslate_quotes.call_count == 1
        # The navigation target now agrees with the seed, so prompts will see the seed.
        store.delete_term("白石")
        store.upsert_term(GlossaryTerm("白石", "White Stone", type=TYPE_NAVIGATION))
        resumed = _bible_agent()
        _seed(BibleSeedService(resumed, _bible_cfg()), store)
        resumed.compile_bible.assert_not_called()
        assert resumed.pretranslate_quotes.call_count == 1
    finally:
        store.close()
