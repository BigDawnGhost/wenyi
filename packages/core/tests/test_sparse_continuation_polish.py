"""Tests for continuation-aware sparse polish and minimal escaped-newline hygiene."""

from __future__ import annotations

import json
import os

from wenyi_core.agents.polisher import Polisher
from wenyi_core.agents.sparse_editor import SparseSemanticEditor, _apply_validated_patches
from wenyi_core.agents.translator import Translator
from wenyi_core.config import Config
from wenyi_core.models.glossary import GlossaryTerm
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.pipeline.translate.post_normalize import repair_escaped_newlines
from wenyi_core.pipeline.translate.translation_batch import (
    POLISH_FAILED,
    BatchPlan,
    TranslationBatchExecutor,
)
from wenyi_core.storage.run_store import STATUS_DONE

from tests.fake_llm import FakeClient, MeteredFakeClient, routing_handler
from tests.pipeline_fixtures import fake_pipeline_config, require_file_storage


def _zh_en_config() -> Config:
    return Config.from_dict(
        {
            "language": {"source": "zh", "target": "en"},
            "llm": {"preset": "fake"},
            "pipeline": {"polish": True, "polish_mode": "sparse"},
        }
    )


def test_sparse_polish_continues_translation_conversation_for_kv_cache():
    calls: list[dict] = []

    def handler(messages, tier, json_mode):
        calls.append({"operation": tier, "messages": messages})
        if tier == "translation.body":
            return json.dumps({"translations": ["Draft zero.", "Draft one literal calque."]})
        if tier == "polish.body":
            return json.dumps(
                {"patches": [{"index": 1, "text": "Clean natural English.", "reason": "calque"}]}
            )
        raise AssertionError(f"unexpected tier {tier}")

    cfg = _zh_en_config()
    client = FakeClient(handler=handler)
    executor = TranslationBatchExecutor(
        Translator(client, cfg),
        Polisher(client, cfg),
        SparseSemanticEditor(client, cfg),
    )
    plan = BatchPlan(
        chapter=0,
        start_index=0,
        segment_indices=(0, 1),
        sources=("第一句。", "第二句。"),
        terms=(GlossaryTerm(source="林尘", target="Lin Chen", type="person"),),
        context="",
        style="Punchy xianxia.",
        book_synopsis="A young cultivator rises.",
        chapter_digest="Chapter 1.",
        annotation_contexts=[[], []],
        next_source="",
    )
    res = executor.execute(plan, polish=True, polish_mode="sparse")
    assert not res.polish_failed
    assert res.targets == ("Draft zero.", "Clean natural English.")
    assert res.before_polish == ("Draft zero.", "Draft one literal calque.")
    assert len(calls) == 2
    translate_msgs = calls[0]["messages"]
    polish_msgs = calls[1]["messages"]
    # Continuation turn preserves the exact [system, user, assistant] prefix for DeepSeek KV cache.
    assert len(polish_msgs) == 4
    assert polish_msgs[:2] == translate_msgs
    assert polish_msgs[2]["role"] == "assistant"
    assert polish_msgs[3]["role"] == "user"


def test_sparse_polish_falls_back_to_standalone_cached_batch_when_continuation_fails():
    calls: list[dict] = []

    def handler(messages, tier, json_mode):
        calls.append({"operation": tier, "messages": messages})
        if tier == "translation.body":
            return json.dumps({"translations": ["Draft zero.", "Draft one."]})
        if tier == "polish.body" and len(messages) == 4:
            return "not json at all"
        if tier == "polish.body" and len(messages) == 2:
            return json.dumps(
                {"patches": [{"index": 0, "text": "Patched zero.", "reason": "omission"}]}
            )
        raise AssertionError(f"unexpected call {tier}")

    cfg = _zh_en_config()
    client = FakeClient(handler=handler)
    executor = TranslationBatchExecutor(
        Translator(client, cfg),
        Polisher(client, cfg),
        SparseSemanticEditor(client, cfg),
    )
    plan = BatchPlan(
        chapter=0,
        start_index=0,
        segment_indices=(0, 1),
        sources=("原文一。", "原文二。"),
        terms=(GlossaryTerm(source="青云宗", target="Azure Cloud Sect", type="organization"),),
        context="",
        style="Crisp tone.",
        book_synopsis="",
        chapter_digest="",
        annotation_contexts=[[], []],
        next_source="",
    )
    res = executor.execute(plan, polish=True, polish_mode="sparse")
    assert not res.polish_failed
    assert res.targets == ("Patched zero.", "Draft one.")
    assert len(calls) == 3
    standalone_system = calls[2]["messages"][0]["content"]
    assert "<style_data>" in standalone_system
    assert "<glossary_data>" in standalone_system
    assert "青云宗 → Azure Cloud Sect" in standalone_system


