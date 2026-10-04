"""Cross-process locking and fail-closed recovery of rotating OAuth credentials."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace

import pytest
from wenyi_core.llm.oauth.credentials import OAuthCredential, OAuthCredentialError
from wenyi_core.llm.oauth.store import coordinated_credential


def test_processes_share_one_refresh(tmp_path):
    cache = tmp_path / "oauth"
    calls = tmp_path / "calls.txt"
    script = """
import os
from pathlib import Path
from dataclasses import replace
from wenyi_core.llm.oauth.credentials import OAuthCredential
from wenyi_core.llm.oauth.store import coordinated_credential
original = OAuthCredential(access_token='old', refresh_token='old-refresh', expires_at=1)
def refresh(credential):
    assert credential.refresh_token == 'old-refresh'
    with Path(os.environ['CALLS_PATH']).open('a') as f:
        f.write('refresh\\n')
    return replace(credential, access_token='new', refresh_token='new-refresh', expires_at=4e9)
assert coordinated_credential('codex', original, refresh).access_token == 'new'
"""
    env = {**os.environ, "WENYI_OAUTH_CACHE_DIR": str(cache), "CALLS_PATH": str(calls)}
    children = [
        subprocess.Popen(
            [sys.executable, "-c", script],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(4)
    ]
    for child in children:
        _, errors = child.communicate(timeout=30)
        assert child.returncode == 0, errors
    assert calls.read_text() == "refresh\n"
    if os.name != "nt":
        assert cache.stat().st_mode & 0o777 == 0o700
        assert (cache / "credentials.json").stat().st_mode & 0o777 == 0o600
        assert (cache / "credentials.lock").stat().st_mode & 0o777 == 0o600


def test_interrupted_refresh_does_not_reuse_old_token(monkeypatch, tmp_path):
    monkeypatch.setenv("WENYI_OAUTH_CACHE_DIR", str(tmp_path))
    original = OAuthCredential(access_token="old", refresh_token="old-refresh", expires_at=1)

    def fail_after_rotation(credential):
        raise RuntimeError("simulated loss of response")

    with pytest.raises(RuntimeError, match="simulated"):
        coordinated_credential("codex", original, fail_after_rotation)
    with pytest.raises(OAuthCredentialError, match="sign in again"):
        coordinated_credential("codex", original, lambda _: pytest.fail("consumed token reused"))
    new = replace(original, access_token="new-login", refresh_token="new-login", expires_at=4e9)
    assert coordinated_credential("codex", new, lambda _: pytest.fail("unnecessary refresh")) == new


def test_failed_replacement_write_keeps_pending_marker(monkeypatch, tmp_path):
    from wenyi_core.llm.oauth import store

    monkeypatch.setenv("WENYI_OAUTH_CACHE_DIR", str(tmp_path))
    original = OAuthCredential(access_token="old", refresh_token="old-refresh", expires_at=1)
    writes = []
    write = store._write

    def fail_replacement(path, payload):
        writes.append(1)
        if len(writes) == 2:
            raise OSError("simulated storage failure after rotation")
        write(path, payload)

    monkeypatch.setattr(store, "_write", fail_replacement)
    with pytest.raises(OAuthCredentialError, match="sign in again"):
        coordinated_credential(
            "codex",
            original,
            lambda current: replace(
                current, access_token="new", refresh_token="new-refresh", expires_at=4e9
            ),
        )
    monkeypatch.setattr(store, "_write", write)
    with pytest.raises(OAuthCredentialError, match="sign in again"):
        coordinated_credential("codex", original, lambda _: pytest.fail("must not reuse"))


def test_corrupt_cache_does_not_refresh_or_leak_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("WENYI_OAUTH_CACHE_DIR", str(tmp_path))
    (tmp_path / "credentials.json").write_text(json.dumps({"aliases": []}))
    original = OAuthCredential(access_token="secret", refresh_token="refresh-secret", expires_at=1)
    with pytest.raises(OAuthCredentialError, match="credential cache") as caught:
        coordinated_credential("codex", original, lambda _: pytest.fail("must not refresh"))
    assert "refresh-secret" not in str(caught.value)
