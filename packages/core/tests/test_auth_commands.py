"""Offline contracts for subscription sign-in, credential import and credential checks.

No command here reaches a provider: sign-in is stubbed at the ``login`` boundary, import
only reads temporary files, and check only inspects the environment.
"""

from __future__ import annotations

import base64
import json
import re

import pytest
from rich.console import Console
from typer.testing import CliRunner
from wenyi_cli.cli import create_app
from wenyi_core.llm.oauth.credentials import OAuthCredential
from wenyi_core.llm.subscriptions import find_subscription, get_subscription

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\].*?\x07|\r")


def plain_text(value: str) -> str:
    """Strip ANSI/control sequences so CLI assertions stay stable under Rich."""
    return _ANSI_RE.sub("", value)


@pytest.fixture(autouse=True)
def isolated_auth_environment(tmp_path, monkeypatch):
    """Never load user credentials or change the desktop clipboard in offline tests."""
    from wenyi_core.llm.subscriptions import SUBSCRIPTIONS

    monkeypatch.chdir(tmp_path)
    for subscription in SUBSCRIPTIONS.values():
        monkeypatch.delenv(subscription.env_var, raising=False)
    monkeypatch.delenv("WENYI_ANTIGRAVITY_CLIENT_ID", raising=False)
    monkeypatch.delenv("WENYI_ANTIGRAVITY_CLIENT_SECRET", raising=False)
    monkeypatch.setattr("wenyi_cli.commands.oauth_ui.copy_device_code", lambda code: False)


def _invoke(*arguments: str):
    """Invoke the auth command group on a wide console so Rich never wraps a cell."""
    return CliRunner().invoke(create_app(console=Console(width=200)), ["auth", *arguments])


def _access_token(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{body}"


def test_subscription_lookup_accepts_login_kind_and_alias():
    codex = get_subscription("codex")
    assert get_subscription("openai-codex") is codex
    assert get_subscription("chatgpt") is codex
    assert get_subscription("copilot").kind == "copilot"
    assert find_subscription("nope") is None
    with pytest.raises(RuntimeError, match="no subscription sign-in"):
        get_subscription("nope")


def test_list_shows_every_sign_in_and_its_credential_variable(monkeypatch):
    monkeypatch.setenv("WENYI_XAI_OAUTH", "token")
    result = _invoke("list", "--json")
    assert result.exit_code == 0, result.output
    rows = {row["login"]: row for row in json.loads(result.output)}
    assert set(rows) == {"antigravity", "codex", "copilot", "minimax", "nous", "qwen", "xai"}
    assert rows["codex"]["env_var"] == "WENYI_CODEX_OAUTH"
    assert rows["codex"]["provider"] == "openai-codex"
    assert rows["codex"]["configured"] is False
    assert rows["xai"]["configured"] is True


def test_list_renders_a_human_readable_table(monkeypatch):
    monkeypatch.delenv("WENYI_CODEX_OAUTH", raising=False)
    result = _invoke("list")
    assert result.exit_code == 0, result.output
    output = plain_text(result.output)
    assert "codex" in output
    assert "ChatGPT (Codex)" in output
    assert "(unset)" in output


def test_login_prints_the_export_line(monkeypatch):
    def fake_login(kind, **kwargs):
        assert kind == "openai-codex"
        kwargs["on_prompt"](_prompt())
        return OAuthCredential(access_token="access", refresh_token="refresh", email="a@b.c")

    monkeypatch.setattr("wenyi_cli.commands.auth.login", fake_login)
    result = _invoke("login", "codex")
    assert result.exit_code == 0, result.output
    output = plain_text(result.output)
    assert "visit https://example.test/device" in output
    assert "enter the code WXYZ-1234" in output
    assert "export WENYI_CODEX_OAUTH=" in output
    assert '"refresh_token":"refresh"' in output


def test_login_reports_a_failed_grant(monkeypatch):
    from wenyi_core.llm.oauth.credentials import OAuthCredentialError

    def failing_login(kind, **kwargs):
        raise OAuthCredentialError("device authorization expired before it was approved")

    monkeypatch.setattr("wenyi_cli.commands.auth.login", failing_login)
    result = _invoke("login", "xai")
    assert result.exit_code == 1
    assert "authorization expired" in plain_text(result.output)
    assert "Traceback" not in result.output


def test_login_rejects_an_unknown_sign_in():
    result = _invoke("login", "nope")
    assert result.exit_code == 2
    output = plain_text(result.output)
    assert "Unknown sign-in 'nope'" in output
    assert "codex" in output


def test_import_reads_a_credential_file_in_json(tmp_path):
    source = tmp_path / "creds.json"
    source.write_text(json.dumps({"access_token": "from-file", "refresh_token": "r"}))
    result = _invoke("import", "qwen", str(source), "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["env_var"] == "WENYI_QWEN_OAUTH"
    assert payload["credential"]["access_token"] == "from-file"


def test_import_falls_back_to_the_clients_own_file(tmp_path, monkeypatch):
    from dataclasses import replace

    from wenyi_core.llm import subscriptions

    source = tmp_path / "oauth_creds.json"
    source.write_text(json.dumps({"access_token": "qwen-token"}))
    qwen = subscriptions.SUBSCRIPTIONS["qwen"]
    monkeypatch.setitem(
        subscriptions.SUBSCRIPTIONS, "qwen", replace(qwen, credential_files=lambda: (source,))
    )
    result = _invoke("import", "qwen")
    assert result.exit_code == 0, result.output
    assert "export WENYI_QWEN_OAUTH=" in plain_text(result.output)


def test_import_rejects_a_source_without_a_token(tmp_path):
    source = tmp_path / "empty.json"
    source.write_text(json.dumps({"nothing": True}))
    result = _invoke("import", "qwen", str(source))
    assert result.exit_code == 1
    assert "No credential found" in plain_text(result.output)


def test_import_warns_about_unusable_files_and_uses_the_rest(tmp_path):
    missing = tmp_path / "missing.json"
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"access_token": "kept"}))
    result = _invoke("import", "qwen", str(missing), str(good))
    assert result.exit_code == 0, result.output
    output = plain_text(result.output)
    assert "Skipped missing.json" in output
    assert '"access_token":"kept"' in output


