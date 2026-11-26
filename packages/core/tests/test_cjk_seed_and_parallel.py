"""Tests for lean CJK upfront glossary seeding and parallel chapter execution."""

from __future__ import annotations

import contextvars
import json
import os
import threading
import time

import pytest
from wenyi_core.agents.cjk_seed import (
    CjkGlossarySeeder,
    SeedCandidate,
    _count_candidates_in_corpus,
    mine_cjk_candidates,
)
from wenyi_core.agents.translator import Translator
from wenyi_core.config import Config
from wenyi_core.glossary.prompt_scope import (
    estimate_term_tokens,
    fit_terms_to_budget,
    split_glossary_tiers,
    split_trimmed_glossary_tiers,
)
from wenyi_core.llm.limits import RequestCancelled
from wenyi_core.llm.retrying import ProviderRequestError
from wenyi_core.models.glossary import TYPE_NAVIGATION, GlossaryTerm
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.pipeline.translate.parallel_chapters import ParallelChapterRunner
from wenyi_core.pipeline.translate.seed_service import SEED_ARTIFACT_KEY
from wenyi_core.pipeline.translate.translation_batch import POLISH_FAILED
from wenyi_core.storage.run_store import STATUS_DONE

from tests.fake_llm import FakeClient, MeteredFakeClient, routing_handler
from tests.pipeline_fixtures import fake_pipeline_config, require_file_storage


def test_mine_cjk_candidates_filters_noise_and_falls_back_without_digests():
    corpus = (
        "青云宗的林尘修炼《太虚剑诀》，拜见大人。林尘再次前往青云宗施展《太虚剑诀》。"
        "孤独自离开。偶尔路过一次。"
    )
    digests = ["本章林尘在青云宗修炼《太虚剑诀》，并与大人交谈，孤也出场。"]
    candidates = mine_cjk_candidates(
        digests,
        corpus,
        existing_sources={"青云宗"},
        context_dependent_sources={"大人", "孤"},
    )
    sources = [c.source for c in candidates]
    assert "太虚剑诀" in sources
    assert "林尘" in sources
    assert "青云宗" not in sources
    assert "大人" not in sources
    assert "孤" not in sources
    # Shadowed substring inside "太虚剑诀" should be suppressed when >=80% shadowed.
    assert "太虚" not in sources

    # Without digests, bounded corpus sampling still discovers recurring CJK entities.
    fallback = mine_cjk_candidates([], corpus, context_dependent_sources={"大人", "孤"})
    fallback_sources = [c.source for c in fallback]
    assert "青云宗" in fallback_sources
    assert "林尘" in fallback_sources


def test_cjk_seeder_preserves_person_named_grace_and_caches_system_prefix():
    systems: list[str] = []

    def handler(messages, tier, json_mode):
        systems.append(messages[0]["content"])
        return json.dumps(
            {
                "terms": [
                    {"source": "格蕾丝", "target": "Grace", "type": "person", "gender": "female"},
                    {"source": "长老称呼", "target": "Elder", "type": "term"},
                    {"source": "青云宗", "target": "Azure Cloud Sect", "type": "organization"},
                ]
            },
            ensure_ascii=False,
        )

    cfg = Config.from_dict(
        {"language": {"source": "zh", "target": "en"}, "llm": {"preset": "fake"}}
    )
    seeder = CjkGlossarySeeder(FakeClient(handler=handler), cfg)
    # 65 candidates forces 2 chunks so we verify byte-identical KV prefix across chunks.
    candidates = [
        SeedCandidate("格蕾丝", 5, "格蕾丝走进大厅"),
        SeedCandidate("长老称呼", 4, "长老称呼"),
        SeedCandidate("青云宗", 8, "青云宗大殿"),
        *[SeedCandidate(f"宗门{i:02d}", 2, "测试") for i in range(62)],
    ]
    terms = seeder.seed(candidates, book_synopsis="Synopsis.", style="Crisp.", max_workers=2)
    by_source = {t.source: t for t in terms}
    assert "格蕾丝" in by_source and by_source["格蕾丝"].target == "Grace"
    assert "青云宗" in by_source and by_source["青云宗"].target == "Azure Cloud Sect"
    assert "长老称呼" not in by_source  # Generic non-person word filtered out.
    assert len(systems) == 2
    assert systems[0] == systems[1]


def test_split_glossary_tiers_keeps_frozen_prefix_independent_of_batch_overflow():
    terms = [
        GlossaryTerm(source="林尘", target="Lin Chen", type="person"),
        GlossaryTerm(source="青云宗", target="Azure Cloud Sect", type="organization"),
        GlossaryTerm(source="太虚剑诀", target="Grand Void Sword Art", type="term"),
        GlossaryTerm(source="第一章 启程", target="Chapter 1: Departure", type=TYPE_NAVIGATION),
    ]
    budget = estimate_term_tokens(terms[0]) + estimate_term_tokens(terms[1])
    p1, o1 = split_glossary_tiers(terms, "林尘施展太虚剑诀，第一章 启程", prefix_budget=budget)
    p2, o2 = split_glossary_tiers(terms, "青云宗的大殿很安静", prefix_budget=budget)
    # Frozen prefix is byte-identical across batches regardless of what overflow terms match.
    assert [t.source for t in p1] == [t.source for t in p2] == ["林尘", "青云宗"]
    assert "太虚剑诀" in [t.source for t in o1]
    assert "第一章 启程" in [t.source for t in o1]
    assert o2 == []


