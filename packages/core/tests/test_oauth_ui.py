"""Clipboard conveniences stay offline and copy only the short-lived device code."""

from io import StringIO

from rich.console import Console
from wenyi_cli.commands import oauth_ui
from wenyi_core.llm.oauth.flows import DeviceCodePrompt


def test_device_prompt_copies_code(monkeypatch):
    copied = []
    monkeypatch.setattr(oauth_ui, "copy_device_code", lambda code: copied.append(code) or True)
    stream = StringIO()
    oauth_ui.print_device_prompt(
        Console(file=stream),
        DeviceCodePrompt(
            provider="test",
            display_name="Test",
            user_code="ABCD",
            verification_url="https://login.example",
            expires_in=300,
            interval=5,
        ),
    )
    assert copied == ["ABCD"]
    assert "copied to clipboard" in stream.getvalue()


def test_clipboard_failure_keeps_manual_instructions(monkeypatch):
    monkeypatch.setattr(oauth_ui, "copy_device_code", lambda code: False)
    stream = StringIO()
    oauth_ui.print_device_prompt(
        Console(file=stream),
        DeviceCodePrompt(
            provider="test",
            display_name="Test",
            user_code="ABCD",
            verification_url="https://login.example",
            expires_in=300,
            interval=5,
        ),
    )
    assert "ABCD" in stream.getvalue()
    assert "Copy the code above" in stream.getvalue()


def test_macos_clipboard_passes_code_via_stdin(monkeypatch):
    calls = []
    monkeypatch.setattr(oauth_ui.sys, "platform", "darwin")
    monkeypatch.setattr(oauth_ui.shutil, "which", lambda name: "/usr/bin/pbcopy")
    monkeypatch.setattr(oauth_ui.subprocess, "run", lambda *a, **kw: calls.append((a, kw)))
    assert oauth_ui.copy_device_code("ABCD")
    assert calls[0][0] == (("pbcopy",),)
    assert calls[0][1]["input"] == "ABCD"
    assert calls[0][1]["timeout"] == 2
