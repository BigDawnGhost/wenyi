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


def test_policy_diagnostics_reject_unavailable_format_before_execution(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("language: {source: en, target: zh}\n")
    result = CliRunner().invoke(
        app, ["--config", str(path), "language-policy", "--format", "invalid"]
    )
    assert result.exit_code == 1
    assert "Unsupported language policy export format" in result.output
    assert "Traceback" not in result.output


def test_subtitle_diagnostics_reject_an_explicit_book_format(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("language: {source: en, target: zh}\n")
    runner = CliRunner()
    result = runner.invoke(app, ["--config", str(path), "language-policy", "--subtitles"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["export"]["context"]["format"] == "srt"
    result = runner.invoke(
        app, ["--config", str(path), "language-policy", "--subtitles", "--format", "epub"]
    )
    assert result.exit_code == 1
    assert "Unsupported language policy export format: epub" in result.output