def test_parallel_chapters_seeds_once_protects_user_terms_and_runs_concurrently(tmp_path):
    book = (
        "第一章 启程\n\n"
        "林尘离开青云宗，踏上修行之路。\n\n"
        "林尘带着太虚剑诀前往天城。\n\n"
        "第二章 试炼\n\n"
        "青云宗的林尘在天城遇到试炼。\n\n"
        "太虚剑诀震动了整个青云宗。\n\n"
        "第三章 突破\n\n"
        "林尘终于突破，青云宗欢呼。\n"
    )
    src_path = tmp_path / "webnovel.txt"
    src_path.write_text(book, encoding="utf-8")

    cfg = fake_pipeline_config(os.path.join(tmp_path, "state"))
    cfg.source_lang = "zh"
    cfg.target_lang = "en"
    cfg.pipeline.review = False
    cfg.pipeline.polish = False
    cfg.pipeline.book_understanding = False
    cfg.pipeline.chapter_concurrency = 3

    lock = threading.Lock()
    active = 0
    peak = 0
    seed_calls = 0

    def handler(messages, tier, json_mode):
        nonlocal active, peak, seed_calls
        system = messages[0]["content"]
        if "compile a consistent glossary" in system:
            seed_calls += 1
            return json.dumps(
                {
                    "terms": [
                        {
                            "source": "林尘",
                            "target": "Lin Chen",
                            "type": "person",
                            "gender": "male",
                        },
                        {"source": "青云宗", "target": "Wrong Sect", "type": "organization"},
                    ]
                },
                ensure_ascii=False,
            )
        if tier == "translation.body":
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.05)
                return routing_handler(messages, tier, json_mode)
            finally:
                with lock:
                    active -= 1
        return routing_handler(messages, tier, json_mode)

    client = MeteredFakeClient(handler=handler)
    orch = Orchestrator(cfg, client=client)
    store = require_file_storage(orch.prepare(str(src_path)))
    # Pre-seed a user-owned term (first_chapter=None): upfront seed must never overwrite it.
    store.upsert_term(GlossaryTerm(source="青云宗", target="Azure Cloud Sect", type="organization"))

    orch.run(str(src_path))
    assert peak >= 2
    assert seed_calls == 1
    by_source = {t.source: t for t in store.all_terms()}
    assert by_source["青云宗"].target == "Azure Cloud Sect"
    assert by_source["林尘"].target == "Lin Chen"
    manifest = store.load_manifest()
    assert all(row["status"] == STATUS_DONE for row in manifest["chapters"])

    # Resuming a completed run reuses the cached seed artifact with 0 extra seed calls.
    orch.run(str(src_path))
    assert seed_calls == 1


def test_parallel_pause_persists_all_completed_batches_from_finished_and_running_chapters(tmp_path):
    class _PauseRequested(KeyboardInterrupt):
        pass

    book = (
        "第一章 一\n\n第一段。\n\n第二段。\n\n第三段。\n\n"
        "第二章 二\n\n第四段。\n\n第五段。\n\n第六段。\n"
    )
    src_path = tmp_path / "pause.txt"
    src_path.write_text(book, encoding="utf-8")

    cfg = fake_pipeline_config(os.path.join(tmp_path, "state"))
    cfg.source_lang = "zh"
    cfg.target_lang = "en"
    cfg.pipeline.review = False
    cfg.pipeline.polish = False
    cfg.pipeline.book_understanding = False
    cfg.pipeline.chapter_concurrency = 2
    cfg.segment.max_tokens_per_batch = 1  # 1 paragraph per batch -> 4 batches per chapter

    ch1_batch1_done = threading.Event()
    release_ch0 = threading.Event()
    paid_segments = 0
    lock = threading.Lock()

    def handler(messages, tier, json_mode):
        nonlocal paid_segments
        system = messages[0]["content"]
        if "compile a consistent glossary" in system:
            return json.dumps({"terms": []})
        if tier == "translation.body":
            user = messages[-1]["content"]
            if "第三段" in user:
                # Hold chapter 0's final batch briefly so chapter 1 finishes batch 0 first.
                ch1_batch1_done.wait(timeout=2.0)
            if "第五段" in user:
                # Hold chapter 1's third batch so chapter 0 completes and triggers pause
                # while chapter 1 is mid-flight with completed batches already in memory.
                release_ch0.wait(timeout=2.0)
            out = routing_handler(messages, tier, json_mode)
            count = len(json.loads(out)["translations"])
            with lock:
                paid_segments += count
            if "第四段" in user:
                ch1_batch1_done.set()
            return out
        return routing_handler(messages, tier, json_mode)

    orch = Orchestrator(cfg, client=MeteredFakeClient(handler=handler))
    store = require_file_storage(orch.prepare(str(src_path)))

    def pause_on_first_progress(done, total, label):
        if done > 0:
            release_ch0.set()
            raise _PauseRequested("user paused")

    with pytest.raises(_PauseRequested):
        orch.run(str(src_path), progress=pause_on_first_progress)

    saved_targets = sum(
        1 for ci in (0, 1) for seg in store.load_chapter(ci).text_segments if seg.target is not None
    )
    assert paid_segments >= 5
    assert saved_targets == paid_segments


