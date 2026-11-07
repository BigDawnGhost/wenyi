"""Developer diagnostics inspect built-in policies without exposing YAML switches."""

import json

import pytest
from typer.testing import CliRunner
from wenyi_cli.cli import app
from wenyi_core.config import _DEFAULT_CONFIG_YAML, Config, parse_config_yaml


@pytest.mark.parametrize("field", ["operations", "accepted_policy_fingerprint"])
def test_yaml_rejects_language_policy_configuration(tmp_path, field):
    path = tmp_path / "config.yaml"
    path.write_text(f"language:\n  source: en\n  target: zh\n  {field}: {{}}\n")
    with pytest.raises(ValueError, match="unknown language configuration fields"):
        Config.load(str(path))
    assert field not in parse_config_yaml(_DEFAULT_CONFIG_YAML)["language"]
    assert field not in Config.model_fields


def test_builtin_policy_diagnostics_need_no_credentials(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("language: {source: ja, target: zh-Hant}\npipeline: {polish: off}\n")
    result = CliRunner().invoke(app, ["--config", str(path), "language-policy", "--format", "epub"])
    assert result.exit_code == 0, result.output
    preview = json.loads(result.output)
    config = Config.load(str(path))
    assert config.pipeline.polish is False
    assert preview["revision"]["fingerprint"] == config.language_policy_revision()
    assert preview["export"]["export"]["language_tag"] == "zh-Hant"
    assert preview["export"]["export"]["preserve_source_ruby"] is True
    assert config.language_document() == {"source": "ja", "target": "zh-Hant"}


def test_experimental_genre_is_cli_only_and_does_not_leak_between_invocations(tmp_path):
    path = tmp_path / "config.yaml"
    original = "language: {source: en, target: zh}\n"
    path.write_text(original, encoding="utf-8")
    for options, expected in (
        (["--experimental-prompt-genre", "academic"], "academic"),
        ([], "literary"),
        (["--experimental-prompt-genre", "literary"], "literary"),
    ):
        result = CliRunner().invoke(
            app, ["--config", str(path), *options, "language-policy", "--format", "docx"]
        )
        assert result.exit_code == 0, result.output
        preview = json.loads(result.output)
        assert all(
            preview[phase]["context"]["genre"] == expected
            for phase in ("analysis", "translation", "review")
        )
        assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("genre", ["neutral", "../academic", ""])
def test_experimental_genre_rejects_unknown_values_before_workflows(tmp_path, genre):
    path = tmp_path / "config.yaml"
    path.write_text("language: {source: en, target: zh}\n", encoding="utf-8")
    result = CliRunner().invoke(
        app, ["--config", str(path), "--experimental-prompt-genre", genre, "language-policy"]
    )
    assert result.exit_code == 2
    assert "Invalid value" in result.output
    assert "Traceback" not in result.output


def test_policy_diagnostics_reject_unavailable_format_before_execution(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("language: {source: en, target: zh}\n")
    result = CliRunner().invoke(
        app, ["--config", str(path), "language-policy", "--format", "invalid"]
    )
    assert result.exit_code == 1
    assert "Unsupported language policy export format" in result.output
    assert "Traceback" not in result.output
