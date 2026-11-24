"""Startup handshake between the native shell version and the Python engine."""

from __future__ import annotations

import pytest
from wenyi_desktop import main
from wenyi_desktop.diagnostics import StartupFailure, StartupFailureCode


@pytest.mark.parametrize(
    ("native", "python"),
    [
        ("1.2.3", "1.2.3"),
        ("1.2.3-rc.1", "1.2.3rc1"),
        ("1.2.3-dev.4+g123.fresh", "1.2.3.dev4+g123.fresh"),
        ("1.2.3-rc.1.dev.4+g123", "1.2.3rc1.dev4+g123"),
    ],
)
def test_matching_versions_start_normally(monkeypatch, native, python):
    monkeypatch.setenv("WENYI_DESKTOP_VERSION", native)
    monkeypatch.setenv("WENYI_DESKTOP_ENGINE_VERSION", python)
    monkeypatch.setattr(main.importlib.metadata, "version", lambda name: python)

    main.verify_version_handshake()


def test_legacy_launcher_still_accepts_equal_versions(monkeypatch):
    monkeypatch.delenv("WENYI_DESKTOP_ENGINE_VERSION", raising=False)
    monkeypatch.setenv("WENYI_DESKTOP_VERSION", "1.2.3")
    monkeypatch.setattr(main.importlib.metadata, "version", lambda name: "1.2.3")

    main.verify_version_handshake()


@pytest.mark.parametrize(
    ("native", "expected", "installed"),
    [
        ("1.2.3", None, "4.5.6"),
        ("1.2.3", "", "1.2.3"),
        ("1.2.3-rc.1", "1.2.3rc1", "1.2.3rc2"),
        ("1.2.3-rc.1", "1.2.3rc1", "1.2.3"),
        ("1.2.3-dev.4+g123", "1.2.3.dev4+g123", "1.2.3.dev5+g123"),
        ("1.2.3-dev.4+g123", "1.2.3.dev4+g123", "1.2.3.dev4+g456"),
    ],
)
def test_version_mismatch_refuses_to_start(monkeypatch, native, expected, installed):
    monkeypatch.setenv("WENYI_DESKTOP_VERSION", native)
    if expected is None:
        monkeypatch.delenv("WENYI_DESKTOP_ENGINE_VERSION", raising=False)
    else:
        monkeypatch.setenv("WENYI_DESKTOP_ENGINE_VERSION", expected)
    monkeypatch.setattr(main.importlib.metadata, "version", lambda name: installed)

    with pytest.raises(StartupFailure) as error:
        main.verify_version_handshake()

    assert error.value.code is StartupFailureCode.VERSION_MISMATCH


def test_unknown_engine_version_only_warns(monkeypatch, capsys):
    monkeypatch.setenv("WENYI_DESKTOP_ENGINE_VERSION", "1.2.3")

    def missing(name):
        raise main.importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(main.importlib.metadata, "version", missing)

    main.verify_version_handshake()

    assert "unknown" in capsys.readouterr().err


def test_missing_shell_version_only_warns(monkeypatch, capsys):
    monkeypatch.delenv("WENYI_DESKTOP_VERSION", raising=False)
    monkeypatch.delenv("WENYI_DESKTOP_ENGINE_VERSION", raising=False)
    monkeypatch.setattr(main.importlib.metadata, "version", lambda name: "1.2.3")

    main.verify_version_handshake()

    stderr = capsys.readouterr().err
    assert "WENYI_DESKTOP_VERSION" in stderr