def test_parallel_polish_outage_marks_failed_and_repolishes_on_resume_without_retranslating(
    tmp_path,
):
    book = "第一章 启程\n\n林尘离开青云宗。\n\n第二章 试炼\n\n青云宗试炼开始。\n"
    src_path = tmp_path / "polish_resume.txt"
    src_path.write_text(book, encoding="utf-8")

    cfg = fake_pipeline_config(os.path.join(tmp_path, "state"))
    cfg.source_lang = "zh"
    cfg.target_lang = "en"
    cfg.pipeline.review = False
    cfg.pipeline.polish = True
    cfg.pipeline.polish_mode = "sparse"
    cfg.pipeline.book_understanding = False
    cfg.pipeline.chapter_concurrency = 2

    translate_calls = 0
    polish_calls = 0
    fail_polish = True

    def handler(messages, tier, json_mode):
        nonlocal translate_calls, polish_calls
        system = messages[0]["content"]
        if "compile a consistent glossary" in system:
            return json.dumps({"terms": []})
        if tier == "translation.body":
            translate_calls += 1
            n = len(json.loads(routing_handler(messages, tier, json_mode))["translations"])
            return json.dumps({"translations": [f"Draft {translate_calls}-{i}." for i in range(n)]})
        if tier == "polish.body":
            polish_calls += 1
            if fail_polish:
                raise TimeoutError("transient polish outage")
            return json.dumps(
                {"patches": [{"index": 0, "text": f"Polished {polish_calls}.", "reason": "style"}]}
            )
        return routing_handler(messages, tier, json_mode)

    orch = Orchestrator(cfg, client=MeteredFakeClient(handler=handler))
    store = require_file_storage(orch.prepare(str(src_path)))

    with pytest.raises(TimeoutError):
        orch.run(str(src_path))

    assert translate_calls == 2
    assert store.pending_chapters() == [0, 1]
    for ci in (0, 1):
        seg = store.load_chapter(ci).text_segments[0]
        assert seg.target is not None
        assert seg.meta.get(POLISH_FAILED) is True

    # Simulate a manual user edit on chapter 0 before resuming; chapter 0 must keep the edit,
    # while chapter 1 gets repolished without any extra translation calls.
    ch0 = store.load_chapter(0)
    ch0.text_segments[0].target = "Manually edited chapter 0."
    store.save_chapter(ch0)

    fail_polish = False
    orch.run(str(src_path))

    assert translate_calls == 2  # Never retranslated already-saved drafts.
    assert store.pending_chapters() == []
    assert store.load_chapter(0).text_segments[0].target == "Manually edited chapter 0."
    assert store.load_chapter(0).text_segments[0].meta.get(POLISH_FAILED) is None
    assert store.load_chapter(1).text_segments[0].target.startswith("Polished ")
    assert store.load_chapter(1).text_segments[0].meta.get(POLISH_FAILED) is None
    manifest = store.load_manifest()
    assert all(not row.get("unpolished") for row in manifest["chapters"])


def test_mine_cjk_candidates_scales_linearly_on_one_million_char_novel():
    # Build a ~1M-char realistic CJK corpus with recurring entities and high unique n-gram noise.
    rng_chars = "天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往秋收冬藏闰余成岁律吕调阳云腾致雨露结为霜"
    verbs = ("离开", "回到", "拜访", "守卫", "探索")
    blocks = []
    for i in range(4000):
        # Mix unique 4-char noise segments with recurring proper nouns in varied contexts.
        c0 = rng_chars[i % len(rng_chars)]
        c1 = rng_chars[(i * 7 + 3) % len(rng_chars)]
        c2 = rng_chars[(i * 13 + 5) % len(rng_chars)]
        c3 = rng_chars[(i * 19 + 11) % len(rng_chars)]
        noise = f"{c0}{c1}{c2}{c3}" * 60
        v1 = verbs[i % len(verbs)]
        v2 = verbs[(i + 2) % len(verbs)]
        blocks.append(f"林尘{v1}青云宗，{v2}《太虚剑诀》，{noise}。")
    corpus = "\n".join(blocks)
    assert len(corpus) >= 950_000

    t0 = time.monotonic()
    candidates = mine_cjk_candidates([], corpus, max_candidates=200)
    elapsed = time.monotonic() - t0
    assert elapsed < 3.0, f"mine_cjk_candidates took {elapsed:.2f}s on ~1M-char corpus"
    sources = [c.source for c in candidates]
    assert "太虚剑诀" in sources
    assert "林尘" in sources
    assert "青云宗" in sources
    assert "太虚" not in sources


