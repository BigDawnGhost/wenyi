"""Configuration-file creation and loading tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml
from pydantic import ValidationError
from wenyi_core.config import _DEFAULT_CONFIG_YAML, Config
from wenyi_core.llm.registry import provider_spec
from wenyi_core.llm.routing import resolve_routes


def _flatten_defaults(values, prefix=""):
    """Map nested configuration onto ``section.key`` paths to compare it as a flat set."""
    flat = {}
    for key, value in values.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(_flatten_defaults(value, f"{path}."))
        else:
            flat[path] = value
    return flat


class TestConfigFileCreation(unittest.TestCase):
    def test_create_default_file_can_be_loaded(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "nested" / "config.yaml"
            created = Config.create_default_file(str(path))
            cfg = Config.load(str(path))

            self.assertTrue(created)
            self.assertTrue(path.is_file())
            self.assertEqual(cfg.llm.providers["default"].kind, "deepseek")
            self.assertEqual(
                resolve_routes(cfg.llm)["translation.body"].endpoint, "https://api.deepseek.com"
            )
            self.assertEqual(
                provider_spec("deepseek").adapter_type().default_api_key_env, "DEEPSEEK_API_KEY"
            )
            self.assertEqual(set(cfg.llm.tiers), {"strong", "cheap", "fast"})
            self.assertEqual(cfg.llm.models[cfg.llm.tiers["strong"]].model, "deepseek-flash")
            self.assertEqual(cfg.llm.models[cfg.llm.tiers["cheap"]].model, "deepseek-flash")
            self.assertEqual(cfg.llm.models[cfg.llm.tiers["fast"]].model, "deepseek-flash")
            self.assertTrue(cfg.llm.models[cfg.llm.tiers["fast"]].options["thinking"])
            for profile in cfg.llm.models.values():
                self.assertEqual(profile.options["reasoning_effort"], "high")
            self.assertFalse(hasattr(cfg.llm, "api_key"))
            generated = path.read_text(encoding="utf-8")
            self.assertIn("# Wenyi configuration", generated)
            self.assertIn("  preset: deepseek", generated)
            self.assertIn("output:\n", generated)
            self.assertTrue(cfg.output.mono)
            self.assertFalse(cfg.output.bilingual)
            self.assertEqual(cfg.output.bilingual_order, "target_first")
            self.assertFalse(cfg.output.bilingual_preserve_source_style)
            self.assertTrue(cfg.output.about_page)
            self.assertTrue(cfg.output.punctuation_normalize)
            self.assertIn("  punctuation_normalize: true", generated)
            self.assertNotIn("\npunctuation:\n", generated)
            self.assertTrue(cfg.pipeline.review)
            self.assertTrue(cfg.pipeline.polish)
            self.assertTrue(cfg.pipeline.annotation_alignment)
            self.assertEqual(cfg.pipeline.review_concurrency, 4)
            self.assertEqual(cfg.pipeline.review_output_retries, 2)
            self.assertTrue(cfg.pipeline.review_agent_loop)
            self.assertEqual(resolve_routes(cfg.llm)["review.verify"].tier, "strong")
            self.assertEqual(cfg.pipeline.review_agent_max_evidence_rounds, 2)
            self.assertTrue(cfg.pipeline.review_conflict_arbitration)
            self.assertTrue(cfg.pipeline.review_fix_loop)
            self.assertEqual(cfg.pipeline.review_fix_max_rounds, 2)
            self.assertEqual(cfg.pipeline.review_clean_confirmations, 2)
            self.assertTrue(cfg.pipeline.review_autofix)
            self.assertEqual(cfg.pipeline.pdf_backend, "mineru")
            self.assertEqual(cfg.segment.max_tokens_per_batch, 1800)
            self.assertEqual(cfg.segment.max_tokens_per_segment, 1200)
            self.assertIn("max_tokens_per_batch: 1800", generated)
            self.assertIn("max_tokens_per_segment: 1200", generated)
            self.assertNotIn("max_chars_per_batch", generated)
            self.assertNotIn("glossary_scope", generated)
            self.assertNotIn("glossary_scope", cfg.pipeline.model_dump())

    def test_removed_glossary_scope_is_rejected(self):
        for scope in ("chapter", "full", None):
            with self.subTest(scope=scope):
                with self.assertRaisesRegex(ValidationError, "glossary_scope"):
                    Config.from_dict({"pipeline": {"glossary_scope": scope}})

    def test_removed_segment_char_keys_are_rejected(self):
        with self.assertRaises(Exception):
            Config.from_dict({"segment": {"max_chars_per_batch": 99}})

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
        """Missing pipeline fields must match the generated YAML defaults."""
        cfg = Config.from_dict({"pipeline": {"review": False}})

        self.assertFalse(cfg.pipeline.review)
        self.assertTrue(cfg.pipeline.polish)
        self.assertTrue(cfg.pipeline.annotation_alignment)
        self.assertEqual(cfg.pipeline.review_concurrency, 4)
        self.assertEqual(cfg.pipeline.review_output_retries, 2)
        self.assertTrue(cfg.pipeline.review_agent_loop)
        self.assertEqual(resolve_routes(cfg.llm)["review.verify"].tier, "strong")
        self.assertEqual(cfg.pipeline.review_agent_max_evidence_rounds, 2)
        self.assertTrue(cfg.pipeline.review_conflict_arbitration)
        self.assertTrue(cfg.pipeline.review_fix_loop)
        self.assertEqual(cfg.pipeline.review_fix_max_rounds, 2)
        self.assertEqual(cfg.pipeline.review_clean_confirmations, 2)
        self.assertTrue(cfg.pipeline.review_autofix)
        self.assertEqual(cfg.pipeline.pdf_backend, "mineru")

    def test_about_page_can_be_disabled(self):
        cfg = Config.from_dict({"output": {"about_page": False}})

        self.assertFalse(cfg.output.about_page)

    def test_export_punctuation_normalization_can_be_disabled(self):
        cfg = Config.from_dict({"output": {"punctuation_normalize": False}})

        self.assertFalse(cfg.output.punctuation_normalize)

    def test_unknown_config_sections_are_rejected(self):
        for section in ("punctuation", "pipline"):
            with self.subTest(section=section):
                with self.assertRaisesRegex(ValueError, "Unknown configuration sections"):
                    Config.from_dict({section: {}})

    def test_config_root_must_be_a_mapping(self):
        with self.assertRaisesRegex(ValueError, "must be a mapping"):
            Config.from_dict(["pipeline"])

    def test_compatible_reasoning_style_is_loaded(self):
        cfg = Config.from_dict(
            {
                "llm": {
                    "providers": {"local": {"kind": "ollama", "reasoning_style": "deepseek"}},
                    "models": {"m": {"provider": "local", "model": "m"}},
                    "tiers": {tier: "m" for tier in ("strong", "cheap", "fast")},
                }
            }
        )
        extra = cfg.llm.providers["local"].model_extra
        assert extra is not None
        self.assertEqual(extra["reasoning_style"], "deepseek")


class TestShippedDefaultConfig(unittest.TestCase):
    """The committed config.yaml must track the generated default template."""

    def test_root_config_matches_builtin_template(self):
        root = Path(__file__).resolve().parents[3] / "config.yaml"
        shipped = _flatten_defaults(yaml.safe_load(root.read_text(encoding="utf-8")))
        template = _flatten_defaults(yaml.safe_load(_DEFAULT_CONFIG_YAML))

        drift = {
            "missing from config.yaml": sorted(set(template) - set(shipped)),
            "missing from the built-in template": sorted(set(shipped) - set(template)),
            "different value": {
                key: (shipped[key], template[key])
                for key in sorted(set(shipped) & set(template))
                if shipped[key] != template[key]
            },
        }

        self.assertEqual(
            {kind: found for kind, found in drift.items() if found},
            {},
            "config.yaml at the repository root drifted from the built-in default template",
        )


if __name__ == "__main__":
    unittest.main()