def test_sparse_patch_validation_is_strict_and_never_drops_markers_or_duplicates():
    drafts = [
        "Hello 〘object:1〙 world.",
        "Keep non-empty draft.",
        "Original two.",
    ]
    res = _apply_validated_patches(
        drafts,
        [
            {"index": 0, "text": "Hello world.", "reason": "dropped marker"},
            {"index": 1, "text": "   ", "reason": "blanked"},
            {"index": 2, "text": "First fix.", "reason": "omission"},
            {"index": 2, "text": "Second overwrite.", "reason": "duplicate index"},
            {"index": True, "text": "Bool index.", "reason": "invalid"},
            {"index": 99, "text": "Out of bounds.", "reason": "invalid"},
        ],
    )
    assert not res.failed
    assert res.targets == ("Hello 〘object:1〙 world.", "Keep non-empty draft.", "First fix.")
    assert res.patched_indices == (2,)


def test_repair_escaped_newlines_preserves_source_literals_code_urls_and_markers():
    assert repair_escaped_newlines("第一行\n第二行", "Line 1\\nLine 2") == "Line 1\nLine 2"
    # Source already has literal \n -> untouched.
    assert repair_escaped_newlines(r"literal \n here", r"literal \n here") == r"literal \n here"
    # Protected spans and unprotected \nu / \frac{1}{\nu} / dir\new stay untouched.
    assert (
        repair_escaped_newlines(
            "原文",
            "Before `a\\nb` https://ex.com/\\n 〘object:a\\nb〙 $\\nu$ "
            "\\frac{1}{\\nu} C:\\new\\dir dir\\new \\nu end.\\nnext",
        )
        == "Before `a\\nb` https://ex.com/\\n 〘object:a\\nb〙 $\\nu$ "
        "\\frac{1}{\\nu} C:\\new\\dir dir\\new \\nu end.\nnext"
    )


def test_sparse_editor_returns_failed_result_when_preflight_exceeds_context_window():
    cfg = Config.from_dict(
        {
            "language": {"source": "zh", "target": "en"},
            "llm": {
                "preset": "fake",
                "models": {"fake/tiny": {"context_window": 120, "max_output_tokens": 50}},
                "routes": {"polish.body": {"model": "fake/tiny"}},
            },
            "pipeline": {"polish": True, "polish_mode": "sparse"},
        }
    )
    editor = SparseSemanticEditor(
        FakeClient(handler=lambda *a: (_ for _ in ()).throw(AssertionError("should not call LLM"))),
        cfg,
    )
    res = editor.edit_batch(["原文。" * 40], ["Draft translation. " * 40])
    assert res.failed is True
    assert res.targets == ("Draft translation. " * 40,)


def test_english_progress_label_renders_stable_pipeline_keys():
    from wenyi_core.progress_labels import english_progress_label

    assert english_progress_label("progress.stage.assembling") == "Assembling translation…"
    assert english_progress_label("progress.stage.seedingGlossary") == "Seeding CJK glossary…"
    assert english_progress_label("progress.stage.reviewRound:1") == "Whole-book review R1"
    assert english_progress_label("progress.stage.reviewRound:2") == "Blind whole-book review R2"
    assert english_progress_label("progress.stage.chapter:5 · draft") == "Chapter 5 · draft"
    assert english_progress_label("Chapter 5") == "Chapter 5"