def test_seed_service_excludes_nav_respects_user_deletions_and_does_not_cache_failed_chunks(
    tmp_path,
):
    book = "第一章 启程\n\n林尘离开青云宗。\n\n第二章 试炼\n\n林尘再次回到青云宗。\n"
    src_path = tmp_path / "seed_edge.txt"
    src_path.write_text(book, encoding="utf-8")

    cfg = fake_pipeline_config(os.path.join(tmp_path, "state"))
    cfg.source_lang = "zh"
    cfg.target_lang = "en"
    cfg.pipeline.review = False
    cfg.pipeline.polish = False
    cfg.pipeline.book_understanding = False
    cfg.pipeline.chapter_concurrency = 2

    seed_systems: list[str] = []
    malformed = True

    def handler(messages, tier, json_mode):
        system = messages[0]["content"]
        if "compile a consistent glossary" in system:
            seed_systems.append(system)
            if malformed:
                return "not valid json"
            return json.dumps(
                {
                    "terms": [
                        {
                            "source": "林尘",
                            "target": "Lin Chen",
                            "type": "person",
                            "gender": "male",
                        },
                        {"source": "青云宗", "target": "Azure Cloud Sect", "type": "organization"},
                    ]
                },
                ensure_ascii=False,
            )
        return routing_handler(messages, tier, json_mode)

    orch = Orchestrator(cfg, client=MeteredFakeClient(handler=handler))
    store = require_file_storage(orch.prepare(str(src_path)))
    # Insert a navigation term (first_chapter=None): must NOT leak into seed system prompt.
    store.upsert_term(
        GlossaryTerm(source="第一章 启程", target="Chapter 1: Departure", type=TYPE_NAVIGATION)
    )

    # First run: malformed chunk fails -> records incomplete attempt (published=False) so retry runs.
    orch.run(str(src_path))
    assert len(seed_systems) == 1
    assert "第一章 启程" not in seed_systems[0]
    first_cached = store.read_artifact(SEED_ARTIFACT_KEY)
    assert isinstance(first_cached, dict)
    assert first_cached.get("complete") is False
    assert first_cached.get("published") is False
    assert first_cached.get("attempts") == 1

    # Mark chapters pending again and fix handler -> seed retries and writes published artifact.
    for ci in (0, 1):
        store.set_chapter_status(ci, "pending")
    malformed = False
    orch.run(str(src_path))
    assert len(seed_systems) == 2
    cached = store.read_artifact(SEED_ARTIFACT_KEY)
    assert isinstance(cached, dict) and cached.get("published") is True
    assert store.get_term("林尘") is not None

    # User deletes a seeded term -> subsequent run hits published cache and does NOT resurrect it.
    assert store.delete_term("林尘") is True
    for ci in (0, 1):
        store.set_chapter_status(ci, "pending")
    orch.run(str(src_path))
    assert len(seed_systems) == 2  # Zero extra LLM calls.
    assert store.get_term("林尘") is None

    # Adding an unrelated custom term changes the seed fingerprint, yet the deleted seed term
    # must stay deleted across the fingerprint change (tracked via published_sources).
    store.upsert_term(GlossaryTerm(source="天城", target="Sky City", type="place"))
    for ci in (0, 1):
        store.set_chapter_status(ci, "pending")
    orch.run(str(src_path))
    assert len(seed_systems) == 3
    assert store.get_term("林尘") is None
    assert store.get_term("天城") is not None


def test_cjk_seeder_propagates_4xx_errors_copies_contextvars_and_runs_preflight():
    # 1. Non-resumable 401 ProviderRequestError must propagate instead of silently returning [].
    def fail_401(messages, tier, json_mode):
        raise ProviderRequestError(
            provider="deepseek",
            operation="glossary.extract",
            error_type="http_error",
            status_code=401,
            reason=None,
            resumable=False,
        )

    cfg = Config.from_dict(
        {"language": {"source": "zh", "target": "en"}, "llm": {"preset": "fake"}}
    )
    seeder_401 = CjkGlossarySeeder(FakeClient(handler=fail_401), cfg)
    with pytest.raises(ProviderRequestError):
        seeder_401.seed_result([SeedCandidate("林尘", 5, "林尘出场")], max_workers=1)

    # 2. Parallel workers inherit contextvars and preflight shrinks oversized existing glossaries.
    marker: contextvars.ContextVar[str] = contextvars.ContextVar("seed_marker", default="missing")
    observed_markers: list[str] = []
    observed_systems: list[str] = []

    def ok_handler(messages, tier, json_mode):
        observed_markers.append(marker.get())
        observed_systems.append(messages[0]["content"])
        return json.dumps({"terms": []})

    tight_cfg = Config.from_dict(
        {
            "language": {"source": "zh", "target": "en"},
            "llm": {
                "preset": "fake",
                "models": {"fake/tight": {"context_window": 1600, "max_output_tokens": 100}},
                "routes": {"glossary.extract": {"model": "fake/tight"}},
            },
        }
    )
    huge_existing = [
        GlossaryTerm(
            source=f"古宗门{i:03d}", target=f"Ancient Sect Number {i:03d}", type="organization"
        )
        for i in range(120)
    ]
    candidates = [SeedCandidate(f"人物{i:02d}", 3, f"人物{i:02d}出场") for i in range(65)]
    token = marker.set("active-trace")
    try:
        res = CjkGlossarySeeder(FakeClient(handler=ok_handler), tight_cfg).seed_result(
            candidates,
            existing_terms=huge_existing,
            max_workers=2,
        )
    finally:
        marker.reset(token)
    assert res.complete is True
    assert observed_markers == ["active-trace", "active-trace"]
    # Preflight trimmed huge_existing so every chunk fits cleanly inside the 900-token window.
    assert all("古宗门119" not in sys_prompt for sys_prompt in observed_systems)


