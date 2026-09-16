"""Shared pytest fixtures for core tests."""

from __future__ import annotations

import re

import pytest

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\].*?\x07|\r")


def plain_text(value: str) -> str:
    """Strip ANSI/control sequences so CLI assertions stay stable under Rich."""
    return _ANSI_RE.sub("", value)


@pytest.fixture(autouse=True)
def _plain_rich_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prefer colorless consoles in tests; Progress may still emit markup."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)

    from rich.console import Console as RichConsole

    def console_factory(*args, **kwargs):
        kwargs = dict(kwargs)
        kwargs["no_color"] = True
        kwargs.setdefault("force_terminal", False)
        return RichConsole(*args, **kwargs)

    monkeypatch.setattr("rich.console.Console", console_factory)
