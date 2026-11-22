"""The build hook must fail with guidance, not a raw linker error, without Rust."""

import subprocess

import pytest
from hatch_build import CustomBuildHook, missing_rust_tools, require_rust_toolchain


@pytest.fixture(autouse=True)
def _clear_rustc_overrides(monkeypatch):
    for name in ("RUSTC", "CARGO_BUILD_RUSTC"):
        monkeypatch.delenv(name, raising=False)


def _forbid_native_build(*args, **kwargs):
    pytest.fail("the native build must not start without the Rust toolchain")


def test_missing_rust_tools_reports_nothing_when_toolchain_present(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    assert missing_rust_tools() == []


def test_missing_rust_tools_reports_missing_cargo_and_rustc(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)

    assert missing_rust_tools() == ["cargo", "rustc"]


def test_missing_rust_tools_trusts_explicit_rustc_override(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setenv("RUSTC", "/opt/rust/bin/rustc")

    assert missing_rust_tools() == ["cargo"]


def test_require_rust_toolchain_returns_when_present(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")

    assert require_rust_toolchain() is None


def test_require_rust_toolchain_exits_with_install_guidance(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(subprocess, "run", _forbid_native_build)

    with pytest.raises(SystemExit) as exited:
        require_rust_toolchain()

    message = str(exited.value.code)
    assert "cargo and rustc" in message
    assert "were not found" in message
    assert "https://rustup.rs" in message
    assert "winget install Rustlang.Rustup" in message
    assert "uv sync" in message


def test_require_rust_toolchain_names_a_single_missing_tool(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None if name == "cargo" else "/usr/bin/cargo")

    with pytest.raises(SystemExit) as exited:
        require_rust_toolchain()

    message = str(exited.value.code)
    assert "but cargo was not found" in message
    assert "rustc" not in message.replace("https://rustup.rs", "")


def test_initialize_preflights_the_toolchain_before_building(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    monkeypatch.setattr(subprocess, "run", _forbid_native_build)
    hook = CustomBuildHook(str(tmp_path), {}, {}, None, "", "")

    with pytest.raises(SystemExit):
        hook.initialize("editable", {"force_include": {}, "artifacts": []})