def test_fit_terms_to_budget_preserves_leading_prefix_across_different_batches():
    terms = [
        GlossaryTerm(source="林尘", target="Lin Chen", type="person"),
        GlossaryTerm(source="青云宗", target="Azure Cloud Sect", type="organization"),
        GlossaryTerm(source="太虚剑诀", target="Grand Void Sword Art", type="term"),
        GlossaryTerm(source="黑风谷", target="Black Wind Valley", type="place"),
        GlossaryTerm(source="血魔殿", target="Blood Demon Hall", type="organization"),
    ]
    # Budget fits the first 2 leading terms plus room for 1 matched overflow term.
    lead_tokens = estimate_term_tokens(terms[0]) + estimate_term_tokens(terms[1])
    extra_tokens = max(estimate_term_tokens(terms[3]), estimate_term_tokens(terms[4]))
    budget = lead_tokens + extra_tokens

    batch_a = fit_terms_to_budget(terms, "黑风谷的夜晚", max_tokens=budget)
    batch_b = fit_terms_to_budget(terms, "血魔殿来袭", max_tokens=budget)
    # Leading non-navigation prefix is identical across both batches for KV cache hits.
    assert [t.source for t in batch_a[:2]] == ["林尘", "青云宗"]
    assert [t.source for t in batch_b[:2]] == ["林尘", "青云宗"]
    assert batch_a[2].source == "黑风谷"
    assert batch_b[2].source == "血魔殿"


def test_parallel_cancellation_during_alignment_still_persists_all_wave_batches(tmp_path):
    book = "第一章 一\n\n第一段。\n\n第二段。\n\n第二章 二\n\n第三段。\n\n第四段。\n"
    src_path = tmp_path / "align_cancel.txt"
    src_path.write_text(book, encoding="utf-8")

    cfg = fake_pipeline_config(os.path.join(tmp_path, "state"))
    cfg.source_lang = "zh"
    cfg.target_lang = "en"
    cfg.pipeline.review = False
    cfg.pipeline.polish = False
    cfg.pipeline.book_understanding = False
    cfg.pipeline.chapter_concurrency = 2
    cfg.segment.max_tokens_per_batch = 1

    orch = Orchestrator(cfg, client=MeteredFakeClient(handler=routing_handler))
    store = require_file_storage(orch.prepare(str(src_path)))

    align_calls = 0

    def cancel_on_first_align(st, ch, segs, start, count):
        nonlocal align_calls
        align_calls += 1
        raise RequestCancelled("cancelled during alignment")

    runner = ParallelChapterRunner(
        orch._translation._batches,
        chapter_batches_fn=orch._translation._chapter_batches,
        align_batch_fn=cancel_on_first_align,
        progress_label_fn=orch._translation.chapter_progress_label,
        flush_usage_fn=lambda st, scope: None,
    )
    with pytest.raises(RequestCancelled):
        runner.run(
            [0, 1],
            store,
            frozen_terms=[],
            style="",
            book_synopsis="",
            annotation_context_registry=None,
            chapter_concurrency=2,
            rolling_context_tokens=200,
            polish=False,
            polish_mode="full",
            progress=None,
            done=0,
            total=6,
        )
    assert align_calls == 1
    # Every segment of the chapter whose alignment triggered cancellation was already saved!
    saved_ch0 = [s.target for s in store.load_chapter(0).text_segments if s.target is not None]
    saved_ch1 = [s.target for s in store.load_chapter(1).text_segments if s.target is not None]
    assert len(saved_ch0) == 3 or len(saved_ch1) == 3


