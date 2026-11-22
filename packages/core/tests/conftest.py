"""Shared pytest fixtures for core tests."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\].*?\x07|\r")


_CORE_TESTS_DIR = Path(__file__).resolve().parent


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


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Turn warnings into errors for the core test suite.

    The repository-wide ``filterwarnings`` ini only pins individual messages
    to errors. This hook adds a blanket warnings-as-errors filter to the core
    domain first, so new warnings fail loudly here; other domains can adopt
    the same policy incrementally. Tests that declare their own
    ``filterwarnings`` marks keep control of their warning filters.

    ``PytestUnraisableExceptionWarning`` stays recording-only: it is raised for
    garbage-collected leftovers of earlier tests, so failing the currently
    running test would attribute the failure to the wrong test.
    """
    for item in items:
        if not item.path.is_relative_to(_CORE_TESTS_DIR):
            continue
        if item.get_closest_marker("filterwarnings") is None:
            item.add_marker(
                pytest.mark.filterwarnings(
                    "error",
                    "always::pytest.PytestUnraisableExceptionWarning",
                )
            )
