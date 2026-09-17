"""Invocation isolation and the installed console entry point."""

from importlib.metadata import distribution
from io import StringIO

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner
from wenyi_cli.cli import create_app, main
from wenyi_cli.commands.context import current_context


def test_help_paths_and_overrides_do_not_leak_between_invocations(tmp_path, monkeypatch):
    first_path = tmp_path / "first.yaml"
    second_path = tmp_path / "second.yaml"
    for path, target in [(first_path, "zh"), (second_path, "fr")]:
        path.write_text(
            f"llm:\n  preset: fake\nlanguage:\n  target: {target}\npipeline:\n  polish: true\n",
            encoding="utf-8",
        )
    console = Console(file=StringIO())
    app = create_app(console=console)
    observed = []
    credential_checks = []

    class Client:
        def validate_credentials(self, operations):
            credential_checks.append(tuple(operations))

    monkeypatch.setattr("wenyi_core.llm.factory.build_client", lambda config: Client())

    @app.command()
    def probe(ctx: typer.Context, polish: bool = typer.Option(True, "--polish/--no-polish")):
        invocation = current_context()
        assert invocation is ctx.find_root().obj
        assert invocation.console is console
        config = invocation.load_config()
        if not polish:
            config.pipeline.polish = False
        invocation.validate_api_configuration(config, "translate")
        observed.append((invocation, config.target_lang, config.pipeline.polish))

    runner = CliRunner()
    help_result = runner.invoke(app, ["-c", str(first_path), "probe", "--help"])
    first = runner.invoke(app, ["-c", str(first_path), "probe", "--no-polish"])
    second = runner.invoke(app, ["--config=" + str(second_path), "probe"])
    assert [r.exit_code for r in (help_result, first, second)] == [0, 0, 0]
    assert [
        (c.config_path, c.skip_api_check, language, polish) for c, language, polish in observed
    ] == [
        (str(first_path), False, "zh", False),
        (str(second_path), False, "fr", True),
    ]
    assert observed[0][0] is not observed[1][0]
    assert len(credential_checks) == 2
    with pytest.raises(RuntimeError):
        current_context()


def test_installed_entry_point_runs_real_bootstrap_before_help(tmp_path, monkeypatch, capsys):
    entries = {
        entry.name: entry
        for entry in distribution("wenyi-cli").entry_points
        if entry.group == "console_scripts"
    }
    assert set(entries) == {"wenyi"}
    entry = entries["wenyi"]
    assert entry.value == "wenyi_cli.main:main"
    assert entry.load() is main
    config_path = tmp_path / "entry.yaml"
    monkeypatch.setattr("sys.argv", ["wenyi", "--config", str(config_path), "--help"])
    with pytest.raises(SystemExit) as exit_info:
        entry.load()()
    assert exit_info.value.code == 0
    assert config_path.is_file()
    assert "translate" in capsys.readouterr().out


def test_early_argument_error_still_creates_selected_config(tmp_path):
    config_path = tmp_path / "invalid-command.yaml"
    result = CliRunner().invoke(create_app(), ["-c", str(config_path), "unknown-command"])
    assert result.exit_code == 2
    assert config_path.is_file()
