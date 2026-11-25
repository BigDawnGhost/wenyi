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

import pytest
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
                keys = re.findall(r"^\d+\.\s+(.+?)\s+\|", user_msg, re.MULTILINE)
                renderings = {k: "" for k in keys}
                renderings["行者"] = "Pilgrim" if bible_calls == 1 else "Xingzhe"
                renderings["三藏"] = "Sanzang"
                return json.dumps({"renderings": renderings})
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
            if "senior bilingual editor" in sys_msg:
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
    # Deterministic hygiene never rewrites the model's punctuation ('--' is kept as drafted).
    assert all(t.endswith("-- ok.") for segs in targets_a.values() for t in segs)
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
    # Hygiene leaves the model's punctuation alone, so the lint still sees it.
    assert report["formatting_lint"]["colon_tags"] == 2
    assert report["formatting_lint"]["raw_double_dash"] == 2


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
            keys = re.findall(r"^\d+\.\s+(.+?)\s+\|", user_msg, re.MULTILINE)
            return json.dumps({"renderings": {k: "" for k in keys}})
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
    # Every batch drafted before the stop was saved and skipped; resume translates only what
    # failed or was stopped early, and never pays for any batch twice.
    assert len(first) + len(resumed) == 9
    assert any("] 行者答道" in msg for msg in resumed)
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
        if "senior bilingual editor" in messages[0]["content"]:
            raise RuntimeError("editor down")
        if "literary translator" in messages[0]["content"]:
            n = len(re.findall(r"^\[\d+\]", messages[-1]["content"], re.MULTILINE))
            # Hygiene repairs the leaked escape, so the published text differs from the raw draft.
            return json.dumps({"translations": [f"Draft\\n{i}." for i in range(n)]})
        return base(messages, tier, json_mode)

    cfg = _parallel_config(tmp_path, polish=True, polish_mode="sparse")
    store = Orchestrator(cfg, client=FakeClient(handler=drafting)).run(str(book))
    first = store.load_chapter(1).text_segments
    assert all(s.meta.get(POLISH_FAILED) for s in first)
    assert [s.target for s in first] == ["Draft\n0."] * len(first)

    edits: list[str] = []

    def editing(messages, tier, json_mode):
        if "senior bilingual editor" in messages[0]["content"]:
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
    _, _, outcomes, error, completed = service._translate_wave_worker(spec)
    assert error is None and completed and len(outcomes) == 3
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
    _, _, outcomes, error, completed = service._translate_wave_worker(spec)
    # The interrupted batch is returned for saving, the error stops the run, and no
    # further batch is translated.
    assert error is stop and not completed
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
    _, _, outcomes, error, completed = service._translate_wave_worker(spec, None, stop)
    assert plans == [] and outcomes == [] and error is None and not completed


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
    _, _, outcomes, error, completed = service._translate_wave_worker(spec)
    assert error is stop and not completed
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


def test_parallel_stopped_earlier_wave_is_never_published_incomplete(tmp_path: Path):
    import pytest
    from wenyi_core.storage.file import FileStorage

    book = tmp_path / "novel.txt"
    book.write_text(_PARALLEL_BOOK, encoding="utf-8")
    cfg = _parallel_config(tmp_path, chapter_concurrency=2)
    ch0_batch0_done = threading.Event()
    stop_ref: list[threading.Event | None] = [None]
    first_calls: list[str] = []
    base = _parallel_handler(first_calls)

    def handler(messages, tier, json_mode):
        sys_msg = messages[0]["content"]
        user_msg = messages[-1]["content"]
        if "literary translator" in sys_msg:
            if "] 行者跟隨三藏走出長安。" in user_msg:
                res = base(messages, tier, json_mode)
                ch0_batch0_done.set()
                # Hold chapter 0 until chapter 1's failure sets the shared stop event.
                assert stop_ref[0] is not None
                assert stop_ref[0].wait(timeout=5)
                return res
            if "] 三藏問行者：此山何名？" in user_msg:
                assert ch0_batch0_done.wait(timeout=5)
                raise RuntimeError("chapter 1 down")
        return base(messages, tier, json_mode)

    orch = Orchestrator(cfg, client=FakeClient(handler=handler))
    orig_worker = orch._translation._translate_wave_worker

    def wrapped_worker(spec, polish_executor=None, stop=None):
        stop_ref[0] = stop
        return orig_worker(spec, polish_executor, stop)

    orch._translation._translate_wave_worker = wrapped_worker
    with pytest.raises(RuntimeError, match="chapter 1 down"):
        orch.run(str(book))

    # Chapter 0 stopped early: batch 0 was saved, later batches remain None, and chapter 0
    # must NOT be marked done in manifest.json.
    run_dir = next((tmp_path / "state").glob("*/targets/*"))
    store = FileStorage(str(run_dir), create=False)
    manifest = store.load_manifest()
    ch0_row = next(r for r in manifest["chapters"] if r["index"] == 0)
    ch0 = store.load_chapter(0)
    assert ch0.text_segments[0].target is not None
    assert any(s.target is None for s in ch0.text_segments)
    assert ch0_row["status"] != "done"

    resumed_calls: list[str] = []
    store = Orchestrator(cfg, client=FakeClient(handler=_parallel_handler(resumed_calls))).run(
        str(book)
    )
    assert len(first_calls) + len(resumed_calls) == 9
    for ci in range(3):
        assert all(s.target for s in store.load_chapter(ci).text_segments)


