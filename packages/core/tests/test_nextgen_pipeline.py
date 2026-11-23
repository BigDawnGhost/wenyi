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


def test_book_bible_snippets_follow_normalized_offsets_after_readings():
    from wenyi_core.agents.book_bible import _extract_distant_snippets, _match_index

    corpus = (
        "漢〘かん〙字〘じ〙" * 30 + "孫悟空が来た。" + "平〘へい〙和〘わ〙" * 30 + "孫悟空が笑う。"
    )
    snip_a, snip_b = _extract_distant_snippets("孫悟空", corpus, _match_index(corpus))
    assert "孫悟空が来た" in snip_a and "孫悟空が笑う" in snip_b


def test_book_bible_snippets_keep_the_original_case():
    from wenyi_core.agents.book_bible import _extract_distant_snippets, _match_index

    corpus = "Then Will said hello. " + "\ufb01ne words. " * 10 + "Will left the Hall."
    snip_a, snip_b = _extract_distant_snippets("will", corpus, _match_index(corpus))
    assert "Will said hello" in snip_a
    assert "Will left the Hall" in snip_b


def _bible_agent(handler, target: str = "en") -> BookBibleAgent:
    cfg = Config.from_dict(
        {
            "language": {"source": "zh-Hant", "target": target},
            "llm": {"preset": "fake"},
            "pipeline": {"glossary_mode": "compiled"},
        }
    )
    agent = BookBibleAgent(FakeClient(handler=handler), cfg)
    agent.src = "zh-Hant"
    agent.tgt = target
    return agent


def test_book_bible_adjudication_ignores_malformed_aliases_and_targets_the_configured_language():
    systems: list[str] = []

    def handler(messages, tier, json_mode):
        systems.append(messages[0]["content"])
        return json.dumps(
            {
                "terms": [
                    {"source": "行者", "target": "Pélerin", "type": "person", "aliases": 1},
                    {"source": "三藏", "target": "Sanzang", "type": "person", "aliases": "三"},
                ]
            }
        )

    agent = _bible_agent(handler, target="fr")
    from wenyi_core.agents.book_bible import BibleCandidate

    candidates = [
        (BibleCandidate("行者", 5, True, "", ""), "Pélerin", "Pèlerin", True),
        (BibleCandidate("三藏", 5, True, "", ""), "Sanzang", "Sanzang", False),
    ]
    terms = agent._adjudicate(candidates, book_synopsis="", style="", existing_terms=[])
    assert {t.source: t.aliases for t in terms} == {"行者": [], "三藏": []}
    assert "target English" not in systems[0]
    assert "(fr)" in systems[0]


def test_bible_fingerprint_changes_with_the_style_brief():
    from wenyi_core.pipeline.translate.bible_seed import _bible_fingerprint

    cfg = Config.from_dict(
        {"language": {"source": "zh", "target": "en"}, "llm": {"preset": "fake"}}
    )
    a = _bible_fingerprint(cfg, "sha", ["d"], "syn", "Terse.")
    b = _bible_fingerprint(cfg, "sha", ["d"], "syn", "Ornate.")
    assert a != b


def test_entity_markers_match_whole_words_in_space_delimited_sources():
    terms = [GlossaryTerm(source="Ann", target="Anne", type="person", gender="female")]
    plan = mask_batch_entities(["Anna met Ann at the Annual fair."], terms)
    assert plan.masked_sources == ("Anna met ⟦N0⟧ at the Annual fair.",)
    filled, _, unresolved = unmask_batch_entities(["Anna a vu ⟦N0⟧ à la foire."], plan)
    assert filled == ["Anna a vu Anne à la foire."]
    assert unresolved == 0


def test_entity_markers_count_dropped_markers_as_unresolved():
    terms = [GlossaryTerm(source="孫悟空", target="Sun Wukong", type="person")]
    plan = mask_batch_entities(["孫悟空回來了。"], terms)
    _, _, unresolved = unmask_batch_entities(["He came back."], plan)
    assert unresolved == 1


def test_entity_markers_accept_a_pronoun_for_a_repeated_name():
    terms = [GlossaryTerm(source="孫悟空", target="Sun Wukong", type="person")]
    plan = mask_batch_entities(["孫悟空說了話。", "孫悟空笑了。"], terms)
    filled, _, unresolved = unmask_batch_entities(["⟦N0⟧ spoke.", "He laughed."], plan)
    assert filled == ["Sun Wukong spoke.", "He laughed."]
    assert unresolved == 0
    _, _, unknown = unmask_batch_entities(["⟦N0⟧ spoke.", "⟦N7⟧ laughed."], plan)
    assert unknown > 0
    _, _, half = unmask_batch_entities(["⟦N0⟧ spoke.", "N0⟧ laughed."], plan)
    assert half > 0


