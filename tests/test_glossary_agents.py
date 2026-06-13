"""分析器 / 术语抽取 / 滚动上下文 的测试（离线）。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from trans_novel.config import Config
from trans_novel.llm.base import FakeClient
from trans_novel.glossary.store import GlossaryStore
from trans_novel.glossary.extractor import GlossaryExtractor
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
            self.assertEqual(store.get_term("綾小路").gender, "男")
            self.assertEqual(store.get_term("高度育成高校").type, "组织")
            store.close()

        brief = a.style_brief(result)
        self.assertIn("绫小路", brief)


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
            self.assertEqual(horikita.gender, "女")
            self.assertEqual(horikita.aliases, ["堀北さん"])
            self.assertEqual(horikita.first_chapter, 1)
            # "未知" 应被规整为空
            self.assertEqual(store.get_term("屋上").gender, "")
            store.close()


class TestRollingContext(unittest.TestCase):
    def test_render_and_bound(self):
        ctx = RollingContext(summary="梗概文本", max_recent_keep=3)
        ctx.add_targets(["a", "b", "c", "d", "e"])
        self.assertEqual(ctx.recent_targets, ["c", "d", "e"])  # 限长
        rendered = ctx.render(n_recent=2)
        self.assertIn("梗概文本", rendered)
        self.assertIn("d", rendered)
        self.assertIn("e", rendered)
        self.assertNotIn("[0]", rendered)

    def test_update_summary(self):
        client = FakeClient(handler=lambda m, t, j: "更新后的梗概")
        ctx = RollingContext(summary="旧梗概")
        ctx.update_summary(client, "新章节译文")
        self.assertEqual(ctx.summary, "更新后的梗概")
        # 调用走的是 cheap 档
        self.assertEqual(client.calls[-1]["tier"], "cheap")

    def test_roundtrip(self):
        ctx = RollingContext(summary="s", recent_targets=["x", "y"])
        ctx2 = RollingContext.from_dict(ctx.to_dict())
        self.assertEqual(ctx2.summary, "s")
        self.assertEqual(ctx2.recent_targets, ["x", "y"])


if __name__ == "__main__":
    unittest.main()