def test_parallel_persistent_align_stop_saves_remaining_waves_and_resume_aligns(tmp_path: Path):
    import pytest
    from wenyi_core.llm.limits import RequestStopped

    book = tmp_path / "novel.txt"
    book.write_text(_PARALLEL_BOOK, encoding="utf-8")
    cfg = _parallel_config(tmp_path, chapter_concurrency=3)
    calls: list[str] = []
    orch = Orchestrator(
        cfg, client=FakeClient(handler=_count_translations(_parallel_handler([]), calls))
    )
    service = orch._translation

    # Simulate a persistent budget/cancel stop during alignment (every align call raises).
    def always_stop_align(store, chapter, segments, start, count):
        if chapter.index == 0 and start >= 1:
            raise RequestStopped("budget exhausted")

    service._align_batch_context = always_stop_align
    with pytest.raises(RequestStopped):
        orch.run(str(book))
    spent = len(calls)

    resumed: list[str] = []
    aligned_on_resume: list[tuple[int, int]] = []
    orch2 = Orchestrator(
        cfg, client=FakeClient(handler=_count_translations(_parallel_handler([]), resumed))
    )
    real_align = orch2._translation._align_batch_context

    def record_align(store, chapter, segments, start, count):
        aligned_on_resume.append((chapter.index, start))
        return real_align(store, chapter, segments, start, count)

    orch2._translation._align_batch_context = record_align
    store = orch2.run(str(book))
    # Every drafted batch across all waves was saved despite the persistent align stop.
    assert spent + len(resumed) == 9
    # Chapter 0's already_done batches were aligned on resume before publishing.
    assert (0, 1) in aligned_on_resume
    assert (0, 2) in aligned_on_resume
    for ci in range(3):
        assert all(s.target for s in store.load_chapter(ci).text_segments)


def test_parallel_persistent_save_error_on_one_chapter_still_saves_other_chapters(tmp_path: Path):
    import pytest
    from wenyi_core.storage.file import FileStorage

    book = tmp_path / "novel.txt"
    book.write_text(_PARALLEL_BOOK, encoding="utf-8")
    cfg = _parallel_config(tmp_path, chapter_concurrency=3)
    calls: list[str] = []
    orch = Orchestrator(
        cfg, client=FakeClient(handler=_count_translations(_parallel_handler([]), calls))
    )
    real_save = FileStorage.save_chapter
    ch1_finished = threading.Event()
    orig_worker = orch._translation._translate_wave_worker

    def paced_worker(spec, polish_executor=None, stop=None):
        if spec.chapter == 0:
            ch1_finished.wait(timeout=5)
        result = orig_worker(spec, polish_executor, stop)
        if spec.chapter == 1:
            ch1_finished.set()
        return result

    orch._translation._translate_wave_worker = paced_worker

    def failing_ch0_save(self, chapter):
        if chapter.index == 0 and any(s.target for s in chapter.text_segments):
            raise OSError("ch0 permanently unwritable")
        return real_save(self, chapter)

    FileStorage.save_chapter = failing_ch0_save
    try:
        with pytest.raises(OSError, match="ch0 permanently unwritable"):
            orch.run(str(book))
    finally:
        FileStorage.save_chapter = real_save

    # Chapter 1's finished wave was still saved during final teardown despite chapter 0's error.
    run_dir = next((tmp_path / "state").glob("*/targets/*"))
    store = FileStorage(str(run_dir), create=False)
    assert all(s.target for s in store.load_chapter(1).text_segments)