def test_batch_translation_retranslates_plain_source_when_a_marker_is_dropped():
    from wenyi_core.agents.translator import Translator
    from wenyi_core.pipeline.translate.translation_batch import BatchPlan, TranslationBatchExecutor

    user_messages: list[str] = []

    def handler(messages, tier, json_mode):
        user = messages[-1]["content"]
        user_messages.append(user)
        if "⟦N0⟧" in user:
            return json.dumps({"translations": ["He came back."]})
        return json.dumps({"translations": ["Sun Wukong came back."]})

    cfg = Config.from_dict(
        {"language": {"source": "zh-Hant", "target": "en"}, "llm": {"preset": "fake"}}
    )
    translator = Translator(FakeClient(handler=handler), cfg)
    executor = TranslationBatchExecutor(translator, None)
    plan = BatchPlan(
        chapter=0,
        start_index=0,
        segment_indices=(0,),
        sources=("孫悟空回來了。",),
        terms=(GlossaryTerm(source="孫悟空", target="Sun Wukong", type="person"),),
        context="",
        style="",
        book_synopsis="",
        chapter_digest="",
        annotation_contexts=[[]],
        next_source="",
        allow_empty_translations=False,
        entity_markers=True,
    )
    draft = executor.translate(plan)
    assert draft.targets == ("Sun Wukong came back.",)
    assert len(user_messages) == 2
    assert "⟦N0⟧" not in user_messages[1]


_PARALLEL_BOOK = (
    "# 第一章\n\n行者跟隨三藏走出長安。\n\n行者笑著說道：前面有山。\n\n"
    "# 第二章\n\n三藏問行者：此山何名？\n\n行者答道：這是兩界山。\n\n"
    "# 第三章\n\n行者護送三藏過了兩界山。\n\n三藏合掌稱謝。\n"
)


def _parallel_handler(translated: list[str], *, fail_on: str | None = None):
    def handler(messages, tier, json_mode):
        sys_msg = messages[0]["content"]
        user_msg = messages[-1]["content"]
        if "pre-translation analyst" in sys_msg:
            return json.dumps({"style_guide": "Plain.", "characters": []})
        if "chapter digest writer" in sys_msg:
            return "行者 and 三藏 travel."
        if "whole-book synopsis writer" in sys_msg:
            return "A journey."
        if "terminology prober" in sys_msg:
            return json.dumps({"renderings": {}})
        if "Book Bible" in sys_msg:
            return json.dumps({"terms": []})
        if "literary translator" in sys_msg:
            if fail_on is not None and f"] {fail_on}" in user_msg:
                raise RuntimeError("provider down")
            n = len(re.findall(r"^\[\d+\]", user_msg, re.MULTILINE))
            translated.append(user_msg)
            return json.dumps({"translations": [f"Draft {i}." for i in range(n)]})
        if "chapter title translator" in sys_msg:
            n = len(re.findall(r"^\[\d+\]", user_msg, re.MULTILINE))
            return json.dumps({"titles": [f"Chapter {i + 1}" for i in range(n)]})
        return "{}"

    return handler


def _parallel_config(tmp_path: Path, **pipeline) -> Config:
    return Config.from_dict(
        {
            "language": {"source": "zh-Hant", "target": "en"},
            "llm": {"preset": "fake"},
            "paths": {"state_dir": str(tmp_path / "state")},
            # One paragraph per batch, so a chapter has several batches in one wave.
            "segment": {"max_tokens_per_batch": 1},
            "pipeline": {
                "glossary_mode": "compiled",
                "chapter_concurrency": 3,
                "polish": False,
                "review": False,
                "review_autofix": False,
                **pipeline,
            },
        }
    )


