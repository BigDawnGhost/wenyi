"""分析器 / 术语抽取 / 滚动上下文 的测试（离线）。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from trans_novel.config import Config
from trans_novel.llm.providers.fake import FakeClient
from trans_novel.glossary.store import GlossaryStore
from trans_novel.glossary.extractor import (
    GlossaryExtractor,
    TranslatedSegmentEvidence,
)
from trans_novel.agents.analyzer import Analyzer
from trans_novel.pipeline.context import RollingContext


def _cfg():
    return Config.from_dict({
        "language": {"source": "ja", "target": "zh"},
        "llm": {"provider": "fake", "tiers": {
            "strong": {"model": "p"}, "cheap": {"model": "f"}}},
    })


class TestAnalyzer(unittest.TestCase):
    def test_analyze_and_seed(self):
        analysis = {
            "genre": "校园", "tone": "冷峻第三人称",
            "style_guide": "保持克制",
            "characters": [{"source": "綾小路", "target": "绫小路",
                            "gender": "男", "reading": "あやのこうじ", "note": "第一人称用俺"}],
            "terms": [{"source": "高度育成高校", "target": "高度育成高中", "type": "组织"}],
        }
        client = FakeClient(handler=lambda m, t, j: json.dumps(analysis, ensure_ascii=False))
        a = Analyzer(client, _cfg())
        result = a.analyze("……样章……")
        self.assertEqual(result["genre"], "校园")

        with tempfile.TemporaryDirectory() as d:
            store = GlossaryStore(os.path.join(d, "g.db"))
            n = a.seed_glossary(store, result)
            self.assertEqual(n, 2)
            character = store.get_term("綾小路")
            organization = store.get_term("高度育成高校")
            self.assertIsNotNone(character)
            self.assertIsNotNone(organization)
            assert character is not None
            assert organization is not None
            self.assertEqual(character.gender, "男")
            self.assertEqual(organization.type, "组织")
            store.close()

        brief = a.style_brief(result)
        self.assertIn("绫小路", brief)

    def test_malformed_collection_items_are_filtered(self):
        analysis = {
            "genre": {"unexpected": True},
            "characters": ["bad", {"source": "綾小路", "target": "绫小路"}],
            "terms": [1, {"source": "学校", "target": "学校", "type": {"bad": 1}}],
        }
        client = FakeClient(
            handler=lambda m, t, j: json.dumps(analysis, ensure_ascii=False)
        )
        analyzer = Analyzer(client, _cfg())
        result = analyzer.analyze("……样章……")

        self.assertEqual(result["genre"], "")
        self.assertEqual(len(result["characters"]), 1)
        self.assertEqual(len(result["terms"]), 1)
        with tempfile.TemporaryDirectory() as d:
            store = GlossaryStore(os.path.join(d, "g.db"))
            self.assertEqual(analyzer.seed_glossary(store, result), 2)
            school = store.get_term("学校")
            self.assertIsNotNone(school)
            assert school is not None
            self.assertEqual(school.type, "术语")
            store.close()


class TestExtractor(unittest.TestCase):
    def test_extract_and_store(self):
        terms = {"terms": [
            {"source": "堀北", "target": "堀北", "type": "人物", "gender": "女",
             "aliases": ["堀北さん"]},
            {"source": "屋上", "target": "天台", "type": "地名", "gender": "未知"},
        ]}
        client = FakeClient(handler=lambda m, t, j: json.dumps(terms, ensure_ascii=False))
        ext = GlossaryExtractor(client, _cfg())
        with tempfile.TemporaryDirectory() as d:
            store = GlossaryStore(os.path.join(d, "g.db"))
            summary = ext.extract_and_store(store, "原文", "译文", chapter=1)
            self.assertEqual(summary["inserted"], 2)
            horikita = store.get_term("堀北")
            self.assertIsNotNone(horikita)
            assert horikita is not None
            self.assertEqual(horikita.gender, "女")
            self.assertEqual(horikita.aliases, ["堀北さん"])
            self.assertEqual(horikita.first_chapter, 1)
            # "未知" 应被规整为空
            rooftop = store.get_term("屋上")
            self.assertIsNotNone(rooftop)
            assert rooftop is not None
            self.assertEqual(rooftop.gender, "")
            store.close()

    def test_malformed_optional_fields_fall_back_safely(self):
        terms = {
            "terms": [
                {
                    "source": "term",
                    "target": "术语",
                    "type": {"bad": 1},
                    "gender": ["bad"],
                    "aliases": 1,
                    "note": {"bad": 1},
                }
            ]
        }
        extractor = GlossaryExtractor(
            FakeClient(handler=lambda m, t, j: json.dumps(terms)), _cfg()
        )

        result = extractor.extract("term", "术语", [])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].type, "术语")
        self.assertEqual(result[0].gender, "")
        self.assertEqual(result[0].aliases, [])
        self.assertEqual(result[0].note, "")

    def test_multiple_new_terms_use_targets_from_first_translated_occurrences(self):
        calls: list[str] = []

        def handler(messages, tier, json_mode):
            system = messages[0]["content"]
            calls.append(system)
            if "术语一致性校准器" in system:
                user = messages[-1]["content"]
                self.assertIn("綾小路第一次走进教室。", user)
                self.assertIn("绫小路第一次走进教室。", user)
                self.assertIn("堀北站在窗边。", user)
                self.assertIn('"proposed_target": "掘北"', user)
                return json.dumps(
                    {"terms": [
                        {"source": "綾小路", "target": "绫小路"},
                        {"source": "堀北", "target": "堀北"},
                    ]},
                    ensure_ascii=False,
                )
            return json.dumps(
                {"terms": [
                    {"source": "綾小路", "target": "凌小路", "type": "人物"},
                    {"source": "堀北", "target": "掘北", "type": "人物"},
                ]},
                ensure_ascii=False,
            )

        extractor = GlossaryExtractor(FakeClient(handler=handler), _cfg())
        history = [
            TranslatedSegmentEvidence(
                chapter=0,
                segment=3,
                source="綾小路第一次走进教室。",
                target="绫小路第一次走进教室。",
            ),
            TranslatedSegmentEvidence(
                chapter=0,
                segment=4,
                source="堀北站在窗边。",
                target="堀北站在窗边。",
            ),
        ]
        with tempfile.TemporaryDirectory() as d:
            store = GlossaryStore(os.path.join(d, "g.db"))
            summary = extractor.extract_and_store(
                store,
                "后来綾小路和堀北再次出现。",
                "后来凌小路和掘北再次出现。",
                chapter=2,
                history=history,
                before=(2, 0),
            )
            ayanokoji = store.get_term("綾小路")
            horikita = store.get_term("堀北")
            self.assertIsNotNone(ayanokoji)
            self.assertIsNotNone(horikita)
            assert ayanokoji is not None
            assert horikita is not None
            self.assertEqual(ayanokoji.target, "绫小路")
            self.assertEqual(horikita.target, "堀北")
            self.assertEqual(ayanokoji.first_chapter, 0)
            self.assertEqual(horikita.first_chapter, 0)
            self.assertEqual(summary["history_matched"], 2)
            self.assertEqual(summary["history_aligned"], 2)
            self.assertEqual(summary["history_unresolved"], 0)
            store.close()
        self.assertEqual(len(calls), 2)

    def test_new_term_without_prior_occurrence_is_inserted_directly(self):
        terms = {
            "terms": [{"source": "綾小路", "target": "绫小路", "type": "人物"}]
        }
        client = FakeClient(
            handler=lambda m, t, j: json.dumps(terms, ensure_ascii=False)
        )
        extractor = GlossaryExtractor(client, _cfg())

        with tempfile.TemporaryDirectory() as d:
            store = GlossaryStore(os.path.join(d, "g.db"))
            summary = extractor.extract_and_store(
                store,
                "綾小路第一次出现。",
                "绫小路第一次出现。",
                chapter=0,
                history=[],
                before=(0, 0),
            )
            self.assertIsNotNone(store.get_term("綾小路"))
            self.assertEqual(summary["history_matched"], 0)
            store.close()

        self.assertEqual(len(client.calls), 1)

    def test_unresolved_historical_term_is_not_locked_to_later_translation(self):
        responses = iter([
            json.dumps(
                {"terms": [{"source": "綾小路", "target": "凌小路"}]},
                ensure_ascii=False,
            ),
            json.dumps(
                {"terms": [{"source": "綾小路", "target": ""}]},
                ensure_ascii=False,
            ),
        ])
        extractor = GlossaryExtractor(
            FakeClient(handler=lambda m, t, j: next(responses)), _cfg()
        )
        history = [
            TranslatedSegmentEvidence(
                chapter=0,
                segment=0,
                source="綾小路がいた。",
                target="他在那里。",
            )
        ]

        with tempfile.TemporaryDirectory() as d:
            store = GlossaryStore(os.path.join(d, "g.db"))
            summary = extractor.extract_and_store(
                store,
                "綾小路が戻った。",
                "凌小路回来了。",
                chapter=1,
                history=history,
                before=(1, 0),
            )
            self.assertIsNone(store.get_term("綾小路"))
            self.assertEqual(summary["history_unresolved"], 1)
            store.close()


class TestRollingContext(unittest.TestCase):
    def test_render_and_bound(self):
        ctx = RollingContext(max_recent_keep=3)
        ctx.add_targets(["a", "b", "c", "d", "e"])
        self.assertEqual(ctx.recent_targets, ["c", "d", "e"])  # 限长
        rendered = ctx.render(n_recent=2)  # 只取最近两段
        self.assertIn("d", rendered)
        self.assertIn("e", rendered)
        self.assertNotIn("c", rendered)

    def test_roundtrip(self):
        ctx = RollingContext(recent_targets=["x", "y"], max_recent_keep=75)
        ctx2 = RollingContext.from_dict(ctx.to_dict())
        self.assertEqual(ctx2.recent_targets, ["x", "y"])
        self.assertEqual(ctx2.max_recent_keep, 75)

    def test_configured_minimum_expands_legacy_context_limit(self):
        ctx = RollingContext.from_dict(
            {"recent_targets": [str(i) for i in range(40)]},
            min_recent_keep=100,
        )
        self.assertEqual(ctx.max_recent_keep, 100)


if __name__ == "__main__":
    unittest.main()