def test_parallel_repolish_never_feeds_rejected_polish_into_subsequent_batch_context(tmp_path):
    book = "第一章 一\n\n第一段。\n\n第二段。\n\n第二章 二\n\n第三段。\n\n第四段。\n"
    src_path = tmp_path / "repolish_ctx.txt"
    src_path.write_text(book, encoding="utf-8")

    cfg = fake_pipeline_config(os.path.join(tmp_path, "state"))
    cfg.source_lang = "zh"
    cfg.target_lang = "en"
    cfg.pipeline.review = False
    cfg.pipeline.polish = True
    cfg.pipeline.polish_mode = "sparse"
    cfg.pipeline.book_understanding = False
    cfg.pipeline.chapter_concurrency = 2
    cfg.segment.max_tokens_per_batch = 1

    captured_translate_users: list[str] = []

    def handler(messages, tier, json_mode):
        system = messages[0]["content"]
        if "compile a consistent glossary" in system:
            return json.dumps({"terms": []})
        if tier == "polish.body":
            return json.dumps(
                {"patches": [{"index": 0, "text": "Rejected polished draft.", "reason": "style"}]}
            )
        if tier == "translation.body":
            captured_translate_users.append(messages[-1]["content"])
            return routing_handler(messages, tier, json_mode)
        return routing_handler(messages, tier, json_mode)

    orch = Orchestrator(cfg, client=MeteredFakeClient(handler=handler))
    store = require_file_storage(orch.prepare(str(src_path)))

    # Mark first two segments of each chapter as already translated with a manual author edit
    # that differs from target_before_polish, while still carrying POLISH_FAILED.
    for ci in (0, 1):
        ch = store.load_chapter(ci)
        for seg in ch.text_segments[:2]:
            seg.target = "Author edit"
            seg.target_before_polish = "Original draft"
            seg.meta[POLISH_FAILED] = True
        store.save_chapter(ch)

    orch.run(str(src_path))
    assert captured_translate_users
    assert all("Author edit" in user for user in captured_translate_users)
    assert all("Rejected polished draft." not in user for user in captured_translate_users)


def test_cjk_seeder_absorbs_400_content_filter_validates_rows_and_keeps_count_parity(tmp_path):
    cfg = Config.from_dict(
        {"language": {"source": "zh", "target": "en"}, "llm": {"preset": "fake"}}
    )
    cand = [SeedCandidate("林尘", 5, "林尘出场")]

    # 1. Explicit {"terms": []} is complete=True; malformed rows are complete=False.
    res_empty = CjkGlossarySeeder(
        FakeClient(handler=lambda *a: json.dumps({"terms": []})), cfg
    ).seed_result(cand, max_workers=1)
    assert res_empty.complete is True and res_empty.terms == []

    for bad_payload in (
        {"terms": [None]},
        {"terms": [{"source": "林尘", "target": {"nested": "bad"}}]},
        {"terms": [{"source": "林尘", "target": "Lin Chen", "aliases": [123]}]},
    ):
        res_bad = CjkGlossarySeeder(
            FakeClient(handler=lambda *a, p=bad_payload: json.dumps(p)), cfg
        ).seed_result(cand, max_workers=1)
        assert res_bad.complete is False

    # 2. HTTP 400 (e.g. DeepSeek "Content Exists Risk") is absorbed as incomplete, while
    # misconfiguration errors like HTTP 405 still propagate.
    def fail_400(messages, tier, json_mode):
        raise ProviderRequestError(
            provider="deepseek",
            operation="glossary.extract",
            error_type="http_error",
            status_code=400,
            reason=None,
            resumable=False,
        )

    def fail_405(messages, tier, json_mode):
        raise ProviderRequestError(
            provider="deepseek",
            operation="glossary.extract",
            error_type="http_error",
            status_code=405,
            reason=None,
            resumable=False,
        )

    res_400 = CjkGlossarySeeder(FakeClient(handler=fail_400), cfg).seed_result(cand, max_workers=1)
    assert res_400.complete is False and res_400.terms == []
    with pytest.raises(ProviderRequestError):
        CjkGlossarySeeder(FakeClient(handler=fail_405), cfg).seed_result(cand, max_workers=1)

    # 3. Incomplete seed retries are capped at 2 attempts on deterministic 400 so runs stop
    # re-paying, and an interrupted publication after attempt 2 replays with 0 extra LLM calls.
    book = "第一章 一\n\n林尘离开青云宗。\n\n第二章 二\n\n林尘回到青云宗。\n"
    src_path = tmp_path / "cap_seed.txt"
    src_path.write_text(book, encoding="utf-8")
    run_cfg = fake_pipeline_config(os.path.join(tmp_path, "state"))
    run_cfg.source_lang = "zh"
    run_cfg.target_lang = "en"
    run_cfg.pipeline.review = False
    run_cfg.pipeline.polish = False
    run_cfg.pipeline.book_understanding = False
    run_cfg.pipeline.chapter_concurrency = 2

    seed_attempts = 0

    def always_400_seed(messages, tier, json_mode):
        nonlocal seed_attempts
        if "compile a consistent glossary" in messages[0]["content"]:
            seed_attempts += 1
            return fail_400(messages, tier, json_mode)
        return routing_handler(messages, tier, json_mode)

    orch = Orchestrator(run_cfg, client=MeteredFakeClient(handler=always_400_seed))
    store = require_file_storage(orch.prepare(str(src_path)))
    for _ in range(3):
        for ci in (0, 1):
            store.set_chapter_status(ci, "pending")
        orch.run(str(src_path))
    assert seed_attempts == 2  # Capped after 2 deterministic failures.

    # Simulate an interrupted publication right after attempt 2 wrote published=False.
    cached = store.read_artifact(SEED_ARTIFACT_KEY)
    assert isinstance(cached, dict)
    store.write_artifact(SEED_ARTIFACT_KEY, {**cached, "published": False})
    for ci in (0, 1):
        store.set_chapter_status(ci, "pending")
    orch.run(str(src_path))
    assert seed_attempts == 2  # Replayed without a 3rd model call.
    assert store.read_artifact(SEED_ARTIFACT_KEY)["published"] is True

    # 4. Counting parity across the 250-candidate threshold using pure-CJK tokens so the
    # >250 branch genuinely runs the linear scan with strictly non-overlapping counts.
    small = _count_candidates_in_corpus("哈哈哈", {"哈哈"})
    pure_cjk_filler = {chr(0x4E00 + i) + chr(0x4E00 + i + 1) for i in range(260)}
    large = _count_candidates_in_corpus("哈哈哈", {"哈哈"} | pure_cjk_filler)
    assert small == {}
    assert large == {}


