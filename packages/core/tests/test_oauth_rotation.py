"""Rotating subscription credentials survive adapters and process boundaries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from wenyi_core.llm.oauth.credentials import OAuthCredential
from wenyi_core.llm.profiles import get_profile
from wenyi_core.llm.providers import _credentials


@pytest.mark.parametrize(
    ("kind", "env_name"), [("codex", "WENYI_CODEX_OAUTH"), ("xai", "WENYI_XAI_OAUTH")]
)
def test_new_adapter_reuses_rotated_token(monkeypatch, tmp_path, kind, env_name):
    monkeypatch.setenv("WENYI_OAUTH_CACHE_DIR", str(tmp_path / "oauth"))
    original = OAuthCredential(access_token="old", refresh_token="refresh-old", expires_at=1)
    monkeypatch.setenv(env_name, json.dumps(original.to_payload()))
    calls = []

    def refresh(profile, credential, env_name):
        calls.append(credential.refresh_token)
        assert credential.refresh_token != "refresh-new"
        return replace(credential, access_token="new", refresh_token="refresh-new", expires_at=4e9)

    monkeypatch.setattr(_credentials, "_refresh", refresh)
    profile = get_profile(kind)
    first = _credentials.resolve_credential(profile)
    # Model a second process that still inherited the original shell export.
    monkeypatch.setenv(env_name, json.dumps(original.to_payload()))
    second = _credentials.resolve_credential(profile, cached=original)
    assert first == second
    assert calls == ["refresh-old"]


def test_rotated_credential_survives_a_new_process(monkeypatch, tmp_path):
    monkeypatch.setenv("WENYI_OAUTH_CACHE_DIR", str(tmp_path / "oauth"))
    original = OAuthCredential(access_token="old", refresh_token="refresh-old", expires_at=1)
    raw = json.dumps(original.to_payload())
    monkeypatch.setenv("WENYI_CODEX_OAUTH", raw)
    monkeypatch.setattr(
        _credentials,
        "_refresh",
        lambda *args: replace(
            original, access_token="new", refresh_token="rotated", expires_at=4e9
        ),
    )
    _credentials.resolve_credential(get_profile("codex"))
    child_env = {**os.environ, "WENYI_CODEX_OAUTH": raw}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from wenyi_core.llm.providers._credentials import resolve_credential; "
            "from wenyi_core.llm.providers import _credentials\n"
            "def fail_refresh(*args): raise AssertionError('unexpected refresh')\n"
            "_credentials._refresh = fail_refresh\n"
            "from wenyi_core.llm.profiles import get_profile; "
            "assert resolve_credential(get_profile('codex')).refresh_token == 'rotated'",
        ],
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_concurrent_adapters_refresh_only_once(monkeypatch, tmp_path):
    monkeypatch.setenv("WENYI_OAUTH_CACHE_DIR", str(tmp_path / "oauth"))
    original = OAuthCredential(access_token="old", refresh_token="refresh-old", expires_at=1)
    monkeypatch.setenv("WENYI_CODEX_OAUTH", json.dumps(original.to_payload()))
    calls = []

    def refresh(*args):
        calls.append(1)
        return replace(original, access_token="new", refresh_token="rotated", expires_at=4e9)

    monkeypatch.setattr(_credentials, "_refresh", refresh)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda _: _credentials.resolve_credential(get_profile("codex")), range(4))
        )
    assert calls == [1]
    assert all(value.access_token == "new" for value in results)


def test_new_wire_adapter_recovers_the_rotated_credential(monkeypatch):
    from wenyi_core.llm.configuration import ProviderConfig
    from wenyi_core.llm.providers.codex import CodexClient

    original = OAuthCredential(access_token="old", refresh_token="refresh-old", expires_at=1)
    raw = json.dumps(original.to_payload())
    monkeypatch.setenv("WENYI_CODEX_OAUTH", raw)
    calls = []

    def refresh(*args):
        calls.append(1)
        return replace(original, access_token="new", refresh_token="rotated", expires_at=4e9)

    monkeypatch.setattr(_credentials, "_refresh", refresh)
    first = CodexClient(ProviderConfig(kind="openai-codex"))
    assert first.auth_token() == "new"
    monkeypatch.setenv("WENYI_CODEX_OAUTH", raw)
    second = CodexClient(ProviderConfig(kind="openai-codex"))
    assert second.auth_token() == "new"
    assert calls == [1]


def test_connections_with_same_subscription_share_rotation(monkeypatch):
    original = OAuthCredential(access_token="old", refresh_token="refresh-old", expires_at=1)
    raw = json.dumps(original.to_payload())
    monkeypatch.setenv("WENYI_CODEX_OAUTH", raw)
    calls = []

    def refresh(*args):
        calls.append(1)
        return replace(original, access_token="new", refresh_token="rotated", expires_at=4e9)

    monkeypatch.setattr(_credentials, "_refresh", refresh)
    _credentials.resolve_credential(get_profile("codex"))
    monkeypatch.setenv("WENYI_CODEX_OAUTH", raw)
    assert (
        _credentials.resolve_credential(
            get_profile("openai"), api_key_env="WENYI_CODEX_OAUTH"
        ).access_token
        == "new"
    )
    assert calls == [1]


def test_explicit_new_login_replaces_cached_account(monkeypatch, tmp_path):
    monkeypatch.setenv("WENYI_OAUTH_CACHE_DIR", str(tmp_path / "oauth"))
    old = OAuthCredential(access_token="old", refresh_token="refresh-old", expires_at=1)
    monkeypatch.setenv("WENYI_CODEX_OAUTH", json.dumps(old.to_payload()))
    monkeypatch.setattr(
        _credentials, "_refresh", lambda *args: replace(old, access_token="rotated", expires_at=4e9)
    )
    cached = _credentials.resolve_credential(get_profile("codex"))
    new = OAuthCredential(
        access_token="other-account", refresh_token="other-refresh", expires_at=4e9
    )
    monkeypatch.setenv("WENYI_CODEX_OAUTH", json.dumps(new.to_payload()))
    assert _credentials.resolve_credential(get_profile("codex"), cached=cached) == new


def test_original_export_recovers_multiple_rotations(monkeypatch, tmp_path):
    monkeypatch.setenv("WENYI_OAUTH_CACHE_DIR", str(tmp_path / "oauth"))
    original = OAuthCredential(access_token="old", refresh_token="first", expires_at=1)
    raw = json.dumps(original.to_payload())
    monkeypatch.setenv("WENYI_CODEX_OAUTH", raw)
    calls = []

    def refresh(profile, credential, env_name):
        calls.append(credential.refresh_token)
        return replace(
            credential, access_token="new", refresh_token=f"next-{len(calls)}", expires_at=4e9
        )

    monkeypatch.setattr(_credentials, "_refresh", refresh)
    _credentials.resolve_credential(get_profile("codex"))
    # Advance expiry without mutating the process's old export.
    monkeypatch.setattr(OAuthCredential, "expired", lambda self: self.refresh_token != "next-2")
    monkeypatch.setenv("WENYI_CODEX_OAUTH", raw)
    assert _credentials.resolve_credential(get_profile("codex")).refresh_token == "next-2"
    monkeypatch.setenv("WENYI_CODEX_OAUTH", raw)
    assert _credentials.resolve_credential(get_profile("codex")).refresh_token == "next-2"
    assert calls == ["first", "next-1"]


def test_cache_failure_prevents_consuming_refresh_token(monkeypatch, tmp_path):
    cache = tmp_path / "not-a-directory"
    cache.write_text("blocked", encoding="utf-8")
    monkeypatch.setenv("WENYI_OAUTH_CACHE_DIR", str(cache))
    credential = OAuthCredential(access_token="old", refresh_token="refresh-old", expires_at=1)
    monkeypatch.setenv("WENYI_CODEX_OAUTH", json.dumps(credential.to_payload()))
    monkeypatch.setattr(_credentials, "_refresh", lambda *args: pytest.fail("must not refresh"))
    with pytest.raises(_credentials.OAuthCredentialError, match="credential cache"):
        _credentials.resolve_credential(get_profile("codex"))
