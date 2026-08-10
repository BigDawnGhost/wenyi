"""配置文件创建、通用 LLM 配置与迁移校验测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from trans_novel.config import Config


class TestConfigFileCreation(unittest.TestCase):
    def test_create_default_file_can_be_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "config.yaml"
            created = Config.create_default_file(str(path))
            cfg = Config.load(str(path))

            self.assertTrue(created)
            self.assertTrue(path.is_file())
            self.assertEqual(cfg.llm.api_format, "openai")
            self.assertEqual(cfg.llm.api_key_env, "LLM_API_KEY")
            self.assertEqual(cfg.llm.base_url, "")
            self.assertEqual(cfg.llm.model, "")
            self.assertEqual(cfg.llm.tiers, {})
            self.assertIsNone(cfg.llm.api_key)
            generated = path.read_text(encoding="utf-8")
            self.assertIn("# trans-novel 配置", generated)
            self.assertIn("  api_format: openai", generated)
            self.assertIn("  api_key_env: LLM_API_KEY", generated)
            self.assertIn('  base_url: ""', generated)
            self.assertIn('  model: ""', generated)
            self.assertIn("output:\n", generated)
            self.assertTrue(cfg.output.mono)
            self.assertFalse(cfg.output.bilingual)
            self.assertEqual(cfg.output.bilingual_order, "target_first")
            self.assertFalse(cfg.output.bilingual_preserve_source_style)
            self.assertTrue(cfg.output.about_page)
            self.assertFalse(cfg.pipeline.review)
            self.assertTrue(cfg.pipeline.polish)
            self.assertEqual(cfg.pipeline.backtranslate_sample, 0.0)
            self.assertTrue(cfg.pipeline.annotation_alignment)
            self.assertEqual(cfg.pipeline.review_concurrency, 4)
            self.assertEqual(cfg.pipeline.review_output_retries, 2)
            self.assertTrue(cfg.pipeline.review_agent_loop)
            self.assertEqual(cfg.pipeline.review_agent_tier, "strong")
            self.assertEqual(cfg.pipeline.review_agent_max_evidence_rounds, 2)
            self.assertTrue(cfg.pipeline.review_conflict_arbitration)
            self.assertTrue(cfg.pipeline.review_fix_loop)
            self.assertEqual(cfg.pipeline.review_fix_max_rounds, 2)
            self.assertEqual(cfg.pipeline.review_clean_confirmations, 2)

    def test_load_never_overwrites_existing_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text("language:\n  source: en\n  target: zh\n", encoding="utf-8")

            cfg = Config.load(str(path))

            self.assertEqual(cfg.source_lang, "en")
            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "language:\n  source: en\n  target: zh\n",
            )


class TestLLMConfig(unittest.TestCase):
    def test_api_format_aliases_are_case_insensitive(self):
        cases = {
            "anthropic": "anthropic",
            "A": "anthropic",
            "openai": "openai",
            "OAI": "openai",
            "fake": "fake",
        }
        for configured, expected in cases.items():
            with self.subTest(configured=configured):
                cfg = Config.from_dict({"llm": {"api_format": configured}})
                self.assertEqual(cfg.llm.api_format, expected)

    def test_direct_api_key_is_secret_and_optional_fields_are_flat(self):
        cfg = Config.from_dict(
            {
                "llm": {
                    "api_format": "a",
                    "api_key": "top-secret",
                    "api_key_env": "IGNORED_KEY",
                    "base_url": "https://example.test/v1/messages",
                    "model": "claude-model",
                    "max_tokens": 9000,
                    "temperature": 0.2,
                    "thinking": True,
                    "reasoning_effort": "low",
                    "request_overrides": {"metadata": {"x": 1}},
                    "tiers": {
                        "fast": {
                            "model": "fast-model",
                            "thinking": False,
                            "request_overrides": {"metadata": {"y": 2}},
                        }
                    },
                }
            }
        )

        assert cfg.llm.api_key is not None
        self.assertEqual(cfg.llm.api_key.get_secret_value(), "top-secret")
        self.assertNotIn("top-secret", repr(cfg.llm))
        self.assertEqual(cfg.llm.max_tokens, 9000)
        self.assertEqual(cfg.llm.tiers["fast"].model, "fast-model")
        self.assertFalse(cfg.llm.tiers["fast"].thinking)

    def test_unknown_format_and_unknown_fields_are_rejected(self):
        for value in ("gemini", "", None):
            with self.subTest(value=value), self.assertRaisesRegex(ValidationError, "api_format"):
                Config.from_dict({"llm": {"api_format": value}})
        with self.assertRaisesRegex(ValidationError, "extra_forbidden"):
            Config.from_dict({"llm": {"api_format": "openai", "unknown": True}})

    def test_tiers_must_be_a_mapping(self):
        with self.assertRaisesRegex(ValueError, "llm.tiers"):
            Config.from_dict({"llm": {"tiers": ["fast"]}})

    def test_unknown_tier_name_is_rejected(self):
        with self.assertRaisesRegex(ValidationError, "literal_error"):
            Config.from_dict(
                {
                    "llm": {
                        "api_format": "openai",
                        "tiers": {"fasat": {"model": "wrong-model"}},
                    }
                }
            )

    def test_old_provider_fields_raise_actionable_migration_error(self):
        legacy_configs = (
            {"llm": {"provider": "deepseek"}},
            {"llm": {"reasoning_style": "openai"}},
            {"llm": {"tiers": {"strong": {"model": "m", "options": {"thinking": True}}}}},
        )
        for raw in legacy_configs:
            with self.subTest(raw=raw), self.assertRaisesRegex(ValueError, "api_format: openai"):
                Config.from_dict(raw)


class TestOtherConfigDefaults(unittest.TestCase):
    def test_partial_config_uses_yaml_pipeline_defaults(self):
        cfg = Config.from_dict({"pipeline": {"review": False}})

        self.assertFalse(cfg.pipeline.review)
        self.assertTrue(cfg.pipeline.polish)
        self.assertEqual(cfg.pipeline.backtranslate_sample, 0.0)
        self.assertTrue(cfg.pipeline.annotation_alignment)
        self.assertEqual(cfg.pipeline.review_concurrency, 4)
        self.assertEqual(cfg.pipeline.review_output_retries, 2)
        self.assertTrue(cfg.pipeline.review_agent_loop)
        self.assertEqual(cfg.pipeline.review_agent_tier, "strong")
        self.assertEqual(cfg.pipeline.review_agent_max_evidence_rounds, 2)
        self.assertTrue(cfg.pipeline.review_conflict_arbitration)
        self.assertTrue(cfg.pipeline.review_fix_loop)
        self.assertEqual(cfg.pipeline.review_fix_max_rounds, 2)
        self.assertEqual(cfg.pipeline.review_clean_confirmations, 2)

    def test_about_page_can_be_disabled(self):
        cfg = Config.from_dict({"output": {"about_page": False}})
        self.assertFalse(cfg.output.about_page)


if __name__ == "__main__":
    unittest.main()
