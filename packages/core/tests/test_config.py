"""配置文件创建与加载测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from wenyi_core.config import Config


class TestConfigFileCreation(unittest.TestCase):
    def test_load_creates_missing_default_config(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "nested" / "config.yaml"
            cfg = Config.load(str(path))

            self.assertTrue(path.is_file())
            self.assertEqual(cfg.llm.provider, "deepseek")
            self.assertIn("strong", cfg.llm.tiers)
            generated = path.read_text(encoding="utf-8")
            self.assertIn("# trans-novel 配置", generated)
            self.assertIn("output:\n", generated)
            self.assertTrue(cfg.output.mono)
            self.assertFalse(cfg.output.bilingual)
            self.assertEqual(cfg.output.bilingual_order, "target_first")
            self.assertTrue(cfg.output.about_page)
            self.assertFalse(cfg.pipeline.autofix_severe)
            self.assertTrue(cfg.pipeline.polish)
            self.assertEqual(cfg.pipeline.backtranslate_sample, 0.0)
            self.assertFalse(cfg.pipeline.consistency_qa)
            self.assertEqual(cfg.pipeline.review_concurrency, 4)

    def test_load_never_overwrites_existing_config(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.yaml"
            path.write_text("language:\n  source: en\n  target: zh\n", encoding="utf-8")

            cfg = Config.load(str(path))

            self.assertEqual(cfg.source_lang, "en")
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "language:\n  source: en\n  target: zh\n",
            )

    def test_partial_config_uses_yaml_pipeline_defaults(self):
        """缺失的流水线字段必须与自动生成的 YAML 默认值一致。"""
        cfg = Config.from_dict({"pipeline": {"review": False}})

        self.assertFalse(cfg.pipeline.review)
        self.assertFalse(cfg.pipeline.autofix_severe)
        self.assertTrue(cfg.pipeline.polish)
        self.assertEqual(cfg.pipeline.backtranslate_sample, 0.0)
        self.assertFalse(cfg.pipeline.consistency_qa)
        self.assertEqual(cfg.pipeline.review_concurrency, 4)

    def test_about_page_can_be_disabled(self):
        cfg = Config.from_dict({"output": {"about_page": False}})

        self.assertFalse(cfg.output.about_page)


if __name__ == "__main__":
    unittest.main()