def test_book_bible_probe_failure_raises_instead_of_caching_empty_bible(caplog):
    import logging

    import pytest

    agent = _bible_agent(lambda messages, tier, json_mode: "NOT JSON")
    digests = ["三藏 gives 孫悟空 the byname 行者."]
    corpus = "行者跟隨三藏。行者大笑。三藏點頭。"
    with pytest.raises(Exception):
        agent.compile_bible(digests, corpus)

    malformed = _bible_agent(lambda messages, tier, json_mode: json.dumps({"oops": 1}))
    with pytest.raises(ValueError, match="no renderings map"):
        malformed.compile_bible(digests, corpus)

    empty_map = _bible_agent(lambda messages, tier, json_mode: json.dumps({"renderings": {}}))
    with pytest.raises(ValueError, match="omitted candidate"):
        empty_map.compile_bible(digests, corpus)

    all_null = _bible_agent(
        lambda messages, tier, json_mode: json.dumps({"renderings": {"行者": None, "三藏": None}})
    )
    with pytest.raises(ValueError, match="non-string rendering"):
        all_null.compile_bible(digests, corpus)

    # Partial stubborn omission or non-string after retry degrades gracefully (warns + drops
    # the unresolved item) instead of aborting the entire book run post-compile.
    def partial_null_handler(messages, tier, json_mode):
        sys_msg = messages[0]["content"]
        if "terminology prober" in sys_msg:
            return json.dumps({"renderings": {"行者": None, "三藏": "Sanzang"}})
        return json.dumps(
            {"terms": [{"source": "三藏", "target": "Sanzang", "type": "person", "gender": "male"}]}
        )

    partial_agent = _bible_agent(partial_null_handler)
    with caplog.at_level(logging.WARNING, logger="wenyi_core.agents.book_bible"):
        res = partial_agent.compile_bible(digests, corpus)
    assert [t.source for t in res.terms] == ["三藏"]
    assert "dropped 1 unresolved candidate" in caplog.text

    # Partial omission where the retry call returns malformed JSON also degrades gracefully
    # when other candidates in the chunk already succeeded.
    caplog.clear()
    probe_attempts = 0

    def partial_omit_then_bad_retry(messages, tier, json_mode):
        nonlocal probe_attempts
        sys_msg = messages[0]["content"]
        if "terminology prober" in sys_msg:
            probe_attempts += 1
            if probe_attempts in (1, 3):
                return json.dumps({"renderings": {"三藏": "Sanzang"}})
            return "NOT JSON"
        return json.dumps(
            {"terms": [{"source": "三藏", "target": "Sanzang", "type": "person", "gender": "male"}]}
        )

    omit_agent = _bible_agent(partial_omit_then_bad_retry)
    with caplog.at_level(logging.WARNING, logger="wenyi_core.agents.book_bible"):
        res_omit = omit_agent.compile_bible(digests, corpus)
    assert [t.source for t in res_omit.terms] == ["三藏"]
    assert "Book Bible probe retry failed" in caplog.text


def test_compiled_book_bible_cache_hits_on_resume_with_translated_repeated_quotes(tmp_path):
    from wenyi_core.progress_labels import english_progress_label
    from wenyi_core.storage.file import FileStorage

    quote = "山高月小，水落石出，此景难忘。"
    book_text = (
        f"第一章\n\n行者与三藏来到山谷。\n\n「{quote}」\n\n"
        f"第二章\n\n三藏再念道：「{quote}」\n\n行者点头。\n"
    )
    src_file = tmp_path / "resume_quotes.txt"
    src_file.write_text(book_text, encoding="utf-8")

    bible_probe_calls = 0
    fail_second_chapter = True

    def handler(messages, tier, json_mode):
        nonlocal bible_probe_calls
        sys_msg = messages[0]["content"]
        user_msg = messages[-1]["content"]
        if "pre-translation analyst" in sys_msg:
            return json.dumps({"style_guide": "Classical adventure style.", "characters": []})
        if "chapter digest writer" in sys_msg:
            return "行者 and 三藏 travel through the valley."
        if "whole-book synopsis writer" in sys_msg:
            return "A short journey of 行者 and 三藏."
        if "terminology prober" in sys_msg:
            bible_probe_calls += 1
            keys = re.findall(r"^\d+\.\s+(.+?)\s+\|", user_msg, re.MULTILINE)
            return json.dumps(
                {"renderings": {k: ("Pilgrim" if k == "行者" else "Sanzang") for k in keys}}
            )
        if "Book Bible" in sys_msg:
            return json.dumps(
                {
                    "terms": [
                        {"source": "行者", "target": "Pilgrim", "type": "person"},
                        {"source": "三藏", "target": "Sanzang", "type": "person"},
                    ],
                    "conventions": [],
                }
            )
        if "repeated" in sys_msg.lower() and "quote" in sys_msg.lower():
            return json.dumps({"quotes": {"0": "The mountains are high and the moon is small."}})
        if "literary translator" in sys_msg:
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
        if "senior bilingual editor" in sys_msg:
            return json.dumps({"patches": []})
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
                "polish_mode": "sparse",
                "chapter_concurrency": 1,
                "polish_concurrency": 1,
                "consistency_sweep": True,
                "review": False,
            },
        }
    )

    progress_labels: list[str] = []
    orch1 = Orchestrator(cfg, client=FakeClient(handler))
    try:
        orch1.run(
            str(src_file),
            progress=lambda _d, _t, lbl: progress_labels.append(lbl),
        )
    except RuntimeError:
        pass
    assert bible_probe_calls > 0
    assert "progress.stage.compilingBible" in progress_labels
    assert english_progress_label("progress.stage.compilingBible") == "Compiling Book Bible…"

    # Resume: chapter 0 is already translated (including the repeated quote). The compiled
    # Book Bible cache MUST hit instead of re-probing!
    calls_before_resume = bible_probe_calls
    fail_second_chapter = False
    resume_labels: list[str] = []
    orch2 = Orchestrator(cfg, client=FakeClient(handler))
    orch2.run(
        str(src_file),
        progress=lambda _d, _t, lbl: resume_labels.append(lbl),
    )
    assert bible_probe_calls == calls_before_resume
    assert "progress.stage.consistencySweep" in resume_labels
    assert (
        english_progress_label("progress.stage.consistencySweep")
        == "Running cross-chapter consistency sweep…"
    )
    run_dir = next((tmp_path / "state").glob("*/targets/*"))
    store = FileStorage(str(run_dir), create=False)
    events = [ev["event"] for ev in store.list_events()]
    assert "glossary_bible_reused" in events


