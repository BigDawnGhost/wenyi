"""Offline validation of the native Windows application's PE subsystem."""

import importlib.util
import struct
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts/desktop_pe.py"


def pe_image(magic=0x20B, subsystem=2):
    """Construct a minimal PE header with one complete section table."""
    size = 240 if magic == 0x20B else 224
    data = bytearray(128 + 24 + size + 40)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 60, 128)
    data[128:132] = b"PE\0\0"
    struct.pack_into("<HH", data, 132, 0x8664 if magic == 0x20B else 0x14C, 1)
    struct.pack_into("<HH", data, 148, size, 2)
    struct.pack_into("<H", data, 152, magic)
    struct.pack_into("<H", data, 152 + 68, subsystem)
    return data


def validator():
    spec = importlib.util.spec_from_file_location("desktop_pe", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("magic", [0x10B, 0x20B])
def test_gui_pe_headers(tmp_path, magic):
    binary = tmp_path / "wenyi-desktop.exe"
    binary.write_bytes(pe_image(magic))
    validator().verify_gui_subsystem(binary)


@pytest.mark.parametrize(
    ("offset", "replacement", "message"),
    [
        (0, b"NO", "DOS signature"),
        (60, struct.pack("<I", 32), "PE offset"),
        (60, struct.pack("<I", 0xFFFFFFFF), "Truncated PE"),
        (128, b"NOPE", "PE signature"),
        (134, b"\0\0", "no sections"),
        (148, struct.pack("<H", 70), "optional header size"),
        (150, b"\0\0", "executable image"),
        (152, b"\0\0", "optional header magic"),
        (220, struct.pack("<H", 3), "Windows CUI=3"),
        (220, struct.pack("<H", 1), "subsystem 1"),
        (260, struct.pack("<I", 17), "data directories exceed header"),
    ],
)
def test_bad_headers_fail(tmp_path, offset, replacement, message):
    data = pe_image()
    data[offset : offset + len(replacement)] = replacement
    binary = tmp_path / "bad.exe"
    binary.write_bytes(data)
    with pytest.raises(ValueError, match=message):
        validator().verify_gui_subsystem(binary)


@pytest.mark.parametrize("length", [0, 2, 63, 128, 151, 153, 221, 391, 431])
def test_truncated_headers_fail(tmp_path, length):
    binary = tmp_path / "truncated.exe"
    binary.write_bytes(pe_image()[:length])
    with pytest.raises(ValueError, match="Truncated"):
        validator().verify_gui_subsystem(binary)


def test_cli_rejects_console_executable_without_traceback(tmp_path):
    binary = tmp_path / "wenyi-desktop.exe"
    binary.write_bytes(pe_image(subsystem=3))
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(binary)], capture_output=True, text=True
    )
    assert result.returncode == 1
    assert "Windows CUI=3" in result.stderr
    assert "Traceback" not in result.stderr


def test_cli_missing_file_is_clear(tmp_path):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(tmp_path / "missing.exe")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "missing.exe" in result.stderr
    assert "Traceback" not in result.stderr


def test_cli_accepts_gui_executable(tmp_path):
    binary = tmp_path / "wenyi-desktop.exe"
    binary.write_bytes(pe_image())
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(binary)], capture_output=True, text=True
    )
    assert result.returncode == 0
    assert "Windows GUI=2" in result.stdout
    assert not result.stderr


def test_windows_workflow_checks_native_binary_after_build():
    workflow = (SCRIPT.parent.parent / ".github/workflows/desktop.yml").read_text()
    step = """      - name: Verify native Windows GUI subsystem
        if: runner.os == 'Windows'
        run: uv run --no-project python scripts/desktop_pe.py apps/desktop/target/release/wenyi-desktop.exe
"""
    assert step in workflow
    assert workflow.index("pnpm desktop:build") < workflow.index(step)
    assert workflow.index(step) < workflow.index("Collect standalone installers")
