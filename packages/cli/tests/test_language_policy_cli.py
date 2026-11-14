"""Developer diagnostics inspect built-in policies without exposing YAML switches."""

import json

import pytest
from rich.text import Text
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
    assert "preserve_source_ruby" not in preview["export"]["export"]
    assert config.language_document() == {"source": "ja", "target": "zh-Hant"}


@pytest.mark.parametrize("option", ["--prompt-genre", "--experimental-prompt-genre"])
def test_prompt_genre_is_invocation_local_and_does_not_change_config(tmp_path, option):
    path = tmp_path / "config.yaml"
    original = "language: {source: en, target: zh}\n"
    path.write_text(original, encoding="utf-8")
    for options, expected in (
        ([option, "academic"], "academic"),
        ([], "literary"),
        ([option, "literary"], "literary"),
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


@pytest.mark.parametrize("option", ["--prompt-genre", "--experimental-prompt-genre"])
@pytest.mark.parametrize("genre", ["neutral", "Academic", "../academic", ""])
def test_prompt_genre_rejects_unknown_values_before_workflows(tmp_path, option, genre):
    path = tmp_path / "config.yaml"
    path.write_text("language: {source: en, target: zh}\n", encoding="utf-8")
    result = CliRunner().invoke(app, ["--config", str(path), option, genre, "language-policy"])
    assert result.exit_code == 2
    assert "Invalid value" in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize("force_terminal", [False, True])
def test_help_advertises_translation_style_as_a_supported_option(
    tmp_path, monkeypatch, force_terminal
):
    monkeypatch.setattr("typer.rich_utils.FORCE_TERMINAL", force_terminal)
    result = CliRunner().invoke(app, ["--config", str(tmp_path / "config.yaml"), "--help"])
    assert result.exit_code == 0, result.output
    if force_terminal:
        assert "\x1b[" in result.output
    output = Text.from_ansi(result.output).plain
    assert "long-form texts" in output
    assert "--prompt-genre" in output
    assert "Translation style" in output
    assert "literary" in output
    assert "academic" in output
    assert "--experimental-prompt-genre" not in output
    assert "CLI-only experimental prompt genre" not in output


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("genre", ["literary", "academic"])
@pytest.mark.parametrize("conflicting", [False, True])
def test_prompt_genre_and_legacy_option_must_agree(tmp_path, reverse, genre, conflicting):
    path = tmp_path / "config.yaml"
    path.write_text("language: {source: en, target: zh}\n", encoding="utf-8")
    legacy_genre = ("literary" if genre == "academic" else "academic") if conflicting else genre
    options = [["--prompt-genre", genre], ["--experimental-prompt-genre", legacy_genre]]
    if reverse:
        options.reverse()
    result = CliRunner().invoke(
        app, ["--config", str(path), *options[0], *options[1], "language-policy"]
    )
    if conflicting:
        assert result.exit_code == 2, result.output
        assert "Conflicting translation styles" in result.output
        assert "Traceback" not in result.output
    else:
        assert result.exit_code == 0, result.output
        preview = json.loads(result.output)
        assert preview["translation"]["context"]["genre"] == genre


def test_policy_diagnostics_reject_unavailable_format_before_execution(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("language: {source: en, target: zh}\n")
    result = CliRunner().invoke(
        app, ["--config", str(path), "language-policy", "--format", "invalid"]
    )
    assert result.exit_code == 1
    assert "Unsupported language policy export format" in result.output
    assert "Traceback" not in result.output
