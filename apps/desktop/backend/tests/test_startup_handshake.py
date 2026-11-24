"""Startup handshake between the native shell version and the Python engine."""

from __future__ import annotations

import pytest
from wenyi_desktop import main


def test_matching_versions_start_normally(monkeypatch, tmp_path):
    monkeypatch.setenv("WENYI_DESKTOP_VERSION", "1.2.3")
    monkeypatch.setattr(main.importlib.metadata, "version", lambda name: "1.2.3")
    monkeypatch.chdir(tmp_path)

    main.verify_version_handshake()


def test_version_mismatch_refuses_to_start(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("WENYI_DESKTOP_VERSION", "1.2.3")
    monkeypatch.setattr(main.importlib.metadata, "version", lambda name: "4.5.6")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exit_info:
        main.verify_version_handshake()

    assert exit_info.value.code != 0
    stderr = capsys.readouterr().err
    assert "1.2.3" in stderr
    assert "4.5.6" in stderr


def test_unknown_engine_version_only_warns(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("WENYI_DESKTOP_VERSION", "1.2.3")

    def missing(name):
        raise main.importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(main.importlib.metadata, "version", missing)
    monkeypatch.chdir(tmp_path)

    main.verify_version_handshake()

    assert "unknown" in capsys.readouterr().err


def test_missing_shell_version_only_warns(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("WENYI_DESKTOP_VERSION", raising=False)
    monkeypatch.setattr(main.importlib.metadata, "version", lambda name: "1.2.3")
    monkeypatch.chdir(tmp_path)

    main.verify_version_handshake()

    stderr = capsys.readouterr().err
    assert "WENYI_DESKTOP_VERSION" in stderr