def test_parallel_ctrl_c_during_batch_event_still_saves_all_wave_targets(tmp_path):
    book = "第一章 一\n\n第一段。\n\n第二段。\n\n第二章 二\n\n第三段。\n\n第四段。\n"
    src_path = tmp_path / "event_interrupt.txt"
    src_path.write_text(book, encoding="utf-8")

    cfg = fake_pipeline_config(os.path.join(tmp_path, "state"))
    cfg.source_lang = "zh"
    cfg.target_lang = "en"
    cfg.pipeline.review = False
    cfg.pipeline.polish = False
    cfg.pipeline.book_understanding = False
    cfg.pipeline.chapter_concurrency = 2
    cfg.segment.max_tokens_per_batch = 1

    orch = Orchestrator(cfg, client=MeteredFakeClient(handler=routing_handler))
    store = require_file_storage(orch.prepare(str(src_path)))

    orig_log = store.log_event

    def interrupt_on_first_batch_event(event, **kwargs):
        if event == "batch_translated":
            raise KeyboardInterrupt("ctrl-c during log_event")
        return orig_log(event, **kwargs)

    store.log_event = interrupt_on_first_batch_event  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        orch._translation.run(store, book_synopsis="")

    # Every completed batch of every chapter in the wave was durably saved before log_event ran.
    saved = [
        seg.target
        for ci in (0, 1)
        for seg in store.load_chapter(ci).text_segments
        if seg.target is not None
    ]
    assert len(saved) >= 3


def test_parallel_interrupted_save_chapter_retries_without_double_consuming_polish_attempt(
    tmp_path,
):
    book = "第一章 启程\n\n第一段。\n\n第二章 试炼\n\n第二段。\n"
    src_path = tmp_path / "save_interrupt.txt"
    src_path.write_text(book, encoding="utf-8")

    cfg = fake_pipeline_config(os.path.join(tmp_path, "state"))
    cfg.source_lang = "zh"
    cfg.target_lang = "en"
    cfg.pipeline.review = False
    cfg.pipeline.polish = True
    cfg.pipeline.polish_mode = "sparse"
    cfg.pipeline.book_understanding = False
    cfg.pipeline.chapter_concurrency = 2

    def bad_polish(messages, tier, json_mode):
        if "compile a consistent glossary" in messages[0]["content"]:
            return json.dumps({"terms": []})
        if tier == "polish.body":
            return "not valid json"
        return routing_handler(messages, tier, json_mode)

    orch = Orchestrator(cfg, client=MeteredFakeClient(handler=bad_polish))
    store = require_file_storage(orch.prepare(str(src_path)))

    orig_save = store.save_chapter
    interrupted_once = False

    def interrupt_first_save(chapter):
        nonlocal interrupted_once
        if not interrupted_once:
            interrupted_once = True
            raise KeyboardInterrupt("ctrl-c during save_chapter")
        return orig_save(chapter)

    store.save_chapter = interrupt_first_save  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        orch._translation.run(store, book_synopsis="")

    # Drain retried save_chapter cleanly WITHOUT reapplying mark_polish_failure a second time,
    # so POLISH_FAILED stays set with polish_attempts == 1 (never exhausted to 2).
    saved_ch0 = store.load_chapter(0)
    saved_ch1 = store.load_chapter(1)
    saved_segs = [s for ch in (saved_ch0, saved_ch1) for s in ch.text_segments if s.target]
    assert len(saved_segs) >= 2
    assert all(s.meta.get(POLISH_FAILED) is True for s in saved_segs)
    assert all(s.meta.get("polish_attempts") == 1 for s in saved_segs)