def test_parallel_failure_saves_every_drafted_batch_and_resume_skips_them(tmp_path: Path):
    import pytest

    book = tmp_path / "novel.txt"
    book.write_text(_PARALLEL_BOOK, encoding="utf-8")
    first: list[str] = []
    cfg = _parallel_config(tmp_path)
    with pytest.raises(Exception, match="provider down"):
        Orchestrator(
            cfg, client=FakeClient(handler=_parallel_handler(first, fail_on="行者答道"))
        ).run(str(book))

    resumed: list[str] = []
    store = Orchestrator(cfg, client=FakeClient(handler=_parallel_handler(resumed))).run(str(book))
    # Only the failed batch is translated again; batches drafted before and after it are kept.
    assert len(resumed) == 1
    assert "] 行者答道" in resumed[0]
    for ci in range(3):
        assert all(s.target for s in store.load_chapter(ci).text_segments)


def test_parallel_chapters_need_compiled_glossary(tmp_path: Path):
    book = tmp_path / "novel.txt"
    book.write_text(_PARALLEL_BOOK, encoding="utf-8")
    cfg = _parallel_config(tmp_path, glossary_mode="incremental")
    orch = Orchestrator(cfg, client=FakeClient(handler=_parallel_handler([])))
    calls: list[int] = []
    original = orch._translation._run_parallel_chapters

    def spy(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    orch._translation._run_parallel_chapters = spy
    orch.run(str(book))
    assert calls == []


def test_parallel_resume_repolishes_drafts_whose_polish_failed(tmp_path: Path):
    from wenyi_core.pipeline.translate.translation import POLISH_FAILED

    book = tmp_path / "novel.txt"
    book.write_text(_PARALLEL_BOOK, encoding="utf-8")
    base = _parallel_handler([])

    def drafting(messages, tier, json_mode):
        if "bilingual literary editor" in messages[0]["content"]:
            raise RuntimeError("editor down")
        if "literary translator" in messages[0]["content"]:
            n = len(re.findall(r"^\[\d+\]", messages[-1]["content"], re.MULTILINE))
            # Hygiene rewrites "--", so the published text differs from the raw draft.
            return json.dumps({"translations": [f"Draft -- {i}." for i in range(n)]})
        return base(messages, tier, json_mode)

    cfg = _parallel_config(tmp_path, polish=True, polish_mode="sparse")
    store = Orchestrator(cfg, client=FakeClient(handler=drafting)).run(str(book))
    first = store.load_chapter(1).text_segments
    assert all(s.meta.get(POLISH_FAILED) for s in first)
    assert [s.target for s in first] == ["Draft—0."] * len(first)

    edits: list[str] = []

    def editing(messages, tier, json_mode):
        if "bilingual literary editor" in messages[0]["content"]:
            edits.append(messages[-1]["content"])
            return json.dumps({"patches": [{"index": 0, "reason": "fix", "text": "Polished."}]})
        return base(messages, tier, json_mode)

    store = Orchestrator(cfg, client=FakeClient(handler=editing)).run(str(book))
    assert edits
    for ci in range(3):
        segments = store.load_chapter(ci).text_segments
        assert not any(s.meta.get(POLISH_FAILED) for s in segments)
        assert all(s.target == "Polished." for s in segments)


def test_parallel_worker_gives_non_body_batches_no_context(tmp_path: Path):
    from wenyi_core.pipeline.translate.translation import _ParallelWaveSpec, _WaveBatchSpec
    from wenyi_core.pipeline.translate.translation_batch import BatchResult

    orch = Orchestrator(
        _parallel_config(tmp_path), client=FakeClient(handler=_parallel_handler([]))
    )
    service = orch._translation
    plans = []

    def execute(plan, *, polish, polish_mode):
        plans.append(plan)
        return BatchResult(tuple(f"T{i}" for i in plan.segment_indices), (None,))

    service._batches.execute = execute

    def batch(start: int, body: bool) -> _WaveBatchSpec:
        return _WaveBatchSpec(
            batch_start=start,
            segment_indices=(start,),
            sources=(f"S{start}",),
            terms=(),
            annotation_contexts=[[]],
            next_source="",
            already_done=False,
            existing_targets=(None,),
            body_context=body,
        )

    spec = _ParallelWaveSpec(
        chapter=0,
        wave_index=0,
        chapter_digest="DIGEST",
        opening_source_lookbehind="LOOKBEHIND",
        style="",
        book_synopsis="SYNOPSIS",
        batches=(batch(0, True), batch(1, False), batch(2, True)),
        quote_references_by_batch=("", "", ""),
        polish=False,
        polish_mode="full",
        entity_markers=False,
        rolling_context_tokens=1000,
    )
    _, _, outcomes, error = service._translate_wave_worker(spec)
    assert error is None and len(outcomes) == 3
    body, caption, after = plans
    assert body.context == "LOOKBEHIND" and body.book_synopsis == "SYNOPSIS"
    assert (caption.context, caption.book_synopsis, caption.chapter_digest) == ("", "", "")
    # The caption's draft never enters later rolling context.
    assert "T0" in after.context and "T1" not in after.context


def test_compiled_only_chapter_compiles_from_every_chapter_digest(tmp_path: Path):
    book = tmp_path / "novel.txt"
    book.write_text(_PARALLEL_BOOK, encoding="utf-8")
    orch = Orchestrator(
        _parallel_config(tmp_path), client=FakeClient(handler=_parallel_handler([]))
    )
    seen: list[int] = []
    original = orch._translation._bible_seed.ensure_seeded

    def spy(store, *, digests, **kwargs):
        seen.append(len(digests))
        return original(store, digests=digests, **kwargs)

    orch._translation._bible_seed.ensure_seeded = spy
    orch.run(str(book), only_chapter=1)
    assert seen == [3]


def _count_translations(handler, calls: list[str]):
    def counting(messages, tier, json_mode):
        if "literary translator" in messages[0]["content"]:
            calls.append(messages[-1]["content"])
        return handler(messages, tier, json_mode)

    return counting


def test_parallel_writer_error_saves_finished_waves_and_starts_no_more(tmp_path: Path):
    import pytest
    from wenyi_core.storage.file import FileStorage

    book = tmp_path / "novel.txt"
    book.write_text(_PARALLEL_BOOK, encoding="utf-8")
    cfg = _parallel_config(tmp_path, chapter_concurrency=2)
    calls: list[str] = []
    orch = Orchestrator(
        cfg, client=FakeClient(handler=_count_translations(_parallel_handler([]), calls))
    )
    real_save = FileStorage.save_chapter_with_status

    def failing_publish(self, chapter, status):
        if chapter.index == 0:
            raise OSError("disk full")
        return real_save(self, chapter, status)

    FileStorage.save_chapter_with_status = failing_publish
    try:
        with pytest.raises(OSError, match="disk full"):
            orch.run(str(book))
    finally:
        FileStorage.save_chapter_with_status = real_save
    spent = len(calls)

    resumed: list[str] = []
    store = Orchestrator(
        cfg, client=FakeClient(handler=_count_translations(_parallel_handler([]), resumed))
    ).run(str(book))
    # Every batch translated before the failure was saved, so none is paid for twice.
    assert spent + len(resumed) == 9
    for ci in range(3):
        assert all(s.target for s in store.load_chapter(ci).text_segments)


def test_parallel_worker_stops_its_wave_on_a_polish_interrupt(tmp_path: Path):
    from wenyi_core.llm.limits import RequestStopped
    from wenyi_core.pipeline.translate.translation import _ParallelWaveSpec, _WaveBatchSpec
    from wenyi_core.pipeline.translate.translation_batch import BatchResult

    orch = Orchestrator(
        _parallel_config(tmp_path), client=FakeClient(handler=_parallel_handler([]))
    )
    service = orch._translation
    stop = RequestStopped("budget exhausted")
    plans = []

    def execute(plan, *, polish, polish_mode):
        plans.append(plan)
        return BatchResult(("T",), ("T",), polish_failed=True, polish_interrupted=stop)

    service._batches.execute = execute
    batches = tuple(
        _WaveBatchSpec(
            batch_start=i,
            segment_indices=(i,),
            sources=(f"S{i}",),
            terms=(),
            annotation_contexts=[[]],
            next_source="",
            already_done=False,
            existing_targets=(None,),
        )
        for i in range(3)
    )
    spec = _ParallelWaveSpec(
        chapter=0,
        wave_index=0,
        chapter_digest="",
        opening_source_lookbehind="",
        style="",
        book_synopsis="",
        batches=batches,
        quote_references_by_batch=("", "", ""),
        polish=True,
        polish_mode="full",
        entity_markers=False,
        rolling_context_tokens=1000,
    )
    _, _, outcomes, error = service._translate_wave_worker(spec)
    # The interrupted batch is returned for saving, the error stops the run, and no
    # further batch is translated.
    assert error is stop
    assert len(plans) == 1 and len(outcomes) == 1


def test_book_bible_adjudication_failure_raises_instead_of_caching_nothing():
    import pytest
    from wenyi_core.agents.book_bible import BibleCandidate

    agent = _bible_agent(lambda messages, tier, json_mode: json.dumps({"oops": 1}))
    candidates = [(BibleCandidate("行者", 5, True, "", ""), "Pilgrim", "Xingzhe", True)]
    with pytest.raises(ValueError, match="no terms list"):
        agent._adjudicate(candidates, book_synopsis="", style="", existing_terms=[])


def test_bible_candidates_mine_cyrillic_names():
    digests = ["Иван встретил Марию у реки."]
    corpus = "Иван пришёл. " * 6 + "Мария ждала. " * 6
    sources = {c.source for c in mine_bible_candidates(digests, corpus, source_lang="ru")}
    assert "Иван" in sources


def test_entity_markers_leave_sources_with_literal_marker_brackets_unmasked():
    terms = [GlossaryTerm(source="孫悟空", target="Sun Wukong", type="person")]
    plan = mask_batch_entities(["孫悟空寫下⟦N0⟧。"], terms)
    assert not plan.active


def test_only_chapter_indexes_quotes_from_the_whole_book(tmp_path: Path):
    from unittest.mock import patch

    from wenyi_core.pipeline.translate import translation as translation_module

    book = tmp_path / "novel.txt"
    book.write_text(_PARALLEL_BOOK, encoding="utf-8")
    orch = Orchestrator(
        _parallel_config(tmp_path), client=FakeClient(handler=_parallel_handler([]))
    )
    seen: list[list[int]] = []
    original = translation_module.QuoteMemory.from_chapters

    def spy(chapters):
        seen.append([ci for ci, _ in chapters])
        return original(chapters)

    with patch.object(translation_module.QuoteMemory, "from_chapters", side_effect=spy):
        orch.run(str(book), only_chapter=1)
    assert seen == [[0, 1, 2]]


def test_book_bible_snippets_match_decomposed_accents():
    from wenyi_core.agents.book_bible import _extract_distant_snippets, _match_index

    corpus = "José rode north. " + "filler text. " * 10 + "José came home."
    snip_a, snip_b = _extract_distant_snippets("José", corpus, _match_index(corpus))
    assert "rode north" in snip_a and "came home" in snip_b


def test_parallel_worker_translates_nothing_after_the_run_stops(tmp_path: Path):
    import threading

    from wenyi_core.pipeline.translate.translation import _ParallelWaveSpec, _WaveBatchSpec

    orch = Orchestrator(
        _parallel_config(tmp_path), client=FakeClient(handler=_parallel_handler([]))
    )
    service = orch._translation
    plans = []
    service._batches.execute = lambda plan, **_: plans.append(plan)
    stop = threading.Event()
    stop.set()
    batch = _WaveBatchSpec(
        batch_start=0,
        segment_indices=(0,),
        sources=("S0",),
        terms=(),
        annotation_contexts=[[]],
        next_source="",
        already_done=False,
        existing_targets=(None,),
    )
    spec = _ParallelWaveSpec(
        chapter=0,
        wave_index=0,
        chapter_digest="",
        opening_source_lookbehind="",
        style="",
        book_synopsis="",
        batches=(batch,),
        quote_references_by_batch=("",),
        polish=False,
        polish_mode="full",
        entity_markers=False,
        rolling_context_tokens=1000,
    )
    _, _, outcomes, error = service._translate_wave_worker(spec, None, stop)
    assert plans == [] and outcomes == [] and error is None


def test_marker_retry_that_echoes_a_marker_is_filled_and_not_continued():
    from wenyi_core.agents.translator import Translator
    from wenyi_core.pipeline.translate.translation_batch import BatchPlan, TranslationBatchExecutor

    def handler(messages, tier, json_mode):
        if "⟦N0⟧" in messages[-1]["content"]:
            return json.dumps({"translations": ["He came back."]})
        return json.dumps({"translations": ["⟦N0⟧ came back."]})

    cfg = Config.from_dict(
        {"language": {"source": "zh-Hant", "target": "en"}, "llm": {"preset": "fake"}}
    )
    executor = TranslationBatchExecutor(Translator(FakeClient(handler=handler), cfg), None)
    plan = BatchPlan(
        chapter=0,
        start_index=0,
        segment_indices=(0,),
        sources=("孫悟空回來了。",),
        terms=(GlossaryTerm(source="孫悟空", target="Sun Wukong", type="person"),),
        context="",
        style="",
        book_synopsis="",
        chapter_digest="",
        annotation_contexts=[[]],
        next_source="",
        allow_empty_translations=False,
        entity_markers=True,
    )
    draft = executor.translate(plan)
    assert draft.targets == ("Sun Wukong came back.",)
    assert draft.turn is None


def test_parallel_worker_returns_drafted_batches_on_a_budget_stop(tmp_path: Path):
    from wenyi_core.llm.limits import RequestStopped
    from wenyi_core.pipeline.translate.translation import _ParallelWaveSpec, _WaveBatchSpec
    from wenyi_core.pipeline.translate.translation_batch import BatchResult

    orch = Orchestrator(
        _parallel_config(tmp_path), client=FakeClient(handler=_parallel_handler([]))
    )
    service = orch._translation
    stop = RequestStopped("budget exhausted")
    calls = []

    def execute(plan, *, polish, polish_mode):
        calls.append(plan)
        if len(calls) == 2:
            raise stop
        return BatchResult(("T",), (None,))

    service._batches.execute = execute
    batches = tuple(
        _WaveBatchSpec(
            batch_start=i,
            segment_indices=(i,),
            sources=(f"S{i}",),
            terms=(),
            annotation_contexts=[[]],
            next_source="",
            already_done=False,
            existing_targets=(None,),
        )
        for i in range(3)
    )
    spec = _ParallelWaveSpec(
        chapter=0,
        wave_index=0,
        chapter_digest="",
        opening_source_lookbehind="",
        style="",
        book_synopsis="",
        batches=batches,
        quote_references_by_batch=("", "", ""),
        polish=False,
        polish_mode="full",
        entity_markers=False,
        rolling_context_tokens=1000,
    )
    _, _, outcomes, error = service._translate_wave_worker(spec)
    assert error is stop
    assert len(outcomes) == 1 and len(calls) == 2


def test_parallel_interrupt_mid_wave_still_saves_the_rest_of_the_wave(tmp_path: Path):
    import pytest
    from wenyi_core.llm.limits import RequestStopped

    book = tmp_path / "novel.txt"
    book.write_text(_PARALLEL_BOOK, encoding="utf-8")
    cfg = _parallel_config(tmp_path)
    calls: list[str] = []
    orch = Orchestrator(
        cfg, client=FakeClient(handler=_count_translations(_parallel_handler([]), calls))
    )
    service = orch._translation
    real_align = service._align_batch_context
    fired: list[int] = []

    def align(store, chapter, segments, start, count):
        if chapter.index == 0 and start == 1 and not fired:
            fired.append(1)
            raise RequestStopped("cancelled")
        return real_align(store, chapter, segments, start, count)

    service._align_batch_context = align
    with pytest.raises(RequestStopped):
        orch.run(str(book))
    spent = len(calls)

    resumed: list[str] = []
    store = Orchestrator(
        cfg, client=FakeClient(handler=_count_translations(_parallel_handler([]), resumed))
    ).run(str(book))
    assert spent + len(resumed) == 9
    for ci in range(3):
        assert all(s.target for s in store.load_chapter(ci).text_segments)


def test_parallel_failed_batch_save_keeps_the_rest_of_the_wave(tmp_path: Path):
    import pytest
    from wenyi_core.storage.file import FileStorage

    book = tmp_path / "novel.txt"
    book.write_text(_PARALLEL_BOOK, encoding="utf-8")
    cfg = _parallel_config(tmp_path)
    calls: list[str] = []
    orch = Orchestrator(
        cfg, client=FakeClient(handler=_count_translations(_parallel_handler([]), calls))
    )
    real_save = FileStorage.save_chapter
    fired: list[int] = []

    def flaky_save(self, chapter):
        if chapter.index == 0 and chapter.text_segments[1].target and not fired:
            fired.append(1)
            raise OSError("disk hiccup")
        return real_save(self, chapter)

    FileStorage.save_chapter = flaky_save
    try:
        with pytest.raises(OSError, match="disk hiccup"):
            orch.run(str(book))
    finally:
        FileStorage.save_chapter = real_save
    spent = len(calls)

    resumed: list[str] = []
    Orchestrator(
        cfg, client=FakeClient(handler=_count_translations(_parallel_handler([]), resumed))
    ).run(str(book))
    # The wave's later batches are saved by the final flush, so nothing is paid twice.
    assert fired and spent + len(resumed) == 9