def test_serial_resume_recovers_polish_after_escaped_newline_repair_and_done_unpolished(tmp_path):
    book = "第一章 启程\n\n第一行\n第二行\n"
    src_path = tmp_path / "escaped_resume.txt"
    src_path.write_text(book, encoding="utf-8")

    cfg = fake_pipeline_config(os.path.join(tmp_path, "state"))
    cfg.source_lang = "zh"
    cfg.target_lang = "en"
    cfg.pipeline.review = False
    cfg.pipeline.polish = True
    cfg.pipeline.polish_mode = "sparse"
    cfg.pipeline.book_understanding = False
    cfg.pipeline.chapter_concurrency = 1
    cfg.pipeline.polish_concurrency = 1

    fail_polish = True
    translate_calls = 0

    def handler(messages, tier, json_mode):
        nonlocal translate_calls
        if tier == "translation.body":
            translate_calls += 1
            n = len(json.loads(routing_handler(messages, tier, json_mode))["translations"])
            # Model leaks literal \n when source has real newline.
            return json.dumps({"translations": ["Line 1\\nLine 2"] * n})
        if tier == "polish.body":
            if fail_polish:
                # Non-resumable malformed output -> chapter finishes marked unpolished.
                return "not valid json"
            return json.dumps(
                {"patches": [{"index": 0, "text": "Polished Line 1\nLine 2", "reason": "flow"}]}
            )
        return routing_handler(messages, tier, json_mode)

    orch = Orchestrator(cfg, client=MeteredFakeClient(handler=handler))
    store = require_file_storage(orch.prepare(str(src_path)))
    orch.run(str(src_path))

    # Chapter finished STATUS_DONE with unpolished=True and repaired target/target_before_polish.
    ch = store.load_chapter(0)
    seg = ch.text_segments[0]
    assert seg.target == "Line 1\nLine 2"
    assert seg.target_before_polish == "Line 1\nLine 2"
    assert seg.meta.get(POLISH_FAILED) is True
    manifest = store.load_manifest()
    assert manifest["chapters"][0]["status"] == STATUS_DONE
    assert manifest["chapters"][0].get("unpolished") is True

    # Simulate an older upgraded project where target_before_polish still holds raw literal \n.
    seg.target_before_polish = "Line 1\\nLine 2"
    store.save_chapter(ch)

    # Resume must pick up the done+unpolished chapter, recognize normalized draft equality,
    # apply the recovered polish, and clear unpolished without retranslating.
    fail_polish = False
    orch.run(str(src_path))

    assert translate_calls == 1
    ch_after = store.load_chapter(0)
    assert ch_after.text_segments[0].target == "Polished Line 1\nLine 2"
    assert ch_after.text_segments[0].meta.get(POLISH_FAILED) is None
    assert not store.load_manifest()["chapters"][0].get("unpolished")


def test_serial_reopened_unpolished_chapter_skips_reextraction_and_caps_deterministic_retries(
    tmp_path,
):
    book = "第一章 启程\n\n第一段。\n"
    src_path = tmp_path / "polish_cap.txt"
    src_path.write_text(book, encoding="utf-8")

    cfg = fake_pipeline_config(os.path.join(tmp_path, "state"))
    cfg.source_lang = "zh"
    cfg.target_lang = "en"
    cfg.pipeline.review = False
    cfg.pipeline.polish = True
    cfg.pipeline.polish_mode = "sparse"
    cfg.pipeline.book_understanding = False
    cfg.pipeline.chapter_concurrency = 1
    cfg.pipeline.polish_concurrency = 1

    tier_counts: dict[str, int] = {}

    def always_bad_polish(messages, tier, json_mode):
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        if tier == "polish.body":
            return "not valid json"
        return routing_handler(messages, tier, json_mode)

    orch = Orchestrator(cfg, client=MeteredFakeClient(handler=always_bad_polish))
    store = require_file_storage(orch.prepare(str(src_path)))

    # Run 1: initial translation + failed polish -> chapter marked unpolished.
    orch.run(str(src_path))
    assert store.load_manifest()["chapters"][0].get("unpolished") is True
    extract_after_run1 = tier_counts.get("glossary.extract", 0)

    # Run 2: reopened done chapter retries polish ONCE without repeating chapter glossary extract,
    # hits the 2-attempt cap, and clears unpolished so Run 3 does zero model calls.
    tier_counts.clear()
    orch.run(str(src_path))
    assert tier_counts.get("glossary.extract", 0) == 0
    assert tier_counts.get("polish.body", 0) == 1
    assert not store.load_manifest()["chapters"][0].get("unpolished")
    assert extract_after_run1 >= 1

    # Run 3: completely settled, 0 model calls.
    tier_counts.clear()
    orch.run(str(src_path))
    assert tier_counts == {}
