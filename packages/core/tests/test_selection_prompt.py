"""Keyboard menus use arrow keys rather than numeric input."""

from io import StringIO

import pytest
from rich.console import Console
from wenyi_cli.commands import selection_prompt


@pytest.mark.parametrize(
    "keys, expected",
    [
        (["\x1b[B", "\r"], 1),
        (["\x1b[A", "\r"], 2),
        (["\x1b[B", "\x1b[A", "\r"], 0),
    ],
)
def test_terminal_arrows(monkeypatch, keys, expected):
    console = Console(file=StringIO(), force_terminal=True)
    monkeypatch.setattr(selection_prompt.sys.stdin, "isatty", lambda: True)
    events = iter(keys)
    monkeypatch.setattr(selection_prompt, "_read_key", lambda: next(events))
    assert selection_prompt.select(console, "Provider", ["A", "B", "C"]) == expected


def test_terminal_ctrl_c_cancels(monkeypatch):
    import typer

    console = Console(file=StringIO(), force_terminal=True)
    monkeypatch.setattr(selection_prompt.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(selection_prompt, "_read_key", lambda: "\x03")
    with pytest.raises(typer.Abort):
        selection_prompt.select(console, "Provider", ["A"])


def test_redirected_input_selects_by_label(monkeypatch):
    monkeypatch.setattr(selection_prompt.sys, "stdin", StringIO("B\n"))
    assert selection_prompt.select(Console(file=StringIO()), "Provider", ["A", "B"]) == 1


def test_redirected_eof_cancels(monkeypatch):
    monkeypatch.setattr(selection_prompt.sys, "stdin", StringIO())
    assert selection_prompt.select(Console(file=StringIO()), "Provider", ["A"]) is None
