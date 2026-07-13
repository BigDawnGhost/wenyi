"""新功能测试（离线）：模型语言检测、标点规范化、术语 AI 审计统一、连续全流程。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile

from wenyi_core.config import Config
from wenyi_core.postprocess.punct import normalize_zh
from wenyi_core.llm.base import FakeClient
from wenyi_core.pipeline.orchestrator import Orchestrator
from tests.sample_data import write_sample_txt
from tests.fake_llm import routing_handler


class TestModelLanguageDetection(unittest.TestCase):
    def _cfg(self, state: str) -> Config:
        return Config.from_dict({
            "language": {"source": "auto", "target": "zh"},
            "llm": {"provider": "fake", "tiers": {
                "strong": {"model": "p"}, "cheap": {"model": "f"}}},
            "pipeline": {"book_understanding": False},
            "paths": {"state_dir": state},
        })

    def test_auto_uses_model_detection(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = self._cfg(os.path.join(d, "state"))

            def handler(messages, tier, json_mode):
                if "语言识别器" in messages[0]["content"]:
                    return json.dumps({"language": "russian"}, ensure_ascii=False)
                return routing_handler(messages, tier, json_mode)

            store = Orchestrator(cfg, client=FakeClient(handler=handler)).prepare(txt)
            self.assertEqual(cfg.source_lang, "ru")
            self.assertEqual(store.load_manifest()["source_lang"], "ru")

    def test_auto_detection_failure_requires_user_source(self):
        with tempfile.TemporaryDirectory() as d:
            txt = os.path.join(d, "novel.txt")
            write_sample_txt(txt)
            cfg = self._cfg(os.path.join(d, "state"))

            def handler(messages, tier, json_mode):
                if "语言识别器" in messages[0]["content"]:
                    return json.dumps({"language": ""}, ensure_ascii=False)
                return routing_handler(messages, tier, json_mode)

            with self.assertRaisesRegex(RuntimeError, "language.source"):
                Orchestrator(cfg, client=FakeClient(handler=handler)).prepare(txt)


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
            # auto 通过模型检测把源语言定为 ja
            self.assertEqual(cfg.source_lang, "ja")
            # 报告含一致性字段。
            self.assertIn("consistency_issues", result["report"])
            with open(result["storage"].event_log_path, "r", encoding="utf-8") as f:
                events = [json.loads(line) for line in f if line.strip()]
            event_names = [e["event"] for e in events]
            self.assertIn("run_initialized", event_names)
            self.assertIn("batch_translated", event_names)
            self.assertIn("report_saved", event_names)
            self.assertIn("assembled", event_names)
            translated = next(e for e in events if e["event"] == "batch_translated")
            self.assertTrue(translated["segments"])
            self.assertIn("source", translated["segments"][0])
            self.assertIn("target", translated["segments"][0])


if __name__ == "__main__":
    unittest.main()
