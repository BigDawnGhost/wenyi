"""The ``.env`` file that ``wenyi model`` writes beside ``config.yaml``."""

from __future__ import annotations

import os
import stat

import pytest
from wenyi_core.envfile import (
    env_file_path,
    export_lines,
    load_env_file,
    parse_env_line,
    quote_env_value,
    read_env_values,
    write_env_values,
)


def test_env_file_lives_beside_the_configuration(tmp_path):
    assert env_file_path(tmp_path / "config.yaml") == tmp_path / ".env"
    assert env_file_path(tmp_path / "nested" / "config.yaml") == tmp_path / "nested" / ".env"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("DEEPSEEK_API_KEY=sk-1", ("DEEPSEEK_API_KEY", "sk-1")),
        ("export DEEPSEEK_API_KEY='sk 1'", ("DEEPSEEK_API_KEY", "sk 1")),
        (
            'export WENYI_CODEX_OAUTH=\'{"access_token":"t"}\'',
            ("WENYI_CODEX_OAUTH", '{"access_token":"t"}'),
        ),
        ('OPENROUTER_API_KEY="quoted"', ("OPENROUTER_API_KEY", "quoted")),
        ("  SPACED  =  value  ", ("SPACED", "value")),
        ("GEMINI_API_KEY=value # trailing note", ("GEMINI_API_KEY", "value")),
        ("", None),
        ("   ", None),
        ("# a comment", None),
        ("not an assignment", None),
        ("1INVALID=x", None),
    ],
)
def test_parse_env_line_reads_assignments_and_ignores_noise(line, expected):
    assert parse_env_line(line) == expected


def test_credentials_are_written_quoting_shell_metacharacters(tmp_path):
    path = tmp_path / ".env"
    write_env_values(path, {"GEMINI_API_KEY": "it's $HOME `x`"})

    assert read_env_values(path) == {"GEMINI_API_KEY": "it's $HOME `x`"}
    assert path.read_text() == "GEMINI_API_KEY='it'\\''s $HOME `x`'\n"
    assert export_lines({"GEMINI_API_KEY": "value"}) == "export GEMINI_API_KEY='value'"


def test_writing_keeps_every_other_line_and_the_file_private(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# my credentials\n"
        "DEEPSEEK_API_KEY='old'\n"
        "\n"
        "MINERU_API_KEY='kept'\n"
        "DEEPSEEK_API_KEY='stale duplicate'\n"
    )

    write_env_values(path, {"DEEPSEEK_API_KEY": "new"})

    assert path.read_text() == (
        "# my credentials\nDEEPSEEK_API_KEY='new'\n\nMINERU_API_KEY='kept'\n"
    )
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_writing_appends_a_missing_variable_and_rejects_unusable_input(tmp_path):
    path = tmp_path / ".env"
    write_env_values(path, {"ANTHROPIC_API_KEY": "sk-ant"})
    write_env_values(path, {"OPENROUTER_API_KEY": "sk-or"})

    assert read_env_values(path) == {
        "ANTHROPIC_API_KEY": "sk-ant",
        "OPENROUTER_API_KEY": "sk-or",
    }
    with pytest.raises(ValueError, match="environment variable name"):
        write_env_values(path, {"not a name": "value"})
    with pytest.raises(ValueError, match="single line"):
        write_env_values(path, {"ANTHROPIC_API_KEY": "two\nlines"})


def test_loading_never_replaces_an_exported_variable(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    write_env_values(path, {"DEEPSEEK_API_KEY": "from-file", "GEMINI_API_KEY": "from-file"})
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-shell")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    loaded = load_env_file(path)

    assert loaded == ("GEMINI_API_KEY",)
    assert os.environ["DEEPSEEK_API_KEY"] == "from-shell"
    assert os.environ["GEMINI_API_KEY"] == "from-file"


def test_loading_can_replace_when_the_caller_asks(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    write_env_values(path, {"DEEPSEEK_API_KEY": "from-file"})
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-shell")

    assert load_env_file(path, override=True) == ("DEEPSEEK_API_KEY",)
    assert os.environ["DEEPSEEK_API_KEY"] == "from-file"


def test_missing_files_read_and_write_as_empty(tmp_path):
    path = tmp_path / "absent" / ".env"
    assert read_env_values(path) == {}
    assert load_env_file(path) == ()
    assert quote_env_value("plain") == "'plain'"