def test_check_reports_a_missing_variable_without_failing_the_listing(monkeypatch):
    monkeypatch.delenv("WENYI_CODEX_OAUTH", raising=False)
    monkeypatch.delenv("WENYI_XAI_OAUTH", raising=False)
    result = _invoke("check", "--json")
    assert result.exit_code == 0, result.output
    reports = json.loads(result.output)
    assert {report["login"] for report in reports} == {
        "antigravity",
        "codex",
        "copilot",
        "minimax",
        "nous",
        "qwen",
        "xai",
    }
    codex = next(report for report in reports if report["login"] == "codex")
    assert codex["status"] == "missing"
    assert codex["configured"] is False


def test_check_reads_identity_and_expiry_from_the_environment(monkeypatch):
    token = _access_token({"email": "reader@example.com", "exp": 4_000_000_000})
    monkeypatch.setenv(
        "WENYI_XAI_OAUTH",
        json.dumps({"access_token": token, "refresh_token": "refresh"}),
    )
    result = _invoke("check", "xai", "--json")
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)[0]
    assert report["status"] == "ready"
    assert report["expired"] is False
    assert report["refreshable"] is True
    assert "reader@example.com" in report["identity"]


def test_check_fails_for_an_explicit_unusable_provider(monkeypatch):
    token = _access_token({"exp": 1_000_000_000})
    monkeypatch.setenv("WENYI_XAI_OAUTH", json.dumps({"access_token": token}))
    result = _invoke("check", "xai", "--json")
    assert result.exit_code == 1
    assert json.loads(result.output)[0]["detail"] == (
        "Expired with no refresh token; sign in again"
    )


def test_check_rejects_a_credential_without_an_access_token(monkeypatch):
    monkeypatch.setenv("WENYI_XAI_OAUTH", json.dumps({"refresh_token": "only"}))
    result = _invoke("check", "xai", "--json")
    assert result.exit_code == 1
    assert json.loads(result.output)[0]["status"] == "invalid"


def test_antigravity_login_requires_a_google_oauth_client():
    result = _invoke("login", "antigravity", "--no-browser")
    assert result.exit_code == 1
    output = plain_text(result.output)
    assert "WENYI_ANTIGRAVITY_CLIENT_ID" in output
    assert "WENYI_ANTIGRAVITY_CLIENT_SECRET" in output


def test_antigravity_login_uses_the_configured_google_oauth_client(monkeypatch):
    monkeypatch.setenv("WENYI_ANTIGRAVITY_CLIENT_ID", "offline-client-id")
    monkeypatch.setenv("WENYI_ANTIGRAVITY_CLIENT_SECRET", "offline-client-secret")
    flow = get_subscription("antigravity").flow()
    assert flow.client_id == "offline-client-id"
    assert flow.client_secret == "offline-client-secret"
    assert flow.redirect_port == 45297


def _prompt():
    from wenyi_core.llm.oauth.flows import DeviceCodePrompt

    return DeviceCodePrompt(
        provider="codex",
        display_name="ChatGPT (Codex)",
        user_code="WXYZ-1234",
        verification_url="https://example.test/device",
        expires_in=900.0,
        interval=5.0,
    )
