"""精简配置、LLM 公共接口与 Provider 请求方言的离线契约测试。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

from pydantic import ValidationError

from tests.fixtures.fake_llm import fake_llm_dict
from trans_novel.config import (
    Config,
    LLMConfig,
    ModelRef,
    OutputConfig,
    PipelineConfig,
    resolve_output,
)
from trans_novel.llm import (
    FakeClient,
    GenerationOptions,
    build_client,
    parse_json_loose,
)
from trans_novel.llm.errors import JSONParseError
from trans_novel.pipeline.planning import translation_model_profile, translator_model_profile


class TestParseJsonLoose(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(parse_json_loose('{"a":1}'), {"a": 1})

    def test_fenced(self):
        self.assertEqual(parse_json_loose("```json\n[1,2,3]\n```"), [1, 2, 3])

    def test_surrounded_by_prose(self):
        text = '思考结束。结果如下：["译文1","译文2"] 完毕。'
        self.assertEqual(parse_json_loose(text), ["译文1", "译文2"])

    def test_failure(self):
        with self.assertRaises(JSONParseError) as caught:
            parse_json_loose("没有任何 JSON 内容")
        # 保持与 ValueError 兼容：既有调用方仍可按 ValueError 捕获该异常
        self.assertIsInstance(caught.exception, ValueError)

    def test_inner_ascii_quotes_repaired(self):
        raw = '{"translations":["磨到那份锱铢必较里暗含的"小气"二字无声地烫上面颊。"]}'
        got = parse_json_loose(raw)
        self.assertEqual(
            got["translations"][0], '磨到那份锱铢必较里暗含的"小气"二字无声地烫上面颊。'
        )

    def test_trailing_extra_brace(self):
        self.assertEqual(parse_json_loose('{"a": 1}\n}'), {"a": 1})

    def test_valid_json_untouched(self):
        self.assertEqual(parse_json_loose('{"a": "b, c: d"}'), {"a": "b, c: d"})


class TestConfigValidation(unittest.TestCase):
    def test_zero_config_defaults(self):
        cfg = Config.from_dict({})
        self.assertEqual(
            cfg.llm.models.translator,
            ["openrouter/tencent/hy-mt2-30b-a3b:off"],
        )
        general = ["opencode-go/muse-spark-1.3-contributor:low"]
        self.assertEqual(cfg.llm.models.analyst, general)
        self.assertEqual(cfg.llm.models.editor, general)
        self.assertEqual(cfg.llm.models.fast, general)

    def test_removed_primary_role_is_rejected(self):
        with self.assertRaisesRegex(ValidationError, "primary"):
            Config.from_dict({"llm": {"models": {"primary": ["fake/a"]}}})

    def test_lists_are_non_empty_and_unique(self):
        for models in ([], ["fake/a", "fake/a"]):
            with self.subTest(models=models), self.assertRaises(ValidationError):
                Config.from_dict({"llm": {"models": {"translator": models}}})

    def test_old_provider_and_scalar_models_are_rejected(self):
        with self.assertRaises(ValueError):
            Config.from_dict({"llm": {"provider": "fake", "models": {"translator": ["fake/a"]}}})
        with self.assertRaises(ValidationError):
            Config.from_dict({"llm": {"models": {"translator": "fake/a"}}})

    def test_fake_model_roles_are_qualified_lists(self):
        self.assertEqual(
            fake_llm_dict(models=("translator", "analyst", "editor", "fast"))["models"],
            {
                "translator": ["fake/translator"],
                "analyst": ["fake/analyst"],
                "editor": ["fake/editor"],
                "fast": ["fake/fast"],
            },
        )

    def test_provider_model_parsing_rules(self):
        from trans_novel.model_profiles import parse_model_selection, parse_provider_model

        self.assertEqual(
            parse_provider_model("openrouter/google/gemini:high"),
            ("openrouter", "google/gemini:high"),
        )
        self.assertEqual(parse_provider_model("ollama/qwen3:32b"), ("ollama", "qwen3:32b"))
        with self.assertRaises(ValueError):
            parse_provider_model("unknown/model")
        with self.assertRaises(ValueError):
            parse_provider_model("fake/")
        self.assertEqual(parse_model_selection("qwen3:32b").model, "qwen3:32b")

    def test_provider_endpoints_and_capabilities_are_keyed(self):
        from trans_novel.llm.registry import ProviderRegistry
        from trans_novel.llm.usage import UsageTracker
        from trans_novel.model_profiles import DIALECT_BAILIAN, DIALECT_DEEPSEEK, capabilities_for

        cfg = Config.from_dict(
            {
                "llm": {
                    "models": {
                        "translator": [
                            "opencode-go/deepseek-v4-flash:high",
                            "bailian/qwen3.7-flash:off",
                        ],
                        "fast": ["bailian/qwen3.7-flash:off"],
                    },
                    "provider_routing": {
                        "openrouter/tencent/hy3": {
                            "only": ["tencent"],
                            "allow_fallbacks": False,
                        }
                    },
                }
            }
        )
        registry = ProviderRegistry(cfg.llm, UsageTracker())
        go = registry.transport("opencode-go")
        bailian = registry.transport("bailian")
        self.assertIs(go, registry.transport("opencode-go"))
        self.assertEqual(go.provider, "opencode-go")
        self.assertEqual(bailian.provider, "bailian")
        self.assertEqual(go.capabilities_for("deepseek-v4-flash").request_dialect, DIALECT_DEEPSEEK)
        hy3 = capabilities_for("openrouter", "tencent/hy3")
        self.assertTrue(hy3.catalogued and hy3.supports_temperature)
        self.assertEqual(
            cfg.llm.provider_routing["openrouter/tencent/hy3"].only,
            ["tencent"],
        )
        self.assertEqual(bailian.capabilities_for("qwen3.7-flash").request_dialect, DIALECT_BAILIAN)

    def test_model_thinking_suffix_is_validated_against_capabilities(self):
        with self.assertRaises(ValidationError):
            Config.from_dict(
                {"llm": {"models": {"translator": ["opencode-go/deepseek-v4-flash:low"]}}}
            )

    def test_openai_compatible_requires_base_url(self):
        with self.assertRaisesRegex(ValidationError, "base_url"):
            LLMConfig.model_validate({"models": {"translator": ["openai-compatible/a"]}})
        cfg = LLMConfig.model_validate(
            {
                "models": {"translator": ["openai-compatible/a"]},
                "base_url": "https://example.com/v1",
            }
        )
        self.assertEqual(cfg.base_url, "https://example.com/v1")

    def test_standard_provider_rejects_endpoint_overrides(self):
        with self.assertRaisesRegex(ValidationError, "只用于 openai-compatible"):
            LLMConfig.model_validate(
                {"models": {"translator": ["fake/a"]}, "base_url": "https://example.com"}
            )

    def test_unknown_and_deprecated_fields_fail_fast(self):
        with self.assertRaisesRegex(ValidationError, "Extra inputs"):
            Config.from_dict({"unknown": True})
        with self.assertRaisesRegex(ValueError, "已废弃"):
            Config.from_dict({"llm": {"provider": "fake", "models": {"translator": ["fake/a"]}}})

    def test_output_paths_are_relative_to_selected_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            config_path.write_text(
                "output:\n"
                "  override_theme:\n"
                "    styles: themes/general.css\n"
                "  bilingual_styles: themes/bilingual.css\n",
                encoding="utf-8",
            )
            config = Config.load(str(config_path))
            theme = config.output.override_theme
            self.assertEqual(theme.styles, str(root / "themes/general.css"))
            self.assertEqual(config.output.bilingual_styles, str(root / "themes/bilingual.css"))
            self.assertEqual(
                config.output_origins,
                dict.fromkeys(
                    ("override_theme.styles", "bilingual_styles"),
                    str(config_path),
                ),
            )
            config_path.write_text("", encoding="utf-8")
            self.assertTrue(Config.load(str(config_path)).output.mono)
            config_path.write_text("false\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "YAML 映射"):
                Config.load(str(config_path))

    def test_relative_output_path_requires_base_directory(self):
        with self.assertRaisesRegex(ValueError, "base_dir"):
            Config.from_dict({"output": {"bilingual_styles": "themes/bilingual.css"}})
        config = Config.from_dict(
            {"output": {"bilingual_styles": "themes/bilingual.css"}},
            base_dir="config-root",
        )
        self.assertTrue(Path(config.output.bilingual_styles).is_absolute())
        self.assertEqual(config.output_origins, {"bilingual_styles": "config-root"})

    def test_output_resolution_uses_leaf_presence_and_normalizes_once(self):
        saved = OutputConfig.model_validate(
            {
                "mono": False,
                "bilingual": {"enabled": True, "order": "source_first"},
                "override_theme": {"styles": "builtin:chinese-reading"},
                "bilingual_styles": "/saved/bilingual.css",
            }
        )
        current = Config.from_dict({"output": {"mono": True, "override_theme": None}}).output
        current.bilingual.enabled = False
        effective, normalized = resolve_output(current, saved)
        self.assertFalse(normalized)
        self.assertTrue(effective.mono)
        self.assertFalse(effective.bilingual.enabled)
        self.assertEqual(effective.bilingual.order, "source_first")
        self.assertIsNone(effective.override_theme)
        self.assertEqual(effective.bilingual_styles, "/saved/bilingual.css")
        omitted, _ = resolve_output(OutputConfig(), saved)
        self.assertIsNotNone(omitted.override_theme)
        disabled = OutputConfig.model_validate({"mono": False, "bilingual": {"enabled": False}})
        normalized_output, normalized = resolve_output(disabled)
        self.assertTrue(normalized)
        self.assertTrue(normalized_output.mono)

    def test_output_boundary_rejects_legacy_shapes_and_invalid_values(self):
        with self.assertRaisesRegex(ValueError, "output.bilingual.enabled"):
            Config.from_dict({"output": {"bilingual": False}})
        with self.assertRaisesRegex(ValueError, "已废弃"):
            Config.from_dict({"pipeline": {}, "output": {}})
        invalid = (
            {"output": {"mono": 1}},
            {"output": {"bilingual_styles": "builtin:general"}},
            {
                "output": {
                    "override_theme": {
                        "rules": "builtin:general",
                        "styles": "builtin:chinese-reading",
                    }
                }
            },
            {"output": {"override_theme": {"styles": ""}}},
        )
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                Config.from_dict(raw)

    def test_quality_profiles(self):
        self.assertFalse(PipelineConfig.for_quality("economy").single_segment_translation)
        self.assertTrue(PipelineConfig.for_quality("balanced").single_segment_translation)
        self.assertTrue(PipelineConfig.for_quality("quality").single_segment_translation)
        self.assertTrue(PipelineConfig.for_quality("quality").polish)

    def test_fake_provider_usable_without_credentials(self):
        cfg = Config.from_dict({"llm": fake_llm_dict()})
        self.assertEqual(
            build_client(cfg).complete(
                [{"role": "user", "content": "x"}],
                agent="preparer",
                operation="terms.mine",
            ),
            "",
        )


class TestProviderTransportConfiguration(unittest.TestCase):
    def test_openai_compatible_overrides_do_not_redirect_builtin_provider(self):
        from trans_novel.llm.registry import ProviderRegistry
        from trans_novel.llm.usage import UsageTracker

        cfg = LLMConfig(
            models={
                "translator": ["deepseek/custom-chain", "openai-compatible/custom-model"],
            },
            base_url="https://custom.example/v1",
            api_key_env="CUSTOM_API_KEY",
        )
        registry = ProviderRegistry(cfg, UsageTracker())

        builtin = registry.transport("deepseek")
        custom = registry.transport("openai-compatible")

        self.assertEqual(builtin.base_url, "https://api.deepseek.com")
        self.assertEqual(builtin.api_key_env, "DEEPSEEK_API_KEY")
        self.assertEqual(custom.base_url, "https://custom.example/v1")
        self.assertEqual(custom.api_key_env, "CUSTOM_API_KEY")

    def test_opencode_go_sends_identity_and_session_headers(self):
        from trans_novel.llm.registry import ProviderRegistry
        from trans_novel.llm.usage import UsageTracker

        cfg = LLMConfig(models={"translator": ["opencode-go/muse-spark-1.3-contributor"]})
        registry = ProviderRegistry(cfg, UsageTracker())
        with (
            patch.dict(os.environ, {"OPENCODE_API_KEY": "test-key"}),
            patch("openai.OpenAI") as openai,
        ):
            transport = registry.transport("opencode-go")
            transport._ensure_client()

        headers = openai.call_args.kwargs["default_headers"]
        self.assertEqual(headers["User-Agent"], "wenyi")
        self.assertRegex(
            headers["x-opencode-session"],
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        )


class TestRoleProfiles(unittest.TestCase):
    def test_profiles_invalidate_only_consuming_roles(self):
        from trans_novel.pipeline.planning import (
            analyst_model_profile,
            editor_model_profile,
            fast_model_profile,
        )

        base_models = {
            "translator": ["fake/t", "fake/t2"],
            "analyst": ["fake/a"],
            "editor": ["fake/e"],
            "fast": ["fake/f"],
        }
        base = Config.from_dict({"llm": {"models": base_models}})
        profiles = {
            "translator": translator_model_profile,
            "analyst": analyst_model_profile,
            "editor": editor_model_profile,
            "fast": fast_model_profile,
        }
        for changed_role in profiles:
            changed_models = {role: list(values) for role, values in base_models.items()}
            changed_models[changed_role] = [f"fake/{changed_role}-changed"]
            changed = Config.from_dict({"llm": {"models": changed_models}})
            for role, role_profile in profiles.items():
                with self.subTest(changed=changed_role, profile=role):
                    if role == changed_role:
                        self.assertNotEqual(role_profile(base), role_profile(changed))
                    else:
                        self.assertEqual(role_profile(base), role_profile(changed))

    def test_translation_profile_tracks_only_models_used_by_preset(self):
        models = {
            "translator": ["fake/t"],
            "analyst": ["fake/a"],
            "editor": ["fake/e"],
            "fast": ["fake/f"],
        }

        def profile(quality, changed_role=None):
            changed = {role: list(values) for role, values in models.items()}
            if changed_role:
                changed[changed_role] = [f"fake/{changed_role}-changed"]
            return translation_model_profile(
                Config.from_dict({"llm": {"models": changed}, "quality": quality})
            )

        self.assertNotEqual(profile("quality"), profile("quality", "translator"))
        self.assertNotEqual(profile("quality"), profile("quality", "analyst"))
        self.assertEqual(profile("quality"), profile("quality", "editor"))
        self.assertEqual(profile("economy"), profile("economy", "analyst"))

    def test_role_profile_serializes_candidate_boundaries(self):
        single = Config.from_dict({"llm": {"models": {"translator": ["fake/a|fake/b"]}}})
        multiple = Config.from_dict({"llm": {"models": {"translator": ["fake/a", "fake/b"]}}})
        self.assertNotEqual(translator_model_profile(single), translator_model_profile(multiple))

    def test_role_profile_includes_base_url_for_custom_provider(self):
        first = Config.from_dict(
            {
                "llm": {
                    "models": {"translator": ["openai-compatible/model"]},
                    "base_url": "https://one.example/v1",
                }
            }
        )
        second = Config.from_dict(
            {
                "llm": {
                    "models": {"translator": ["openai-compatible/model"]},
                    "base_url": "https://two.example/v1",
                }
            }
        )
        self.assertNotEqual(translator_model_profile(first), translator_model_profile(second))
        self.assertIn(
            '"base_url":"https://one.example/v1"',
            translator_model_profile(first),
        )


class TestFakeClient(unittest.TestCase):
    def test_default(self):
        client = FakeClient()
        self.assertEqual(
            client.complete(
                [{"role": "user", "content": "x"}],
                agent="translator",
                operation="translate.batch",
            ),
            "",
        )
        self.assertEqual(
            client.complete_json(
                [{"role": "user", "content": "x"}],
                agent="translator",
                operation="translate.batch",
            ),
            [],
        )

    def test_handler_preserves_call_metadata(self):
        client = FakeClient(handler=lambda messages, agent, operation, json_mode: "hello")
        self.assertEqual(
            client.complete(
                [{"role": "user", "content": "x"}],
                stage="Translator",
                agent="translator",
                operation="translate.batch",
            ),
            "hello",
        )
        self.assertEqual(client.calls[0]["agent"], "translator")
        self.assertEqual(client.calls[0]["operation"], "translate.batch")


class TestProviderRequestCapabilities(unittest.TestCase):
    messages: ClassVar[list[dict[str, str]]] = [{"role": "user", "content": "translate"}]

    @staticmethod
    def _model(*, enabled: bool = True, effort: str = "high") -> ModelRef:
        return ModelRef(
            "deepseek",
            "deepseek-model",
            reasoning_enabled=enabled,
            reasoning_effort=effort,
        )

    def test_deepseek_enabled_and_disabled_thinking(self):
        from trans_novel.llm.providers.transport import build_request_kwargs
        from trans_novel.model_profiles import DIALECT_DEEPSEEK, ModelCapabilities

        capabilities = ModelCapabilities(
            request_dialect=DIALECT_DEEPSEEK,
            reasoning_efforts=frozenset({"high"}),
        )
        enabled = build_request_kwargs(
            capabilities,
            self._model(),
            self.messages,
            json_mode=True,
            max_tokens=10,
        )
        disabled = build_request_kwargs(
            capabilities,
            self._model(enabled=False),
            self.messages,
            max_tokens=10,
        )
        self.assertEqual(enabled["model"], "deepseek-model")
        self.assertEqual(enabled["response_format"], {"type": "json_object"})
        self.assertEqual(enabled["extra_body"], {"thinking": {"type": "enabled"}})
        self.assertEqual(enabled["reasoning_effort"], "high")
        self.assertEqual(enabled["max_tokens"], 4096)
        self.assertEqual(disabled["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertNotIn("reasoning_effort", disabled)
        self.assertEqual(disabled["max_tokens"], 10)

    def test_bailian_enabled_and_disabled_thinking(self):
        from trans_novel.llm.providers.transport import build_request_kwargs
        from trans_novel.model_profiles import DIALECT_BAILIAN, ModelCapabilities

        enabled = build_request_kwargs(
            ModelCapabilities(DIALECT_BAILIAN, frozenset({"high"})),
            self._model(),
            self.messages,
            max_tokens=10,
        )
        disabled = build_request_kwargs(
            ModelCapabilities(DIALECT_BAILIAN),
            self._model(enabled=False),
            self.messages,
            max_tokens=10,
        )
        self.assertEqual(enabled["extra_body"], {"enable_thinking": True})
        self.assertEqual(enabled["reasoning_effort"], "high")
        self.assertEqual(enabled["max_tokens"], 4096)
        self.assertEqual(disabled["extra_body"], {"enable_thinking": False})
        self.assertNotIn("reasoning_effort", disabled)
        self.assertEqual(disabled["max_tokens"], 10)

    def test_openai_and_openrouter_capabilities(self):
        from trans_novel.llm.providers.transport import build_request_kwargs
        from trans_novel.model_profiles import (
            DIALECT_OPENAI,
            DIALECT_OPENROUTER,
            ModelCapabilities,
        )

        efforts = frozenset({"high"})
        openai = build_request_kwargs(
            ModelCapabilities(DIALECT_OPENAI, efforts),
            self._model(),
            self.messages,
        )
        self.assertEqual(openai["reasoning_effort"], "high")
        router = build_request_kwargs(
            ModelCapabilities(DIALECT_OPENROUTER, efforts),
            self._model(enabled=False),
            self.messages,
        )
        self.assertEqual(router["extra_body"], {"reasoning": {"enabled": False}})

    def test_configured_openrouter_provider_route_is_sent(self):
        from trans_novel.config import ProviderRouting
        from trans_novel.llm.providers.transport import build_request_kwargs
        from trans_novel.model_profiles import DIALECT_OPENROUTER, ModelCapabilities

        kwargs = build_request_kwargs(
            ModelCapabilities(DIALECT_OPENROUTER),
            ModelRef("openrouter", "any/model", reasoning_enabled=False),
            self.messages,
            provider_routing=ProviderRouting(only=["tencent"], allow_fallbacks=False),
        )
        self.assertEqual(
            kwargs["extra_body"]["provider"],
            {"only": ["tencent"], "allow_fallbacks": False},
        )

    def test_responses_api_conversion_preserves_controls(self):
        from trans_novel.llm.providers.transport import (
            build_request_kwargs,
            build_responses_request_kwargs,
        )
        from trans_novel.model_profiles import DIALECT_OPENAI, ModelCapabilities

        chat = build_request_kwargs(
            ModelCapabilities(
                request_dialect=DIALECT_OPENAI,
                reasoning_efforts=frozenset({"low"}),
                supports_temperature=True,
            ),
            self._model(effort="low"),
            self.messages,
            json_mode=True,
            max_tokens=8,
            generation_options=GenerationOptions(temperature=0.1),
        )
        responses = build_responses_request_kwargs(chat)
        self.assertEqual(responses["input"], self.messages)
        self.assertEqual(responses["max_output_tokens"], 4096)
        self.assertEqual(responses["reasoning"], {"effort": "low"})
        self.assertEqual(responses["text"], {"format": {"type": "json_object"}})
        self.assertEqual(responses["temperature"], 0.1)
        self.assertNotIn("messages", responses)

    def test_unknown_capabilities_do_not_send_or_budget_reasoning(self):
        from trans_novel.llm.providers.transport import build_request_kwargs
        from trans_novel.model_profiles import ModelCapabilities

        kwargs = build_request_kwargs(
            ModelCapabilities(),
            self._model(),
            self.messages,
            max_tokens=100,
        )
        self.assertEqual(kwargs["max_tokens"], 100)
        self.assertNotIn("extra_body", kwargs)
        self.assertNotIn("reasoning_effort", kwargs)

    def test_unsupported_effort_is_disabled_instead_of_upgraded(self):
        from trans_novel.llm.providers.transport import build_request_kwargs
        from trans_novel.model_profiles import DIALECT_DEEPSEEK, ModelCapabilities

        kwargs = build_request_kwargs(
            ModelCapabilities(DIALECT_DEEPSEEK, frozenset({"high"})),
            self._model(effort="low"),
            self.messages,
        )
        self.assertEqual(kwargs["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertNotIn("reasoning_effort", kwargs)


class TestGenerationOptions(unittest.TestCase):
    def test_validation_boundaries_and_types(self):
        for temperature in (0.0, 2.0, 1, 0):
            GenerationOptions(temperature=temperature)
        for temperature in (float("nan"), float("inf"), -0.1, 2.1, True, "0.1"):
            with self.subTest(temperature=temperature), self.assertRaises((TypeError, ValueError)):
                GenerationOptions(temperature=temperature)
        GenerationOptions(seed=-(2**100))
        for seed in (True, 1.0, "1"):
            with self.subTest(seed=seed), self.assertRaises((TypeError, ValueError)):
                GenerationOptions(seed=seed)
        for field in ("require_catalogued_model", "require_thinking_disabled"):
            with self.subTest(field=field), self.assertRaises((TypeError, ValueError)):
                GenerationOptions(**{field: 1})

    def test_is_immutable(self):
        options = GenerationOptions(temperature=0.1)
        with self.assertRaises((AttributeError, TypeError)):
            options.temperature = 0.2


class TestOpenRouterTranslationCapabilities(unittest.TestCase):
    messages: ClassVar[list[dict[str, str]]] = [{"role": "user", "content": "translate"}]
    model_ids: ClassVar[tuple[str, ...]] = (
        "tencent/hy-mt2-30b-a3b",
        "tencent/hy-mt2-30b-a3b-20260521",
    )

    def test_every_hy_mt2_id_accepts_controlled_generation(self):
        from trans_novel.llm.providers.transport import build_request_kwargs
        from trans_novel.model_profiles import capabilities_for

        options = GenerationOptions(
            temperature=0.1,
            require_catalogued_model=True,
            require_thinking_disabled=True,
        )
        for model in self.model_ids:
            with self.subTest(model=model):
                model_ref = ModelRef("openrouter", model, reasoning_enabled=False)
                kwargs = build_request_kwargs(
                    capabilities_for("openrouter", model),
                    model_ref,
                    self.messages,
                    generation_options=options,
                )
                self.assertEqual(kwargs["temperature"], 0.1)
                self.assertNotIn("extra_body", kwargs)


class TestOpenCodeGLMCapabilities(unittest.TestCase):
    def test_glm_5_3_flash_accepts_controlled_generation(self):
        from trans_novel.llm.providers.transport import build_request_kwargs
        from trans_novel.model_profiles import capabilities_for

        model = "glm-5.3-flash"
        kwargs = build_request_kwargs(
            capabilities_for("opencode-go", model),
            ModelRef("opencode-go", model, reasoning_enabled=False),
            [{"role": "user", "content": "translate"}],
            generation_options=GenerationOptions(
                temperature=0.1,
                require_catalogued_model=True,
                require_thinking_disabled=True,
            ),
        )
        self.assertEqual(kwargs["temperature"], 0.1)
        self.assertNotIn("extra_body", kwargs)


class TestBailianGenerationCapabilities(unittest.TestCase):
    messages: ClassVar[list[dict[str, str]]] = [{"role": "user", "content": "translate"}]
    model_ids: ClassVar[tuple[str, ...]] = (
        "qwen3.7-flash",
        "qwen3.7-flash-2026-07-15",
        "qwen3.7-plus",
        "qwen3.7-plus-us",
        "qwen3.7-plus-2026-05-26",
        "qwen3.8-max",
        "deepseek-v4-flash",
        "deepseek-v4-flash-0731",
        "deepseek-v4-pro",
        "deepseek-v4-pro-us",
        "deepseek-v4-pro-0813",
    )

    def test_every_official_id_accepts_controlled_disabled_generation(self):
        from trans_novel.llm.providers.transport import build_request_kwargs
        from trans_novel.model_profiles import capabilities_for

        options = GenerationOptions(
            temperature=0.1,
            require_catalogued_model=True,
            require_thinking_disabled=True,
        )
        for model in self.model_ids:
            with self.subTest(model=model):
                model_ref = ModelRef("bailian", model, reasoning_enabled=False)
                kwargs = build_request_kwargs(
                    capabilities_for("bailian", model),
                    model_ref,
                    self.messages,
                    generation_options=options,
                )
                self.assertEqual(kwargs["temperature"], 0.1)
                self.assertEqual(kwargs["extra_body"], {"enable_thinking": False})

    def test_unknown_and_unsupported_seed_fail_closed(self):
        from trans_novel.llm.providers.transport import build_request_kwargs
        from trans_novel.model_profiles import capabilities_for

        unknown = ModelRef("bailian", "not-catalogued", reasoning_enabled=False)
        with self.assertRaises(ValueError):
            build_request_kwargs(
                capabilities_for("bailian", unknown.model),
                unknown,
                self.messages,
                generation_options=GenerationOptions(
                    temperature=0.1, require_catalogued_model=True
                ),
            )
        candidate = ModelRef("bailian", "qwen3.8-max", reasoning_enabled=False)
        with self.assertRaises(ValueError):
            build_request_kwargs(
                capabilities_for("bailian", candidate.model),
                candidate,
                self.messages,
                generation_options=GenerationOptions(seed=1),
            )

    def test_disabled_requirement_rejects_enabled_ref(self):
        from trans_novel.llm.providers.transport import build_request_kwargs
        from trans_novel.model_profiles import capabilities_for

        model_ref = ModelRef("bailian", "qwen3.8-max", reasoning_enabled=True)
        with self.assertRaises(ValueError):
            build_request_kwargs(
                capabilities_for("bailian", model_ref.model),
                model_ref,
                self.messages,
                generation_options=GenerationOptions(require_thinking_disabled=True),
            )

    def test_absent_options_preserve_request_kwargs(self):
        from trans_novel.llm.providers.transport import build_request_kwargs
        from trans_novel.model_profiles import capabilities_for

        model_ref = ModelRef("bailian", "qwen3.8-max", reasoning_enabled=False)
        capabilities = capabilities_for("bailian", model_ref.model)
        self.assertEqual(
            build_request_kwargs(capabilities, model_ref, self.messages),
            build_request_kwargs(
                capabilities,
                model_ref,
                self.messages,
                generation_options=None,
            ),
        )

    def test_qwen_enabled_thinking_omits_unverified_effort(self):
        from trans_novel.llm.providers.transport import build_request_kwargs
        from trans_novel.model_profiles import capabilities_for

        for model in ("qwen3.7-plus", "qwen3.8-max"):
            with self.subTest(model=model):
                model_ref = ModelRef("bailian", model, reasoning_enabled=True)
                kwargs = build_request_kwargs(
                    capabilities_for("bailian", model_ref.model),
                    model_ref,
                    self.messages,
                )
                self.assertEqual(kwargs["extra_body"], {"enable_thinking": True})
                self.assertNotIn("reasoning_effort", kwargs)

    def test_qwen_explicit_off_disables_thinking(self):
        from trans_novel.llm.providers.transport import build_request_kwargs
        from trans_novel.model_profiles import capabilities_for

        for model in ("qwen3.7-plus", "qwen3.8-max"):
            with self.subTest(model=model):
                model_ref = ModelRef("bailian", model, reasoning_enabled=False)
                kwargs = build_request_kwargs(
                    capabilities_for("bailian", model_ref.model),
                    model_ref,
                    self.messages,
                )
                self.assertEqual(kwargs["extra_body"], {"enable_thinking": False})
                self.assertNotIn("reasoning_effort", kwargs)


if __name__ == "__main__":
    unittest.main()
