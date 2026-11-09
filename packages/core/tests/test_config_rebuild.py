"""Saved settings are backed up before reconstruction; new input stays strict."""

import os
import stat
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from wenyi_core.config import Config
from wenyi_core.config_rebuild import load_rebuilt_config, rebuild_config
from wenyi_core.i18n.languages import honorific_rule


def test_rebuild_keeps_valid_fields_and_drops_retired_values_without_aliases():
    original = {
        "language": {"source": "ja", "target": "en", "old_option": True},
        "llm": {"preset": "fake"},
        "pipeline": {
            "translation_mode": "best_of_three",
            "precision_concurrency": 1,
            "review": False,
            "rolling_context_tokens": 1024,
        },
        "output": {"bilingual": True, "bilingual_order": "obsolete", "unused": 1},
        "honorific": {"strategy": "drop"},
        "paths": {"state_dir": "keep-this-state"},
    }
    before = deepcopy(original)
    result = rebuild_config(original)
    config = Config.from_dict(result.document)
    assert result.changed
    assert config.pipeline.translation_mode == "standard"
    assert config.pipeline.review is False
    assert config.pipeline.rolling_context_tokens == 1024
    assert config.output.bilingual is True
    assert config.output.bilingual_order == "target_first"
    assert config.state_dir == "keep-this-state"
    assert (config.source_lang, config.target_lang) == ("ja", "en")
    assert config.llm.preset == "fake"
    assert "honorific" not in result.document
    assert original == before
    assert not rebuild_config(result.document).changed


def test_rebuild_prunes_nested_invalid_fields_without_losing_model_registry():
    original = {
        "llm": {
            "preset": "fake",
            "providers": {
                "custom": {
                    "kind": "openai-compatible",
                    "base_url": "https://example.invalid",
                    "retired": True,
                }
            },
        }
    }
    result = rebuild_config(original)
    assert result.removed == ("llm.providers.custom.retired",)
    config = Config.from_dict(result.document)
    assert config.llm.providers["custom"].base_url == "https://example.invalid"
    assert config.llm.preset == "fake"


def test_single_provider_configuration_is_discarded_not_converted():
    original = {
        "llm": {"provider": "deepseek", "tiers": {"fast": {"options": {"thinking": False}}}},
        "pipeline": {"review_agent_tier": "cheap", "review": False},
    }
    before = deepcopy(original)
    with pytest.raises(ValueError):
        Config.from_dict(original)
    result = rebuild_config(original)
    config = Config.from_dict(result.document)
    assert config.llm == Config().llm
    assert config.llm.routes == {}
    assert config.pipeline.review is False
    assert original == before
    assert not rebuild_config(result.document).changed


@pytest.mark.parametrize("preset", ["removed-provider", [], {}, False])
def test_rebuild_invalid_preset_preserves_independent_valid_budget(preset):
    result = rebuild_config({"llm": {"preset": preset, "budget": {"max_requests": 17}}})
    config = Config.from_dict(result.document)
    assert result.changed
    assert config.llm.preset == Config().llm.preset
    assert config.llm.budget.max_requests == 17
    assert not rebuild_config(result.document).changed


@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_fields_are_rebuilt_in_schema_order_not_document_order(reverse):
    fields = [
        ("translation_mode", "three_draft_synthesis"),
        ("polish", False),
        ("rolling_context_tokens", 1024),
    ]
    pipeline = dict(reversed(fields) if reverse else fields)
    result = rebuild_config({"pipeline": pipeline})
    config = Config.from_dict(result.document)
    assert config.pipeline.translation_mode == "standard"
    assert config.pipeline.polish is False
    assert config.pipeline.rolling_context_tokens == 1024


def test_rebuild_drops_invalid_dependency_group_instead_of_inventing_model_references():
    result = rebuild_config(
        {
            "llm": {"models": {"missing": {"model": "model", "provider": "gone"}}},
            "pipeline": {"review": False},
        }
    )
    config = Config.from_dict(result.document)
    assert result.changed
    assert "missing" not in config.llm.models
    assert config.pipeline.review is False


