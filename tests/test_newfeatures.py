"""新功能测试（离线）：语言检测、标点规范化、术语 AI 审计统一、连续全流程。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile

from trans_novel.config import Config
from trans_novel.ingest.detect import detect_language
from trans_novel.postprocess.punct import normalize_zh
from trans_novel.llm.base import FakeClient
from trans_novel.pipeline.orchestrator import Orchestrator
from trans_novel.glossary.store import GlossaryStore, GlossaryTerm
from tests.sample_data import write_sample_txt
from tests.fake_llm import routing_handler


class TestDetect(unittest.TestCase):
    def test_japanese(self):
        self.assertEqual(detect_language("綾小路は教室にいた。「やあ」と言った。"), "ja")

    def test_english(self):
        self.assertEqual(detect_language("It took a moment for his words to sink in."), "en")

    def test_kana_wins_over_latin(self):
        self.assertEqual(detect_language("ABC です"), "ja")


class TestPunct(unittest.TestCase):
    def test_japanese_quotes(self):
        self.assertEqual(normalize_zh("「你好」"), "“你好”")
        self.assertEqual(normalize_zh("『书名』"), "‘书名’")

    def test_halfwidth_to_full_in_cjk(self):
        self.assertEqual(normalize_zh("他说,真的吗?"), "他说，真的吗？")

    def test_no_harm_to_english_numbers(self):
        self.assertEqual(normalize_zh("9.11 vs 9.8"), "9.11 vs 9.8")

    def test_ellipsis_and_dash(self):
        self.assertEqual(normalize_zh("等等...走了--他笑了"), "等等……走了——他笑了")


class TestGlossaryAudit(unittest.TestCase):
    def test_unify_variants_and_rewrite_targets(self):
        from trans_novel.agents.glossary_auditor import GlossaryAuditor

        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            state = os.path.join(d, "state")
            cfg = Config.from_dict({
                "language": {"source": "ja", "target": "zh"},
                "llm": {"provider": "fake", "tiers": {
                    "strong": {"model": "p"}, "cheap": {"model": "f"}}},
                "pipeline": {"review": False, "polish": False,
                             "backtranslate_sample": 0.0, "consistency_qa": False},
                "glossary_audit": False,
                "paths": {"state_dir": state},
            })
            orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
            store = orch.run(txt)

            # 人为制造译法漂移：术语表写入 佳穂，章节正文里混入 佳穗
            g = GlossaryStore(store.glossary_path)
            g.upsert_term(GlossaryTerm(source="カホ", target="佳穂", type="人物"), chapter=0)
            g.close()
            ch = store.load_chapter(0)
            ch.segments[1].target = "佳穂和佳穗在一起。"  # 同名两种写法
            store.save_chapter(ch)

            def handler(messages, tier, json_mode):
                if "术语一致性审计员" in messages[0]["content"]:
                    return json.dumps({"unifications": [
                        {"source": "カホ", "canonical": "佳穂",
                         "variants": ["佳穗"], "reason": "统一为佳穂"}
                    ]}, ensure_ascii=False)
                return "{}"

            g = GlossaryStore(store.glossary_path)
            applied = GlossaryAuditor(FakeClient(handler=handler), cfg).audit(store, g)
            self.assertEqual(len(applied), 1)
            term = g.get_term("カホ")
            self.assertTrue(term.locked)
            self.assertIn("佳穗", term.aliases)
            g.close()

            # 正文里的 佳穗 应已被改写为 佳穂
            ch2 = store.load_chapter(0)
            self.assertEqual(ch2.segments[1].target, "佳穂和佳穂在一起。")


class TestRunAll(unittest.TestCase):
    def test_continuous_pipeline_outputs_epub(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            state = os.path.join(d, "state")
            cfg = Config.from_dict({
                "language": {"source": "auto", "target": "zh"},
                "llm": {"provider": "fake", "tiers": {
                    "strong": {"model": "p"}, "cheap": {"model": "f"}}},
                "pipeline": {"review": True, "polish": True,
                             "backtranslate_sample": 0.0, "consistency_qa": True},
                "concurrency": 3,
                "paths": {"state_dir": state},
            })
            seen = []
            orch = Orchestrator(cfg, client=FakeClient(handler=routing_handler))
            result = orch.run_all(
                txt, progress=lambda done, total, label: seen.append((done, total)),
                out_format="epub",
            )
            self.assertTrue(result["output"].endswith(".epub"))
            self.assertTrue(zipfile.is_zipfile(result["output"]))
            # 进度回调被触发，且最终 done==total
            self.assertTrue(seen)
            self.assertEqual(seen[-1][0], seen[-1][1])
            # auto 检测把源语言定为 ja
            self.assertEqual(cfg.source_lang, "ja")
            # 报告含一致性与统一字段
            self.assertIn("consistency_issues", result["report"])
            self.assertIn("glossary_unifications", result["report"])


if __name__ == "__main__":
    unittest.main()
