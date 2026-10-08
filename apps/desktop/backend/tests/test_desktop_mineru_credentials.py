"""MinerU credentials are independent of LLM connections and never persisted as values."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event, current_thread

import pytest
from fastapi.testclient import TestClient
from wenyi_core.llm.configuration import ProviderConfig
from wenyi_desktop.local_credentials import (
    CredentialConflict,
    CredentialStore,
    CredentialUnavailable,
)
from wenyi_desktop.main import create_app


class FakeVault:
    def __init__(self):
        self.values = {}

    def set_password(self, service, reference, value):
        self.values[(service, reference)] = value

    def get_password(self, service, reference):
        return self.values.get((service, reference))

    def delete_password(self, service, reference):
        self.values.pop((service, reference), None)


@pytest.fixture(autouse=True)
def no_mineru_environment(monkeypatch):
    monkeypatch.delenv("MINERU_API_KEY", raising=False)


def test_environment_priority_preserves_session_fallback(tmp_path, monkeypatch):
    store = CredentialStore(tmp_path, vault_factory=lambda: None)
    store.save_mineru(secret="fake-manual", storage="session")
    monkeypatch.setenv("MINERU_API_KEY", "fake-environment")
    snapshot = store.resolve_mineru_token()
    assert snapshot == "fake-environment"
    assert store.mineru_status() == {
        "mode": "environment",
        "storage": None,
        "available": True,
        "environment": "MINERU_API_KEY",
        "system_storage_available": False,
        "requires_key": True,
    }
    monkeypatch.delenv("MINERU_API_KEY")
    assert store.resolve_mineru_token() == "fake-manual"
    assert snapshot == "fake-environment"
    store.clear_session()
    assert store.resolve_mineru_token() is None
    assert not store.mineru_status()["available"]


@pytest.mark.parametrize("storage", ["system", "session"])
def test_restart_clear_and_independent_llm_namespace(tmp_path, storage):
    vault = FakeVault()
    store = CredentialStore(tmp_path, vault=vault)
    store.update("mineru", mode="manual", secret="fake-llm", storage="system")
    store.save_mineru(secret="fake-service", storage=storage)
    assert store.snapshot({"mineru": ProviderConfig(kind="openai")}) == {"mineru": "fake-llm"}
    assert store.resolve_mineru_token() == "fake-service"
    with sqlite3.connect(store.database) as conn:
        serialized = json.dumps(list(conn.iterdump()))
    assert "fake-service" not in serialized
    assert "fake-llm" not in serialized
    reopened = CredentialStore(tmp_path, vault=vault)
    assert reopened.resolve_mineru_token() == ("fake-service" if storage == "system" else None)
    store.reconcile(set(), {})
    assert store.resolve_mineru_token() == "fake-service"
    store.save_mineru(clear=True)
    assert store.resolve_mineru_token() is None
    assert not vault.values


@pytest.mark.parametrize("failure", ["deleted", "unreadable", "corrupt"])
def test_status_checks_vault_safely(tmp_path, failure):
    vault = FakeVault()
    store = CredentialStore(tmp_path, vault=vault)
    store.save_mineru(secret="fake-service")
    if failure == "deleted":
        vault.values.clear()
    elif failure == "corrupt":
        vault.values = dict.fromkeys(vault.values, "fake\nunsafe")
    else:

        def unreadable(*args):
            raise RuntimeError("private-vault-error")

        vault.get_password = unreadable
    assert not store.mineru_status()["available"]
    assert store.mineru_status()["storage"] == "system"
    assert "private" not in json.dumps(store.mineru_status())


def test_status_initializes_lazy_vault(tmp_path):
    calls = []
    vault = FakeVault()

    def factory():
        calls.append(True)
        return vault

    store = CredentialStore(tmp_path, vault_factory=factory)
    assert store.mineru_status()["system_storage_available"]
    assert store.mineru_status()["system_storage_available"]
    assert calls == [True]


def test_invalid_environment_status_fails_closed(tmp_path, monkeypatch):
    store = CredentialStore(tmp_path, vault=FakeVault())
    store.save_mineru(secret="fake-fallback", storage="session")
    monkeypatch.setenv("MINERU_API_KEY", "fake\nunsafe")
    status = store.mineru_status()
    assert status["mode"] == "environment"
    assert not status["available"]
    assert "fake" not in json.dumps(status)
    with pytest.raises(ValueError, match="header-safe"):
        store.resolve_mineru_token()


@pytest.mark.parametrize("storage", ["session", "system"])
@pytest.mark.parametrize("clear", [False, True])
@pytest.mark.parametrize("operation", ["resolve_mineru_token", "mineru_status"])
def test_read_retries_after_concurrent_source_cleanup(
    tmp_path, monkeypatch, storage, clear, operation
):
    store = CredentialStore(tmp_path, vault=FakeVault())
    store.save_mineru(secret="fake-old", storage=storage)
    original = store._mineru_source
    paused, resume = Event(), Event()
    reader = None

    def source(connection=None):
        result = original(connection)
        if current_thread() is reader and not paused.is_set():
            paused.set()
            assert resume.wait(5)
        return result

    monkeypatch.setattr(store, "_mineru_source", source)

    def read():
        nonlocal reader
        reader = current_thread()
        return getattr(store, operation)()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(read)
        try:
            assert paused.wait(5)
            if clear:
                store.save_mineru(clear=True)
            else:
                store.save_mineru(secret="fake-winner", storage=storage)
        finally:
            resume.set()
        result = future.result(timeout=5)
    if operation == "mineru_status":
        assert result["available"] is (not clear)
        assert result["storage"] == (None if clear else storage)
    else:
        assert result == (None if clear else "fake-winner")


@pytest.mark.parametrize("operation", ["resolve_mineru_token", "mineru_status"])
def test_persistent_read_churn_has_bounded_safe_conflict(tmp_path, monkeypatch, operation):
    store = CredentialStore(tmp_path, vault=FakeVault())
    calls = []

    def changing_source(connection=None):
        calls.append(True)
        return {"mode": "manual"}, len(calls)

    monkeypatch.setattr(store, "_mineru_source", changing_source)
    with pytest.raises(
        CredentialConflict, match="^MinerU credential changed; reload before reading$"
    ):
        getattr(store, operation)()
    assert len(calls) == 6


@pytest.mark.parametrize("secret", ["", "fake\n", "fake key", "fake\u200b", '{"token":"fake"}'])
def test_invalid_tokens_never_reach_storage(tmp_path, monkeypatch, secret):
    store = CredentialStore(tmp_path, vault_factory=lambda: pytest.fail("Vault accessed"))
    with pytest.raises(ValueError):
        store.save_mineru(secret=secret)
    assert store._mineru_source() == ({}, 0)
    assert not store._session
    if secret:
        # Environment values are trimmed; embedded unsafe characters remain invalid.
        monkeypatch.setenv("MINERU_API_KEY", f"prefix{secret}suffix")
        with pytest.raises(ValueError):
            store.resolve_mineru_token()


def test_source_revision_detects_concurrent_clear(tmp_path):
    vault = FakeVault()
    store = CredentialStore(tmp_path, vault=vault)
    original = vault.set_password

    def concurrent_clear(*args):
        original(*args)
        store.save_mineru(clear=True)

    vault.set_password = concurrent_clear
    with pytest.raises(CredentialConflict):
        store.save_mineru(secret="fake-loser", storage="system")
    assert store.resolve_mineru_token() is None
    assert not vault.values


def test_vault_io_has_no_catalog_writer(tmp_path):
    vault = FakeVault()
    store = CredentialStore(tmp_path, vault=vault)

    def assert_unlocked():
        with sqlite3.connect(store.database, timeout=0) as conn:
            conn.execute("BEGIN IMMEDIATE")

    for method in ("set_password", "get_password", "delete_password"):
        original = getattr(vault, method)

        def unlocked(*args, original=original):
            assert_unlocked()
            return original(*args)

        setattr(vault, method, unlocked)
    store.save_mineru(secret="fake-service", storage="system")
    assert store.resolve_mineru_token() == "fake-service"
    store.save_mineru(clear=True)


def test_api_auth_status_and_redacted_validation(desktop_context, caplog):
    from dataclasses import replace

    context = replace(desktop_context, api_token="fake-local-bearer")
    path = "/desktop/external-credentials/mineru"
    headers = {"Authorization": "Bearer fake-local-bearer", "Origin": "tauri://localhost"}
    with TestClient(create_app(context=context)) as client:
        assert client.get(path).status_code == 401
        assert (
            client.get(path, headers={**headers, "Origin": "https://evil.invalid"}).status_code
            == 403
        )
        assert client.get(path, headers=headers).json()["requires_key"]
        malformed = client.put(path, headers=headers, content='{"secret":"fake-invalid"')
        assert malformed.status_code == 422
        assert "fake-invalid" not in malformed.text
        for body in (
            {"secret": "fake-invalid\n"},
            {"secret": '{"access_token":"fake-json"}'},
            {"secret": "fake-invalid", "mode": "manual"},
            {"secret": "fake-invalid", "clear": True},
            {"secret": {"fake-invalid": True}},
            {"clear": "true"},
            {},
            {"clear": False},
        ):
            response = client.put(path, headers=headers, json=body)
            assert response.status_code == 422
            assert "fake-invalid" not in response.text
            assert "fake-json" not in response.text
        response = client.put(
            path, headers=headers, json={"secret": "fake-service", "storage": "session"}
        )
        assert response.status_code == 200
        assert response.json()["mode"] == "manual"
        assert set(response.json()) == {
            "mode",
            "storage",
            "available",
            "environment",
            "system_storage_available",
            "requires_key",
        }
        assert "fake-service" not in response.text
        assert context.settings_store.credentials.resolve_mineru_token() == "fake-service"
        settings = context.settings_store.load()
        assert "fake-service" not in settings.config.model_dump_json()
        assert "fake-service" not in settings.default_template
        with sqlite3.connect(context.settings_store.credentials.database) as conn:
            assert "fake-service" not in json.dumps(list(conn.iterdump()))
        assert client.put(path, headers=headers, json={"clear": True}).status_code == 200
        assert not client.get(path, headers=headers).json()["available"]
    assert "fake-service" not in caplog.text
    assert "fake-invalid" not in caplog.text


def test_auto_storage_fallback_and_clear_keeps_environment(tmp_path, monkeypatch):
    store = CredentialStore(tmp_path)
    store.save_mineru(secret="fake-session")
    assert store.mineru_status()["storage"] == "session"
    monkeypatch.setenv("MINERU_API_KEY", "fake-environment")
    store.save_mineru(clear=True)
    assert store.resolve_mineru_token() == "fake-environment"
    assert store.mineru_status()["mode"] == "environment"
    monkeypatch.delenv("MINERU_API_KEY")
    assert store.resolve_mineru_token() is None


def test_forced_system_failure_preserves_fallback(tmp_path):
    store = CredentialStore(tmp_path)
    store.save_mineru(secret="fake-old", storage="session")
    with pytest.raises(CredentialUnavailable):
        store.save_mineru(secret="fake-new", storage="system")
    assert store.resolve_mineru_token() == "fake-old"
    assert list(store._session.values()) == ["fake-old"]


def test_concurrent_save_retains_winner(tmp_path):
    vault = FakeVault()
    store = CredentialStore(tmp_path, vault=vault)
    store.save_mineru(secret="fake-old")
    original = vault.set_password

    def concurrent_save(*args):
        original(*args)
        vault.set_password = original
        store.save_mineru(secret="fake-winner")

    vault.set_password = concurrent_save
    with pytest.raises(CredentialConflict):
        store.save_mineru(secret="fake-loser")
    assert store.resolve_mineru_token() == "fake-winner"
    assert list(vault.values.values()) == ["fake-winner"]


def test_store_commit_failure_cleans_staged_reference(tmp_path, monkeypatch):
    vault = FakeVault()
    store = CredentialStore(tmp_path, vault=vault)
    store.save_mineru(secret="fake-old")
    original = store._mineru_source

    def fail_commit(connection=None):
        if connection is not None:
            raise OSError("fake-failure")
        return original()

    monkeypatch.setattr(store, "_mineru_source", fail_commit)
    with pytest.raises(OSError):
        store.save_mineru(secret="fake-staged")
    assert store.resolve_mineru_token() == "fake-old"
    assert list(vault.values.values()) == ["fake-old"]


def test_each_context_binds_its_own_resolver(tmp_path):
    from wenyi_desktop.main import create_context

    contexts = []
    try:
        for name in ("first", "second"):
            workspace = tmp_path / name
            workspace.mkdir()
            store = CredentialStore(workspace)
            store.save_mineru(secret=f"fake-{name}", storage="session")
            context = create_context(workspace, credentials=store)
            contexts.append(context)
            assert context.settings_store.credentials is store
            assert context.mineru_token_resolver.__self__ is store
            assert context.mineru_token_resolver() == f"fake-{name}"
        assert contexts[0].mineru_token_resolver() == "fake-first"
    finally:
        for context in contexts:
            context.settings_store.credentials.clear_session()
            context.repository.close()
