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
    assert qm.exact_match(quote) == f"“{rendering}”"


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