def test_bible_candidates_mine_accented_greek_and_latin_extended_names():
    greek_digests = ["Η Αθήνα και η Μαρία συνάντησαν τον Σωκράτης και τον Όμηρος."]
    greek_corpus = "Η Αθήνα μίλησε. " * 4 + "Ο Σωκράτης απάντησε. " * 4 + "Ο Όμηρος έγραψε. " * 4
    greek_sources = {
        c.source for c in mine_bible_candidates(greek_digests, greek_corpus, source_lang="el")
    }
    assert {"Αθήνα", "Σωκράτης", "Όμηρος"} <= greek_sources

    latin_digests = ["Łukasz met Nguyễn and Čapek in Prague."]
    latin_corpus = "Łukasz arrived. " * 4 + "Nguyễn smiled. " * 4 + "Čapek wrote. " * 4
    latin_sources = {
        c.source for c in mine_bible_candidates(latin_digests, latin_corpus, source_lang="pl")
    }
    assert {"Łukasz", "Nguyễn", "Čapek"} <= latin_sources


def test_batch_executor_inline_polish_preserves_draft_on_request_stopped():
    from wenyi_core.agents.translator import Translator
    from wenyi_core.llm.limits import RequestStopped
    from wenyi_core.pipeline.translate.translation_batch import BatchPlan, TranslationBatchExecutor

    stop = RequestStopped("budget exhausted")

    class _StoppingEditor:
        def edit_batch(self, *args, **kwargs):
            raise stop

    cfg = Config.from_dict(
        {"language": {"source": "zh-Hant", "target": "en"}, "llm": {"preset": "fake"}}
    )
    translator = Translator(
        FakeClient(handler=lambda m, t, j: json.dumps({"translations": ["Draft one."]})), cfg
    )
    executor = TranslationBatchExecutor(translator, None, _StoppingEditor())
    plan = BatchPlan(
        chapter=0,
        start_index=0,
        segment_indices=(0,),
        sources=("第一段。",),
        terms=(),
        context="",
        style="",
        book_synopsis="",
        chapter_digest="",
        annotation_contexts=[[]],
        next_source="",
        allow_empty_translations=False,
    )
    result = executor.execute(plan, polish=True, polish_mode="sparse")
    assert result.targets == ("Draft one.",)
    assert result.before_polish == ("Draft one.",)
    assert result.polish_failed is True
    assert result.polish_interrupted is stop


def test_parallel_secondary_save_error_does_not_mask_earlier_chapter_failure(tmp_path: Path):
    import pytest
    from wenyi_core.llm.limits import RequestCancelled
    from wenyi_core.storage.file import FileStorage

    book = tmp_path / "novel.txt"
    book.write_text(_PARALLEL_BOOK, encoding="utf-8")
    cfg = _parallel_config(tmp_path, chapter_concurrency=3)
    ch1_finished = threading.Event()
    base = _parallel_handler([])

    def handler(messages, tier, json_mode):
        sys_msg = messages[0]["content"]
        user_msg = messages[-1]["content"]
        if "literary translator" in sys_msg and "] 行者跟隨三藏走出長安。" in user_msg:
            ch1_finished.wait(timeout=5)
            raise RequestCancelled("user cancelled")
        return base(messages, tier, json_mode)

    orch = Orchestrator(cfg, client=FakeClient(handler=handler))
    orig_worker = orch._translation._translate_wave_worker

    def paced_worker(spec, polish_executor=None, stop=None):
        result = orig_worker(spec, polish_executor, stop)
        if spec.chapter == 1:
            ch1_finished.set()
        return result

    orch._translation._translate_wave_worker = paced_worker
    real_save = FileStorage.save_chapter

    def failing_ch1_save(self, chapter):
        if chapter.index == 1 and any(s.target for s in chapter.text_segments):
            raise OSError("ch1 save failed")
        return real_save(self, chapter)

    FileStorage.save_chapter = failing_ch1_save
    try:
        with pytest.raises(RequestCancelled, match="user cancelled"):
            orch.run(str(book))
    finally:
        FileStorage.save_chapter = real_save


