"""Tests for next-gen pipeline architecture:
- Book Bible compilation + dual-context entropy probe + resumable artifact caching
- Chapter-parallel drafting with positional source lookbehind, single-writer order
  invariance, crash/resume safety, and huge-chapter wave splitting
- Sparse source-grounded semantic editor (polish_mode='sparse')
- Opaque entity-marker transport (entity_markers=True)
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from wenyi_core.agents.book_bible import BookBibleAgent, mine_bible_candidates
from wenyi_core.agents.sparse_editor import SparseSemanticEditor
from wenyi_core.config import Config
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.models.glossary import GlossaryTerm
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.pipeline.translate.entity_markers import (
    mask_batch_entities,
    unmask_batch_entities,
)


def test_bible_candidates_mine_digest_entities_and_filter_forms_of_address():
    digests = [
        "孫悟空 becomes Sanzang's (三藏) disciple and receives the byname 行者. 孤 speaks at court.",
        "行者 and 三藏 meet 豬八戒 at 高老莊. 弼馬溫 is mocked.",
    ]
    corpus = "\n".join(
        [
            "三藏對孫悟空說：叫你行者吧。",
            "行者笑著點頭，跟著三藏往西去。",
            "豬八戒在高老莊嘲笑行者曾做過弼馬溫。",
            "行者大怒，說起弼馬溫舊事，豬八戒連忙求饒，三藏在旁勸解，高老莊眾人圍觀。",
            "孤今日甚喜。孤要宴飲。",
        ]
    )
    cands = mine_bible_candidates(digests, corpus, source_lang="zh-Hant")
    sources = {c.source for c in cands}
    assert "行者" in sources
    assert "三藏" in sources
    assert "弼馬溫" in sources
    assert "弼馬" not in sources  # partial stem shadowed by 弼馬溫
    assert "孤" not in sources  # blocked context-dependent pronoun


def test_bible_candidates_capture_three_hit_chapter_entities_and_prune_stems():
    corpus = "\n".join(
        [
            "增長天王守在南天門外，手握寶劍。",
            "石匣之中放著錦襴袈裟，增長天王前來巡視。",
            "打開石匣，取出袈裟，增長天王微微點頭，復合石匣。",
            "他在御馬監當了弼馬溫，人人都叫他弼馬溫，好個弼馬溫，氣煞弼馬溫。",
        ]
    )
    cands = mine_bible_candidates([], corpus, source_lang="zh-Hant")
    sources = {c.source for c in cands}
    assert "增長天王" in sources
    assert "弼馬溫" in sources
    assert "弼馬" not in sources


def test_book_bible_dual_probe_adjudicates_and_respects_user_imports(tmp_path: Path):
    calls: list[str] = []

    def handler(messages, tier, json_mode):
        sys_msg = messages[0]["content"]
        user_msg = messages[-1]["content"]
        if "terminology prober" in sys_msg:
            calls.append("probe")
            if len(calls) == 1:
                return json.dumps(
                    {
                        "renderings": {
                            "行者": "Xingzhe",
                            "三藏": "San Zang",
                            "弼馬溫": "Bi Ma Wen",
                            "大人": "Your Excellency",
                        }
                    }
                )
            return json.dumps(
                {
                    "renderings": {
                        "行者": "Pilgrim",
                        "三藏": "Sanzang",
                        "弼馬溫": "Protector of the Horses",
                        "大人": "my lord",
                    }
                }
            )
        if "Book Bible" in sys_msg:
            calls.append("adjudicate")
            assert "DIVERGENT" in user_msg
            return json.dumps(
                {
                    "terms": [
                        {
                            "source": "行者",
                            "target": "Pilgrim",
                            "type": "person",
                            "gender": "male",
                            "aliases": [],
                            "note": "Wukong's byname",
                        },
                        {
                            "source": "三藏",
                            "target": "Sanzang",
                            "type": "person",
                            "gender": "male",
                            "aliases": [],
                            "note": "",
                        },
                        {
                            "source": "弼馬溫",
                            "target": "Protector of the Horses",
                            "type": "term",
                            "gender": "unknown",
                            "aliases": [],
                            "note": "",
                        },
                    ]
                }
            )
        return "{}"

    cfg = Config.from_dict(
        {
            "language": {"source": "zh-Hant", "target": "en"},
            "llm": {"preset": "fake"},
            "paths": {"state_dir": str(tmp_path / "state")},
            "pipeline": {"glossary_mode": "compiled"},
        }
    )
    client = FakeClient(handler=handler)
    agent = BookBibleAgent(client, cfg)
    agent.src = "zh-Hant"
    agent.tgt = "en"
    digests = ["三藏 gives 孫悟空 the byname 行者; 弼馬溫 is his old heavenly post."]
    corpus = "行者跟隨三藏。行者曾任弼馬溫。三藏勸阻行者，莫提弼馬溫。"
    res = agent.compile_bible(
        digests,
        corpus,
        book_synopsis="Journey to the West.",
        existing_terms=[GlossaryTerm(source="三藏", target="Tripitaka", type="person")],
    )
    assert calls == ["probe", "probe", "adjudicate"]
    assert res.divergent_count >= 2
    by_src = {t.source: t.target for t in res.terms}
    assert by_src["行者"] == "Pilgrim"
    assert by_src["弼馬溫"] == "Protector of the Horses"
    # Existing user-imported 三藏 was excluded from candidates and never overwritten.
    assert "三藏" not in by_src
    assert set(res.variants_by_source["行者"]) == {"Xingzhe", "Pilgrim"}


def test_sparse_semantic_editor_patches_only_defects_and_preserves_markers():
    def handler(messages, tier, json_mode):
        assert "SRC:" in messages[-1]["content"]
        assert "DRF:" in messages[-1]["content"]
        return json.dumps(
            {
                "patches": [
                    {
                        "index": 1,
                        "reason": "negation_scope",
                        "text": "Do not treat it as casual talk and keep reciting it!",
                    },
                    {
                        "index": 2,
                        "reason": "dropped_marker_should_be_rejected",
                        "text": "He dropped the formula completely.",
                    },
                ]
            }
        )

    cfg = Config.from_dict(
        {"language": {"source": "zh-Hant", "target": "en"}, "llm": {"preset": "fake"}}
    )
    editor = SparseSemanticEditor(FakeClient(handler=handler), cfg)
    editor.src = "zh-Hant"
    editor.tgt = "en"
    res = editor.edit_batch(
        ["第一段很好。", "你也莫當常言，只管念誦。", "第三段有公式。"],
        [
            "The first paragraph is already great.",
            "Do not treat it lightly—just keep reciting.",
            "The third paragraph has ⟦OBJ:1⟧.",
        ],
    )
    assert not res.failed
    assert res.patched_indices == (1,)
    assert res.targets[0] == "The first paragraph is already great."
    assert res.targets[1] == "Do not treat it as casual talk and keep reciting it!"
    # Patch 2 tried to drop ⟦OBJ:1⟧ and was safely rejected.
    assert res.targets[2] == "The third paragraph has ⟦OBJ:1⟧."


def test_entity_markers_mask_and_unmask_possessives():
    terms = [
        GlossaryTerm(source="孫悟空", target="Sun Wukong", type="person", gender="male"),
        GlossaryTerm(source="悟空", target="Wukong", type="person", gender="male"),
        GlossaryTerm(source="花果山", target="Flower-Fruit Mountain", type="place"),
    ]
    plan = mask_batch_entities(["孫悟空回到了花果山，悟空很高興。"], terms)
    assert plan.active
    assert "⟦N0⟧" in plan.masked_sources[0]
    assert "bare name, no 'the'" in plan.legend_block
    assert "孫悟空" not in plan.masked_sources[0]
    assert "花果山" not in plan.masked_sources[0]
    # Simulate model output using the markers + possessive.
    model_out = ["⟦N0⟧ returned to ⟦N1⟧, and ⟦N2⟧'s heart was glad."]
    filled, filled_n, unresolved_n = unmask_batch_entities(model_out, plan)
    assert unresolved_n == 0
    assert filled_n == 3
    assert filled[0] == "Sun Wukong returned to Flower-Fruit Mountain, and Wukong's heart was glad."


def test_parallel_compiled_pipeline_is_order_independent_and_resumable(tmp_path: Path):
    """Chapter-parallel drafting with compiled Book Bible produces identical saved targets
    regardless of worker completion order, uses positional source lookbehind, skips per-batch
    extraction, and reuses the bible cache on resume.
    """
    book = tmp_path / "novel.txt"
    book.write_text(
        "# 第一章\n\n行者跟隨三藏走出長安。\n\n行者笑著說道：前面有山。\n\n"
        "# 第二章\n\n三藏問行者：此山何名？\n\n行者答道：這是兩界山。\n\n"
        "# 第三章\n\n行者護送三藏過了兩界山。\n\n三藏合掌稱謝。\n",
        encoding="utf-8",
    )

    def run_once(run_name: str, *, reverse_delays: bool):
        extract_calls = 0
        bible_calls = 0
        contexts_seen: dict[str, str] = {}
        active_threads: set[int] = set()
        lock = threading.Lock()

        def handler(messages, tier, json_mode):
            nonlocal extract_calls, bible_calls
            sys_msg = messages[0]["content"]
            user_msg = messages[-1]["content"]
            if "pre-translation analyst" in sys_msg:
                return json.dumps({"style_guide": "Classical literary tone.", "characters": []})
            if "chapter digest writer" in sys_msg:
                return "行者 and 三藏 travel toward 兩界山."
            if "whole-book synopsis writer" in sys_msg:
                return "行者 escorts 三藏 on a journey."
            if "terminology prober" in sys_msg:
                bible_calls += 1
                return json.dumps(
                    {
                        "renderings": {
                            "行者": "Pilgrim" if bible_calls == 1 else "Xingzhe",
                            "三藏": "Sanzang",
                        }
                    }
                )
            if "Book Bible" in sys_msg:
                bible_calls += 1
                return json.dumps(
                    {
                        "terms": [
                            {
                                "source": "行者",
                                "target": "Pilgrim",
                                "type": "person",
                                "gender": "male",
                            },
                            {
                                "source": "三藏",
                                "target": "Sanzang",
                                "type": "person",
                                "gender": "male",
                            },
                        ]
                    }
                )
            if "extractor" in sys_msg:
                extract_calls += 1
                return '{"terms":[]}'
            if "literary translator" in sys_msg:
                with lock:
                    active_threads.add(threading.get_ident())
                if reverse_delays and "兩界山" in user_msg:
                    time.sleep(0.05)
                elif not reverse_delays and "長安" in user_msg:
                    time.sleep(0.05)
                n = len(re.findall(r"^\[\d+\]", user_msg, re.MULTILINE))
                # Record context received by each chapter.
                if "此山何名" in user_msg:
                    contexts_seen["ch1"] = user_msg
                return json.dumps(
                    {"translations": [f"Pilgrim and Sanzang ({i}) -- ok." for i in range(n)]}
                )
            if "bilingual literary editor" in sys_msg:
                return json.dumps({"patches": []})
            if "chapter title translator" in sys_msg:
                n = len(re.findall(r"^\[\d+\]", user_msg, re.MULTILINE))
                return json.dumps({"titles": [f"Chapter {i + 1}" for i in range(n)]})
            return "{}"

        cfg = Config.from_dict(
            {
                "language": {"source": "zh-Hant", "target": "en"},
                "llm": {"preset": "fake"},
                "paths": {"state_dir": str(tmp_path / run_name)},
                "pipeline": {
                    "glossary_mode": "compiled",
                    "chapter_concurrency": 3,
                    "polish": True,
                    "polish_mode": "sparse",
                    "deterministic_hygiene": True,
                    "review": False,
                    "review_autofix": False,
                },
            }
        )
        orch = Orchestrator(cfg, client=FakeClient(handler=handler))
        store = orch.run(str(book))
        targets_by_ch = {
            ci: [s.target for s in store.load_chapter(ci).text_segments] for ci in range(3)
        }
        return store, targets_by_ch, extract_calls, bible_calls, contexts_seen

    store_a, targets_a, ext_a, bible_a, ctx_a = run_once("run-a", reverse_delays=False)
    store_b, targets_b, ext_b, bible_b, ctx_b = run_once("run-b", reverse_delays=True)

    # Zero hot-path per-batch extraction calls in compiled mode.
    assert ext_a == 0 and ext_b == 0
    assert bible_a == 3 and bible_b == 3
    # Completion order never changes chapter content.
    assert targets_a == targets_b
    # Deterministic hygiene fixed '--' -> '—'.
    assert all("—" in t and "--" not in t for segs in targets_a.values() for t in segs)
    # Chapter 1 opening context received deterministic positional source lookbehind (not translated targets).
    assert "Preceding chapter closing source" in ctx_a["ch1"]
    assert "行者笑著說道" in ctx_a["ch1"]

    # Re-running on existing state reuses glossary_bible.json artifact without calling bible LLM again.
    resumed_calls = 0

    def resume_handler(messages, tier, json_mode):
        nonlocal resumed_calls
        resumed_calls += 1
        return "{}"

    cfg_resume = Config.from_dict(
        {
            "language": {"source": "zh-Hant", "target": "en"},
            "llm": {"preset": "fake"},
            "paths": {"state_dir": str(tmp_path / "run-a")},
            "pipeline": {
                "glossary_mode": "compiled",
                "chapter_concurrency": 3,
                "polish": True,
                "polish_mode": "sparse",
                "review": False,
                "review_autofix": False,
            },
        }
    )
    Orchestrator(cfg_resume, client=FakeClient(handler=resume_handler)).run(str(book))
    assert resumed_calls == 0


def test_benchmark_metrics_script_analyzes_completed_run(tmp_path: Path):
    """Verify scripts/benchmark_metrics.py parses a real Orchestrator run cleanly."""
    import importlib.util

    book = tmp_path / "mini.txt"
    book.write_text("# 第一章\n\n行者笑著說道：走吧。\n\n行者又說：快走。\n", encoding="utf-8")

    def handler(messages, tier, json_mode):
        sys_msg = messages[0]["content"]
        user_msg = messages[-1]["content"]
        if "pre-translation analyst" in sys_msg:
            return json.dumps({"style_guide": "Crisp.", "characters": []})
        if "chapter digest writer" in sys_msg:
            return "行者 speaks."
        if "whole-book synopsis writer" in sys_msg:
            return "行者 travels."
        if "terminology prober" in sys_msg:
            return json.dumps({"renderings": {"行者": "Pilgrim"}})
        if "Book Bible" in sys_msg:
            return json.dumps(
                {"terms": [{"source": "行者", "target": "Pilgrim", "type": "person"}]}
            )
        if "literary translator" in sys_msg:
            n = len(re.findall(r"^\[\d+\]", user_msg, re.MULTILINE))
            return json.dumps({"translations": ['Pilgrim said: "Let us go -- now!"'] * n})
        if "chapter title translator" in sys_msg:
            n = len(re.findall(r"^\[\d+\]", user_msg, re.MULTILINE))
            return json.dumps({"titles": [f"Chapter {i + 1}" for i in range(n)]})
        return "{}"

    cfg = Config.from_dict(
        {
            "language": {"source": "zh-Hant", "target": "en"},
            "llm": {"preset": "fake"},
            "paths": {"state_dir": str(tmp_path / "state")},
            "pipeline": {
                "glossary_mode": "compiled",
                "polish": False,
                "review": False,
                "deterministic_hygiene": True,
            },
        }
    )
    Orchestrator(cfg, client=FakeClient(handler=handler)).run(str(book))

    script_path = Path(__file__).resolve().parents[3] / "scripts" / "benchmark_metrics.py"
    spec = importlib.util.spec_from_file_location("benchmark_metrics", script_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    report = mod.analyze_run(str(tmp_path))
    assert report["segments_translated"] == 2
    assert report["glossary_entries"] >= 1
    assert report["formatting_lint"]["colon_tags"] == 0
    assert report["formatting_lint"]["raw_double_dash"] == 0
    assert report["post_normalized"]["typography_fixes"] >= 1