def test_resuming_single_unpolished_chapter_after_parallel_run_skips_batch_extraction(tmp_path):
    book = "第一章 启程\n\n第一段。\n\n第二章 试炼\n\n第二段。\n"
    src_path = tmp_path / "single_repolish.txt"
    src_path.write_text(book, encoding="utf-8")

    cfg = fake_pipeline_config(os.path.join(tmp_path, "state"))
    cfg.source_lang = "zh"
    cfg.target_lang = "en"
    cfg.pipeline.review = False
    cfg.pipeline.polish = True
    cfg.pipeline.polish_mode = "sparse"
    cfg.pipeline.book_understanding = False
    cfg.pipeline.chapter_concurrency = 2

    extract_calls = 0
    fail_ch1_polish = True

    def handler(messages, tier, json_mode):
        nonlocal extract_calls
        system = messages[0]["content"]
        if "compile a consistent glossary" in system:
            return json.dumps({"terms": []})
        if tier == "glossary.extract":
            extract_calls += 1
        if tier == "polish.body" and fail_ch1_polish and "第二段" in messages[1]["content"]:
            return "not valid json"
        if tier == "polish.body":
            return json.dumps({"patches": [{"index": 0, "text": "Polished.", "reason": "flow"}]})
        return routing_handler(messages, tier, json_mode)

    orch = Orchestrator(cfg, client=MeteredFakeClient(handler=handler))
    store = require_file_storage(orch.prepare(str(src_path)))
    orch.run(str(src_path))

    # Chapter 0 finished cleanly; chapter 1 finished STATUS_DONE with unpolished=True.
    manifest = store.load_manifest()
    assert not manifest["chapters"][0].get("unpolished")
    assert manifest["chapters"][1].get("unpolished") is True

    # Resume has 1 target (chapter 1) -> falls back to serial translate_chapter with
    # reopened_done=True and must run ZERO glossary.extract calls.
    extract_calls = 0
    fail_ch1_polish = False
    orch.run(str(src_path))
    assert extract_calls == 0
    assert not store.load_manifest()["chapters"][1].get("unpolished")


def test_preflight_trimming_never_promotes_per_batch_overflow_into_system_glossary():
    terms = [
        GlossaryTerm(source="林尘", target="Lin Chen", type="person"),
        GlossaryTerm(
            source="太上忘情九霄剑宗",
            target="Supreme Emotionless Nine Heavens Immortal Sword Sect",
            type="organization",
        ),
        GlossaryTerm(source="黑风谷", target="Black Wind Valley", type="place"),
        GlossaryTerm(source="血魔殿", target="Blood Demon Hall", type="organization"),
    ]
    # Budget fits leading term + 1 matched overflow term, but NOT the oversized 2nd term.
    budget = estimate_term_tokens(terms[0]) + max(
        estimate_term_tokens(terms[2]), estimate_term_tokens(terms[3])
    )
    assert estimate_term_tokens(terms[0]) + estimate_term_tokens(terms[1]) > budget
    sel_a = fit_terms_to_budget(terms, "黑风谷", max_tokens=budget)
    sel_b = fit_terms_to_budget(terms, "血魔殿", max_tokens=budget)
    sys_a, usr_a = split_trimmed_glossary_tiers(terms, sel_a, "黑风谷", prefix_budget=0)
    sys_b, usr_b = split_trimmed_glossary_tiers(terms, sel_b, "血魔殿", prefix_budget=0)
    assert [t.source for t in sys_a] == ["林尘"]
    assert [t.source for t in sys_b] == ["林尘"]
    assert [t.source for t in usr_a] == ["黑风谷"]
    assert [t.source for t in usr_b] == ["血魔殿"]

    # End-to-end Translator preflight check: system prompt stays byte-identical across batches.
    observed_systems: list[str] = []
    observed_users: list[str] = []

    def handler(messages, tier, json_mode):
        observed_systems.append(messages[0]["content"])
        observed_users.append(messages[1]["content"])
        return json.dumps({"translations": ["Translated."]})

    big_glossary = [
        GlossaryTerm(source="林尘", target="Lin Chen", type="person"),
        *[
            GlossaryTerm(source=f"填充词{i:03d}", target=f"Padding Term {i:03d}", type="term")
            for i in range(60)
        ],
        GlossaryTerm(source="黑风谷", target="Black Wind Valley", type="place"),
        GlossaryTerm(source="血魔殿", target="Blood Demon Hall", type="organization"),
    ]
    cfg = Config.from_dict(
        {
            "language": {"source": "zh", "target": "en"},
            "llm": {
                "preset": "fake",
                "models": {"fake/tight": {"context_window": 1650, "max_output_tokens": 100}},
                "routes": {"translation.body": {"model": "fake/tight"}},
            },
            "pipeline": {"glossary_prompt_tokens": 0},
        }
    )
    translator = Translator(FakeClient(handler=handler), cfg)
    translator.translate_batch(["黑风谷的夜晚。"], glossary_terms=big_glossary)
    translator.translate_batch(["血魔殿的夜晚。"], glossary_terms=big_glossary)
    assert len(observed_systems) == 2
    assert observed_systems[0] == observed_systems[1]
    assert "黑风谷" not in observed_systems[0]
    assert "血魔殿" not in observed_systems[1]
    assert "黑风谷" in observed_users[0]
    assert "血魔殿" in observed_users[1]