def test_parallel_same_chapter_save_error_does_not_mask_its_own_wave_error(tmp_path: Path):
    import pytest
    from wenyi_core.llm.limits import RequestCancelled
    from wenyi_core.storage.file import FileStorage

    book = tmp_path / "novel.txt"
    book.write_text(_PARALLEL_BOOK, encoding="utf-8")
    cfg = _parallel_config(tmp_path, chapter_concurrency=2)
    base = _parallel_handler([])

    def handler(messages, tier, json_mode):
        sys_msg = messages[0]["content"]
        user_msg = messages[-1]["content"]
        if "literary translator" in sys_msg and "] 行者笑著說道：前面有山。" in user_msg:
            raise RequestCancelled("budget cancelled on batch 1")
        return base(messages, tier, json_mode)

    orch = Orchestrator(cfg, client=FakeClient(handler=handler))
    real_save = FileStorage.save_chapter

    def failing_ch0_save(self, chapter):
        if chapter.index == 0 and any(s.target for s in chapter.text_segments):
            raise OSError("ch0 save failed")
        return real_save(self, chapter)

    FileStorage.save_chapter = failing_ch0_save
    try:
        with pytest.raises(RequestCancelled, match="budget cancelled on batch 1"):
            orch.run(str(book))
    finally:
        FileStorage.save_chapter = real_save


def test_book_bible_probe_retries_omitted_keys_once_before_failing():
    calls: list[list[str]] = []

    def handler(messages, tier, json_mode):
        sys_msg = messages[0]["content"]
        user_msg = messages[-1]["content"]
        if "terminology prober" in sys_msg:
            keys = re.findall(r"^\d+\.\s+(.+?)\s+\|", user_msg, re.MULTILINE)
            calls.append(keys)
            if len(calls) == 1:
                # Omit one candidate on the first attempt of probe A.
                return json.dumps({"renderings": {k: "Sanzang" for k in keys if k != "行者"}})
            return json.dumps(
                {"renderings": {k: ("Pilgrim" if k == "行者" else "Sanzang") for k in keys}}
            )
        return json.dumps(
            {
                "terms": [
                    {"source": "行者", "target": "Pilgrim", "type": "person", "gender": "male"},
                    {"source": "三藏", "target": "Sanzang", "type": "person", "gender": "male"},
                ]
            }
        )

    agent = _bible_agent(handler)
    digests = ["三藏 gives 孫悟空 the byname 行者."]
    corpus = "行者跟隨三藏。行者大笑。三藏點頭。"
    result = agent.compile_bible(digests, corpus)
    assert {t.source for t in result.terms} >= {"行者", "三藏"}
    # Second call retried only the omitted candidate ("行者"), not the whole chunk.
    assert calls[1] == ["行者"]


def test_bible_candidates_mine_arabic_and_other_unicameral_scripts():
    ar_digests = ["Muhammad (محمد) met Abu Bakr (أبو بكر) in Cairo (القاهرة)."]
    ar_corpus = "سافر محمد إلى القاهرة. " * 3 + "وتحدث أبو بكر مع محمد في القاهرة. " * 2
    ar_sources = {c.source for c in mine_bible_candidates(ar_digests, ar_corpus, source_lang="ar")}
    assert {"محمد", "أبو بكر", "القاهرة"} <= ar_sources

    ko_digests = ["Hong Gildong (홍길동) left Seoul (서울)."]
    ko_corpus = "홍길동은 서울을 떠났다. 홍길동은 다시 서울로 돌아왔다."
    ko_sources = {c.source for c in mine_bible_candidates(ko_digests, ko_corpus, source_lang="ko")}
    assert {"홍길동", "서울"} <= ko_sources


def test_sparse_editor_rejects_patches_that_drop_or_invent_object_markers():
    from wenyi_core.agents.sparse_editor import SparseSemanticEditor

    def handler(messages, tier, json_mode):
        return json.dumps(
            {
                "patches": [
                    {"index": 0, "reason": "polish", "text": "He looked carefully."},
                    {"index": 1, "reason": "polish", "text": "She smiled 〘object:fake〙 warmly."},
                ]
            }
        )

    editor = SparseSemanticEditor(FakeClient(handler=handler), Config())
    res = editor.edit_batch(
        ["他仔细看着。", "她温柔地笑了。"],
        ["He looked 〘object:fig1〙 carefully.", "She smiled warmly."],
    )
    assert res.targets == ("He looked 〘object:fig1〙 carefully.", "She smiled warmly.")
    assert res.patched_indices == ()