@pytest.mark.parametrize(
    "document",
    [
        {"honorific": {"strategy": "normalize"}},
        {"pipeline": {"translation_mode": "best_of_three"}},
        {"pipeline": {"precision_concurrency": 1}},
    ],
)
def test_strict_configuration_does_not_accept_retired_fields(document):
    with pytest.raises(ValueError):
        Config.from_dict(document)
    with pytest.raises(ValidationError):
        Config.model_validate({"honorific_strategy": "drop"})


def test_honorific_guidance_is_owned_by_language_pairs():
    japanese = Config.from_dict({"language": {"source": "ja", "target": "zh"}})
    guidance = dict(japanese.language_policy().prompt_values)
    assert honorific_rule("ja", "zh") in guidance["configured_lang_guidance"]
    assert "先輩 → 前辈" in guidance["configured_lang_guidance"]
    assert "先輩" not in honorific_rule("fr", "en")
    assert "honorific_strategy" not in japanese.model_dump()
    assert guidance["configured_lang_guidance"] == guidance["lang_guidance"]


@pytest.mark.parametrize("invalid", ["[]", "false", "language: {target: typo}"])
def test_cli_invalid_saved_values_are_backed_up_before_using_defaults(tmp_path, invalid):
    path = tmp_path / "config.yaml"
    path.write_text(invalid, encoding="utf-8")
    assert load_rebuilt_config(path).target_lang == Config().target_lang
    backups = list(tmp_path.glob("config.yaml.backup-*.yaml"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == invalid


def test_cli_reconstruction_backs_up_exact_bytes_and_is_idempotent(tmp_path):
    path = tmp_path / "config.yaml"
    original = b"# Keep the original comments.\nllm: {preset: fake}\nhonorific: {strategy: drop}\n"
    path.write_bytes(original)
    with ThreadPoolExecutor(max_workers=2) as pool:
        configs = list(pool.map(lambda _: load_rebuilt_config(path), range(2)))
    assert all(config.llm.preset == "fake" for config in configs)
    backups = list(tmp_path.glob("config.yaml.backup-*.yaml"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert yaml.safe_load(path.read_text()) == {"llm": {"preset": "fake"}}
    assert load_rebuilt_config(path).llm.preset == "fake"
    assert list(tmp_path.glob("config.yaml.backup-*.yaml")) == backups


@pytest.mark.skipif(os.name == "nt", reason="POSIX file permissions")
def test_cli_reconstruction_preserves_private_file_permissions(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("honorific: {strategy: drop}\n", encoding="utf-8")
    path.chmod(0o600)
    load_rebuilt_config(path)
    backup = next(tmp_path.glob("config.yaml.backup-*.yaml"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


@pytest.mark.parametrize("fail_backup", [True, False])
def test_cli_reconstruction_failure_keeps_original_settings(tmp_path, monkeypatch, fail_backup):
    from wenyi_core import config_rebuild

    path = tmp_path / "config.yaml"
    original = b"llm: {preset: fake}\nhonorific: {strategy: drop}\n"
    path.write_bytes(original)
    publish = config_rebuild._publish_config

    def fail(target, data, mode):
        if (Path(target) != path) is fail_backup:
            raise OSError("synthetic full disk")
        return publish(target, data, mode)

    monkeypatch.setattr(config_rebuild, "_publish_config", fail)
    with pytest.raises(OSError, match="full disk"):
        load_rebuilt_config(path)
    assert path.read_bytes() == original
    backups = list(tmp_path.glob("config.yaml.backup-*.yaml"))
    assert len(backups) == (0 if fail_backup else 1)
    if backups:
        assert backups[0].read_bytes() == original


def test_valid_cli_file_is_not_rewritten(tmp_path):
    path = tmp_path / "config.yaml"
    original = "# Retain comments.\nllm: {preset: fake}\npipeline: {review: false}\n"
    path.write_text(original, encoding="utf-8")
    assert not load_rebuilt_config(path).pipeline.review
    assert path.read_text(encoding="utf-8") == original
    assert list(tmp_path.iterdir()) == [path]
