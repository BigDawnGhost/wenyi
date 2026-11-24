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


def test_dev_suffix_on_same_release_starts_normally(monkeypatch, tmp_path):
    monkeypatch.setenv("WENYI_DESKTOP_VERSION", "1.2.3")
    monkeypatch.setattr(main.importlib.metadata, "version", lambda name: "1.2.3.dev5+g1a2b3c4")
    monkeypatch.chdir(tmp_path)

    main.verify_version_handshake()


def test_local_version_suffix_on_same_release_starts_normally(monkeypatch, tmp_path):
    monkeypatch.setenv("WENYI_DESKTOP_VERSION", "1.2.3")
    monkeypatch.setattr(main.importlib.metadata, "version", lambda name: "1.2.3+build.7")
    monkeypatch.chdir(tmp_path)

    main.verify_version_handshake()


def test_pre_release_shell_matches_pre_release_engine(monkeypatch, tmp_path):
    monkeypatch.setenv("WENYI_DESKTOP_VERSION", "1.2.3-rc.1")
    monkeypatch.setattr(main.importlib.metadata, "version", lambda name: "1.2.3rc1")
    monkeypatch.chdir(tmp_path)

    main.verify_version_handshake()


def test_pre_release_shell_refuses_a_different_release(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("WENYI_DESKTOP_VERSION", "1.2.3-rc.1")
    monkeypatch.setattr(main.importlib.metadata, "version", lambda name: "1.2.4rc1")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit):
        main.verify_version_handshake()
    assert "1.2.4rc1" in capsys.readouterr().err


def test_release_segments_pad_to_match(monkeypatch, tmp_path):
    monkeypatch.setenv("WENYI_DESKTOP_VERSION", "1.2")
    monkeypatch.setattr(main.importlib.metadata, "version", lambda name: "1.2.0")
    monkeypatch.chdir(tmp_path)

    main.verify_version_handshake()


def test_dev_suffix_does_not_hide_a_release_mismatch(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("WENYI_DESKTOP_VERSION", "1.2.3")
    monkeypatch.setattr(main.importlib.metadata, "version", lambda name: "1.2.4.dev1")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit):
        main.verify_version_handshake()
    assert "1.2.4.dev1" in capsys.readouterr().err


def test_unparseable_versions_still_require_an_exact_match(monkeypatch, tmp_path):
    monkeypatch.setenv("WENYI_DESKTOP_VERSION", "1.2.3")
    monkeypatch.setattr(main.importlib.metadata, "version", lambda name: "mystery-build")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit):
        main.verify_version_handshake()


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