def test_entity_markers_skip_segments_with_protected_object_offsets():
    from wenyi_core.agents.translator import Translator
    from wenyi_core.pipeline.translate.translation_batch import BatchPlan, TranslationBatchExecutor

    seen_prompts: list[str] = []

    def handler(messages, tier, json_mode):
        user_msg = messages[-1]["content"]
        seen_prompts.append(user_msg)
        return json.dumps(
            [
                "Great Sage Sun Wukong spoke. 〘object:obj1〙",
                "Pilgrim 〘object:obj2〙 spoke.",
                "⟦N0⟧ laughed.",
            ]
        )

    cfg = Config()
    cfg.pipeline.entity_markers = True
    translator = Translator(FakeClient(handler=handler), cfg)
    executor = TranslationBatchExecutor(translator, polisher=None)
    terms = (
        GlossaryTerm(source="齊天大聖孫悟空", target="Great Sage Sun Wukong", type="person"),
        GlossaryTerm(source="行者", target="Pilgrim", type="person"),
    )
    plan = BatchPlan(
        chapter=0,
        start_index=0,
        segment_indices=(0, 1, 2),
        sources=("齊天大聖孫悟空說。", "行者說道。", "行者大笑。"),
        terms=terms,
        context="",
        style="",
        book_synopsis="",
        chapter_digest="",
        annotation_contexts=[
            [
                {
                    "target_key": "obj1",
                    "source": "[figure 1]",
                    "marker": "〘object:obj1〙",
                    "source_offset": "9",
                }
            ],
            [
                {
                    "target_key": "obj2",
                    "source": "[figure 2]",
                    "marker": "〘object:obj2〙",
                    "source_offset": "2",
                }
            ],
            [],
        ],
        next_source="",
        entity_markers=True,
    )
    draft = executor.translate(plan)
    assert draft.targets == (
        "Great Sage Sun Wukong spoke. 〘object:obj1〙",
        "Pilgrim 〘object:obj2〙 spoke.",
        "Pilgrim laughed.",
    )
    # Protected-object segments kept exact source offsets while the plain segment was masked.
    assert "齊天大聖孫悟空說。〘object:obj1〙" in seen_prompts[0]
    assert "行者〘object:obj2〙說道。" in seen_prompts[0]
    assert "⟦N0⟧大笑。" in seen_prompts[0]


def test_chapter_hygiene_preserves_offset_bound_annotation_and_docx_style_segments(
    tmp_path: Path,
):
    from wenyi_core.alignment import target_digest
    from wenyi_core.models.document import Chapter, Segment
    from wenyi_core.pipeline.annotate.annotations import AnnotationService
    from wenyi_core.pipeline.run.runtime import PipelineRuntime
    from wenyi_core.pipeline.translate.translation import TranslationService
    from wenyi_core.storage.file import FileStorage

    cfg = _parallel_config(tmp_path, chapter_concurrency=1)
    store = FileStorage(str(tmp_path / "run"), create=True)
    runtime = PipelineRuntime(cfg, client=FakeClient(handler=_parallel_handler([])))
    service = TranslationService(runtime, AnnotationService(runtime))

    ann_head = Segment(
        index=0,
        source="行者道：「第一段。",
        target="Walker paused.\\nFirst part.",
        meta={
            "annotations": {
                "items": [{"id": "a1", "mode": "point", "source_start": 0, "source_end": 2}],
                "target_digest": target_digest("Walker paused.\\nFirst part.Continuation.\\nhere."),
                "placements": [
                    {"id": "a1", "status": "aligned", "target_start": 0, "target_end": 6}
                ],
            }
        },
    )
    ann_cont = Segment(
        index=1,
        source="續段。",
        target="Continuation.\\nhere.",
        cont=True,
    )
    docx_seg = Segment(
        index=2,
        source="三藏笑道。",
        target="Sanzang waited.\\nNow.",
        meta={
            "docx_styles": {
                "items": [{"id": "s1", "mode": "range", "source_start": 0, "source_end": 2}],
                "target_digest": target_digest("Sanzang waited.\\nNow."),
                "placements": [
                    {"id": "s1", "status": "aligned", "target_start": 0, "target_end": 7}
                ],
            }
        },
    )
    plain_seg = Segment(
        index=3,
        source="行者又道。",
        target="Walker waited.\\nNow.",
    )
    chapter = Chapter(index=0, title="Ch1", segments=[ann_head, ann_cont, docx_seg, plain_seg])

    service._apply_chapter_hygiene(store, chapter)

    # Offset-bound annotation (head + cont) and docx_styles segments stay byte-identical.
    assert ann_head.target == "Walker paused.\\nFirst part."
    assert ann_cont.target == "Continuation.\\nhere."
    assert docx_seg.target == "Sanzang waited.\\nNow."
    # The plain segment gets the leaked-escape repair.
    assert plain_seg.target == "Walker waited.\nNow."


