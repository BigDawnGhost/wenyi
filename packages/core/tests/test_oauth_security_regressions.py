"""Offline regressions for safe OAuth exports and device authorization polling."""

from __future__ import annotations

import json
import subprocess

import pytest
from wenyi_core.envfile import export_lines
from wenyi_core.llm.oauth import flows
from wenyi_core.llm.oauth.credentials import OAuthCredential, format_env_export


@pytest.mark.parametrize("identity", ["x'$(printf injected)'y", "O'Brien", "$(printf injected)"])
def test_export_preserves_shell_metacharacters(identity):
    credential = OAuthCredential(access_token="fake-token", email=identity)
    command = format_env_export("TEST_OAUTH", credential)
    result = subprocess.run(
        ["/bin/sh", "-c", command + "\nprintf '%s' \"$TEST_OAUTH\""],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(result.stdout) == credential.to_payload()


@pytest.mark.parametrize("name", ["BAD;printf injected", "BAD NAME", "1BAD", "BAD\nNAME"])
def test_exports_reject_invalid_variable_names(name):
    with pytest.raises(ValueError, match="environment variable name"):
        format_env_export(name, OAuthCredential(access_token="fake"))
    with pytest.raises(ValueError, match="environment variable name"):
        export_lines({name: "fake"})


@pytest.mark.parametrize("http_status", [200, 400])
@pytest.mark.parametrize(
    "error,expected",
    [
        ("authorization_pending", "pending"),
        ("slow_down", "slow-down"),
        ("expired_token", "failed"),
        ("access_denied", "failed"),
    ],
)
def test_device_poll_checks_oauth_error_before_http_status(
    monkeypatch, http_status, error, expected
):
    flow = flows.DeviceCodeFlow(
        provider="copilot",
        display_name="Copilot",
        client_id="fake-client",
        device_code_url="https://example.invalid/device",
        token_url="https://example.invalid/token",
    )
    monkeypatch.setattr(flows, "_post", lambda *a, **kw: (http_status, {"error": error}))
    state, credential, message = flows._poll_once(flow, {"device_code": "fake-code"})
    assert state == expected
    assert credential is None


def test_device_login_waits_and_slows_down_until_approved(monkeypatch):
    flow = flows.DeviceCodeFlow(
        provider="copilot",
        display_name="Copilot",
        client_id="fake-client",
        device_code_url="https://example.invalid/device",
        token_url="https://example.invalid/token",
    )
    responses = iter(
        [
            (
                200,
                {
                    "device_code": "fake-code",
                    "user_code": "ABCD",
                    "verification_uri": "https://example.invalid/verify",
                    "interval": 1,
                },
            ),
            (200, {"error": "authorization_pending"}),
            (200, {"error": "slow_down"}),
            (200, {"access_token": "fake-approved"}),
        ]
    )
    monkeypatch.setattr(flows, "_post", lambda *a, **kw: next(responses))
    waits = []
    result = flows.device_code_login(
        flow,
        on_prompt=lambda prompt: None,
        sleep=waits.append,
        open_browser=False,
    )
    assert result.access_token == "fake-approved"
    assert waits[-1] > waits[0]