def test_bible_candidates_tiebreak_deterministically_at_max_candidates():
    digests = ["Alpha met Beta and Gamma and Delta."]
    corpus = "Alpha Alpha. Beta Beta. Gamma Gamma. Delta Delta."
    first = [
        c.source for c in mine_bible_candidates(digests, corpus, source_lang="en", max_candidates=2)
    ]
    second = [
        c.source for c in mine_bible_candidates(digests, corpus, source_lang="en", max_candidates=2)
    ]
    assert first == second == ["Gamma", "Delta"]


def test_book_bible_treats_single_empty_probe_as_divergent_for_non_digest_candidate():
    probe_call = 0

    def handler(messages, tier, json_mode):
        nonlocal probe_call
        sys_msg = messages[0]["content"]
        user_msg = messages[-1]["content"]
        if "terminology prober" in sys_msg:
            probe_call += 1
            keys = re.findall(r"^\d+\.\s+(.+?)\s+\|", user_msg, re.MULTILINE)
            # First probe renders the corpus-mined term; second probe excludes it with "".
            return json.dumps(
                {"renderings": {k: ("Tiger Vanguard" if probe_call == 1 else "") for k in keys}}
            )
        return json.dumps(
            {
                "terms": [
                    {
                        "source": "虎先鋒",
                        "target": "Tiger Vanguard",
                        "type": "person",
                        "gender": "male",
                    }
                ]
            }
        )

    agent = _bible_agent(handler)
    # Non-digest candidate with exactly 3 occurrences (below count >= 5 anchor threshold).
    corpus = "虎先鋒出洞。虎先鋒喝道。虎先鋒退去。"
    result = agent.compile_bible(["Unrelated digest."], corpus)
    assert result.divergent_count == 1
    assert any(t.source == "虎先鋒" and t.target == "Tiger Vanguard" for t in result.terms)


def test_sparse_editor_returns_failed_on_context_window_overflow(monkeypatch):
    from wenyi_core.agents import sparse_editor as sparse_mod
    from wenyi_core.agents.sparse_editor import SparseSemanticEditor

    def _overflow(**kwargs):
        raise ValueError("prompt exceeds context window")

    monkeypatch.setattr(sparse_mod, "fit_prompt_to_context_window", _overflow)
    editor = SparseSemanticEditor(FakeClient(handler=lambda *a, **k: "{}"), Config())
    res = editor.edit_batch(["原文。"], ["Draft text."])
    assert res.failed is True
    assert res.targets == ("Draft text.",)


def test_entity_markers_plain_retry_rejects_residual_marker_brackets():
    import pytest
    from wenyi_core.agents.translator import Translator
    from wenyi_core.pipeline.translate.translation_batch import BatchPlan, TranslationBatchExecutor

    calls = 0

    def handler(messages, tier, json_mode):
        nonlocal calls
        calls += 1
        if calls == 1:
            return json.dumps(["He laughed."])  # drops ⟦N0⟧, triggering plain retry
        return json.dumps(["⟦N99⟧ laughed."])  # residual unknown marker on plain retry

    cfg = Config()
    cfg.pipeline.entity_markers = True
    translator = Translator(FakeClient(handler=handler), cfg)
    executor = TranslationBatchExecutor(translator, polisher=None)
    plan = BatchPlan(
        chapter=0,
        start_index=0,
        segment_indices=(0,),
        sources=("行者大笑。",),
        terms=(GlossaryTerm(source="行者", target="Pilgrim", type="person"),),
        context="",
        style="",
        book_synopsis="",
        chapter_digest="",
        annotation_contexts=[[]],
        next_source="",
        entity_markers=True,
    )
    with pytest.raises(ValueError, match="residual marker"):
        executor.translate(plan)


def test_book_bible_keeps_adjudicated_gender_when_scenes_mix_pronouns():
    """他 and 她 after a name usually refer to other people; they are no disguise signal."""

    def handler(messages, tier, json_mode):
        sys_msg = messages[0]["content"]
        if "terminology prober" in sys_msg:
            return json.dumps({"renderings": {"黛玉": "Daiyu"}})
        if "Book Bible" in sys_msg:
            return json.dumps(
                {
                    "terms": [
                        {"source": "黛玉", "target": "Daiyu", "type": "person", "gender": "female"}
                    ]
                }
            )
        return "{}"

    corpus = "黛玉见了他，心中一动。黛玉低头想她母亲的话。黛玉笑了。黛玉起身。黛玉回房。"
    agent = _bible_agent(handler)
    result = agent.compile_bible(["黛玉见了宝玉。"], corpus)
    by_source = {term.source: term for term in result.terms}
    assert by_source["黛玉"].gender == "female"
    assert "do not spoil" not in by_source["黛玉"].note.lower()


@pytest.mark.parametrize(
    ("source", "target"),
    [
        # Russian case ending: a probe can observe "Москву" next to canonical "Москва".
        ("He went to Moscow.", "Он отправился в Москву."),
        # English plural of a family name.
        ("史密斯一家到了。", "The Smiths arrived."),
        # A variant that is part of another name.
        ("老高的女儿高翠兰。", "Old Gao's daughter, Gao Cuilan."),
        # An open glossary conflict may be a different place.
        ("白石在白石镇外。", "White Stone lies beyond White Rock Town."),
    ],
)
def test_chapter_hygiene_never_rewrites_names(tmp_path: Path, source: str, target: str):
    """Name consistency belongs to the glossary prompt and Review, not a regex pass."""
    from wenyi_core.models.document import Chapter, Segment
    from wenyi_core.pipeline.annotate.annotations import AnnotationService
    from wenyi_core.pipeline.run.runtime import PipelineRuntime
    from wenyi_core.pipeline.translate.translation import TranslationService
    from wenyi_core.storage.file import FileStorage

    cfg = _parallel_config(tmp_path, chapter_concurrency=1)
    store = FileStorage(str(tmp_path / "run"), create=True)
    runtime = PipelineRuntime(cfg, client=FakeClient(handler=_parallel_handler([])))
    service = TranslationService(runtime, AnnotationService(runtime))
    service._bible_variants = {
        "Moscow": ("Москву",),
        "史密斯": ("Smiths",),
        "老高": ("Gao",),
    }
    store.open_conflicts = lambda: [{"source": "白石", "proposed_target": "White Rock"}]
    for term in (
        GlossaryTerm(source="Moscow", target="Москва", type="place"),
        GlossaryTerm(source="史密斯", target="Smith", type="person"),
        GlossaryTerm(source="老高", target="Old Gao", type="person"),
        GlossaryTerm(source="白石", target="White Stone", type="place"),
    ):
        store.upsert_term(term)
    seg = Segment(index=0, source=source, target=target)
    chapter = Chapter(index=0, title="Ch1", segments=[seg])

    service._apply_chapter_hygiene(store, chapter)

    assert seg.target == target


def test_serial_chapters_pass_book_bible_variants_to_batches(tmp_path: Path):
    """Probe-observed name variants are sparse-editor evidence on the serial path too."""
    book = tmp_path / "novel.txt"
    book.write_text(_PARALLEL_BOOK, encoding="utf-8")
    orch = Orchestrator(
        _parallel_config(tmp_path, chapter_concurrency=1),
        client=FakeClient(handler=_parallel_handler([])),
    )
    original = orch._translation._bible_seed.ensure_seeded

    def seeded(store, **kwargs):
        original(store, **kwargs)
        return {"行者": ("Walker",)}

    orch._translation._bible_seed.ensure_seeded = seeded
    plans: list = []
    real_execute = orch._translation._batches.execute

    def capture(plan, **kwargs):
        plans.append(plan)
        return real_execute(plan, **kwargs)

    orch._translation._batches.execute = capture
    orch.run(str(book))
    assert plans
    assert all(plan.variants_by_source == {"行者": ("Walker",)} for plan in plans)


def test_parallel_resume_repolish_passes_book_bible_variants(tmp_path: Path):
    book = tmp_path / "novel.txt"
    book.write_text(_PARALLEL_BOOK, encoding="utf-8")
    base = _parallel_handler([])

    def drafting(messages, tier, json_mode):
        if "senior bilingual editor" in messages[0]["content"]:
            raise RuntimeError("editor down")
        return base(messages, tier, json_mode)

    cfg = _parallel_config(tmp_path, polish=True, polish_mode="sparse")
    Orchestrator(cfg, client=FakeClient(handler=drafting)).run(str(book))

    orch = Orchestrator(cfg, client=FakeClient(handler=base))
    original = orch._translation._bible_seed.ensure_seeded

    def seeded(store, **kwargs):
        original(store, **kwargs)
        return {"行者": ("Walker",)}

    orch._translation._bible_seed.ensure_seeded = seeded
    jobs: list = []
    real_schedule = orch._translation._schedule_polish

    def capture(store, job, *args, **kwargs):
        jobs.append(job)
        return real_schedule(store, job, *args, **kwargs)

    orch._translation._schedule_polish = capture
    orch.run(str(book))
    assert jobs
    assert all(job.variants_by_source == {"行者": ("Walker",)} for job in jobs)
