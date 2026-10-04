"""Credential regression tests use fake keys, temporary catalogs and a fake vault only."""

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from wenyi_backend.context import use_context
from wenyi_core.config import Config
from wenyi_core.llm.configuration import ProviderConfig
from wenyi_core.llm.retrying import ProviderRequestError
from wenyi_desktop import local_credentials
from wenyi_desktop.local_credentials import (
    CredentialStore,
    CredentialUnavailable,
    build_local_client,
)
from wenyi_desktop.main import create_app, create_context
from wenyi_desktop.routers import credentials

system_keyring = local_credentials.system_keyring


@pytest.mark.parametrize("mode", ["manual", "environment"])
def test_local_client_sanitizes_real_provider_failure(tmp_path, monkeypatch, caplog, mode):
    secret = f"fake-{mode}-provider-error-key"
    config = Config.from_dict(
        {
            "llm": {
                "providers": {
                    "one": {
                        "kind": "openai",
                        "api_key_env": "WENYI_TEST_FAKE_KEY",
                        "max_retries": 1,
                    }
                },
                "models": {"m": {"provider": "one", "model": "offline"}},
                "tiers": {"strong": "m", "cheap": "m", "fast": "m"},
            }
        }
    )
    store = CredentialStore(tmp_path)
    if mode == "manual":
        store.update("one", mode="manual", secret=secret, storage="session")
    else:
        monkeypatch.setenv("WENYI_TEST_FAKE_KEY", secret)
    client = build_local_client(config, store)
    client.limits.wait_for_retry = lambda delay: None
    events, calls = [], []
    client.set_event_sink(lambda event, **data: events.append((event, data)))

    def create(**kwargs):
        calls.append(kwargs)
        raise httpx.ConnectError(f"Credential {secret} was rejected")

    client.adapter("one")._client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    with pytest.raises(ProviderRequestError) as caught:
        try:
            client.complete([], operation="translation.body")
        except Exception:
            logging.getLogger(__name__).exception("Desktop job failed")
            raise
    assert len(calls) == 2
    assert secret not in str(caught.value)
    assert secret not in json.dumps(events)
    assert secret not in caplog.text
    assert caught.value.__context__ is None


@pytest.mark.parametrize("control", ["\r", "\n", "\t", "\x00", "\x7f", "\u200b"])
def test_manual_control_characters_are_rejected_before_storage(tmp_path, control):
    store = CredentialStore(tmp_path)
    with pytest.raises(ValueError, match="control characters") as caught:
        store.update("one", mode="manual", secret=f"fake-{control}-key", storage="session")
    assert "fake-" not in str(caught.value)
    assert not store._records()
    assert not store._session


def test_environment_control_characters_are_rejected_before_sdk(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key\ninvalid")
    config = Config.from_dict(
        {
            "llm": {
                "providers": {"one": {"kind": "openai"}},
                "models": {"m": {"provider": "one", "model": "offline"}},
                "tiers": {"strong": "m", "cheap": "m", "fast": "m"},
            }
        }
    )
    with pytest.raises(ValueError, match="control characters"):
        build_local_client(config, CredentialStore(tmp_path))


class FakeVault:
    def __init__(self):
        self.values = {}
        self.fail = False

    def set_password(self, service, reference, secret):
        if self.fail:
            raise RuntimeError("fake-sensitive-error-do-not-echo")
        self.values[service, reference] = secret

    def get_password(self, service, reference):
        return self.values.get((service, reference))

    def delete_password(self, service, reference):
        self.values.pop((service, reference), None)


@pytest.fixture(autouse=True)
def isolated_vault(monkeypatch):
    vault = FakeVault()
    monkeypatch.setattr(local_credentials, "system_keyring", lambda: vault)
    yield vault


@pytest.fixture
def desktop_context(tmp_path, isolated_vault):
    context = create_context(
        tmp_path,
        credentials=CredentialStore(tmp_path, vault=isolated_vault),
        api_token="fake-local-token",
    )
    try:
        with use_context(context):
            yield context
    finally:
        context.settings_store.credentials.clear_session()
        context.repository.close()


def provider():
    return ProviderConfig(kind="openai", api_key_env="WENYI_TEST_FAKE_KEY")


@pytest.mark.parametrize("failure", ["absent", "partial"])
def test_default_storage_falls_back_without_persisting_secret(tmp_path, failure):
    vault = FakeVault()
    if failure == "partial":

        def fail(service, reference, secret):
            vault.values[service, reference] = secret
            raise RuntimeError(secret)

        vault.set_password = fail
    store = CredentialStore(tmp_path, vault=vault if failure == "partial" else None)
    store.update("one", mode="manual", secret="fake-auto-secret")
    assert store.status("one", provider())["storage"] == "session"
    assert store.snapshot({"one": provider()})["one"] == "fake-auto-secret"
    assert not vault.values
    assert not CredentialStore(tmp_path).status("one", provider())["available"]
    assert all(b"fake-auto-secret" not in path.read_bytes() for path in tmp_path.iterdir())


def test_modes_clear_restart_and_no_plaintext(tmp_path, monkeypatch, isolated_vault):
    monkeypatch.setenv("WENYI_TEST_FAKE_KEY", "fake-env-key")
    store = CredentialStore(tmp_path, vault=isolated_vault)
    cfg = provider()
    assert store.snapshot({"one": cfg}) == {"one": "fake-env-key"}
    store.update("one", mode="manual", secret="fake-manual-key")
    assert store.snapshot({"one": cfg, "two": cfg}) == {
        "one": "fake-manual-key",
        "two": "fake-env-key",
    }
    assert "fake-" not in json.dumps(store.status("one", cfg))
    assert all(b"fake-manual-key" not in path.read_bytes() for path in tmp_path.iterdir())
    reopened = CredentialStore(tmp_path, vault=isolated_vault)
    assert reopened.snapshot({"one": cfg})["one"] == "fake-manual-key"
    store.update("one", mode="environment")
    assert store.snapshot({"one": cfg})["one"] == "fake-env-key"
    store.update("one", mode="manual", clear=True)
    assert store.snapshot({"one": cfg})["one"] is None
    assert not isolated_vault.values
    assert store.status("one", cfg)["mode"] == "manual"


def test_empty_input_preserves_key_and_save_does_not_read_vault(desktop_context, isolated_vault):
    store = desktop_context.settings_store.credentials
    name = next(iter(credentials.load_settings().config.llm.providers))
    reads = []

    def unexpected_read(*args):
        reads.append(args)
        raise AssertionError("Saving must not read the vault")

    isolated_vault.get_password = unexpected_read
    result = credentials.save_credential(
        name, credentials.CredentialInput(mode="manual", secret="fake-save")
    )
    assert result["available"] and result["storage"] == "system"
    assert not reads
    previous = store._records()
    with pytest.raises(ValueError, match="blank"):
        store.update(name, mode="manual", secret="")
    assert store._records() == previous
    assert list(isolated_vault.values.values()) == ["fake-save"]


def test_fallback_commit_failure_releases_session_and_keeps_old_key(
    desktop_context, isolated_vault, monkeypatch
):
    from fastapi import HTTPException

    store = desktop_context.settings_store.credentials
    name = next(iter(credentials.load_settings().config.llm.providers))
    store.update(name, mode="manual", secret="fake-old")
    previous = store._records()
    isolated_vault.fail = True

    def fail(*args):
        raise OSError("fake-failure")

    monkeypatch.setattr(store, "_commit", fail)
    with pytest.raises(HTTPException) as caught:
        credentials.save_credential(
            name, credentials.CredentialInput(mode="manual", secret="fake-session-staged")
        )
    assert caught.value.status_code == 503
    assert not store._session
    assert store._records() == previous
    assert list(isolated_vault.values.values()) == ["fake-old"]


def test_no_vault_requires_explicit_session_and_session_expires(tmp_path):
    store = CredentialStore(tmp_path)
    with pytest.raises(CredentialUnavailable, match="session-only"):
        store.update("one", mode="manual", secret="fake-secret", storage="system")
    assert not store._records()
    store.update("one", mode="manual", secret="fake-session-secret", storage="session")
    assert store.snapshot({"one": provider()})["one"] == "fake-session-secret"
    assert not CredentialStore(tmp_path).status("one", provider())["available"]
    assert all(b"fake-session-secret" not in path.read_bytes() for path in tmp_path.iterdir())


def test_failed_writes_keep_old_selection(tmp_path, monkeypatch, isolated_vault):
    store = CredentialStore(tmp_path, vault=isolated_vault)
    store.update("one", mode="manual", secret="fake-old")
    isolated_vault.fail = True
    with pytest.raises(CredentialUnavailable) as caught:
        store.update("one", mode="manual", secret="fake-new", storage="system")
    assert "sensitive" not in str(caught.value)
    assert store.snapshot({"one": provider()})["one"] == "fake-old"
    isolated_vault.fail = False

    def fail(*args):
        raise OSError("fake-disk-failure")

    monkeypatch.setattr(store, "_commit", fail)
    with pytest.raises(CredentialUnavailable, match="preferences"):
        store.update("one", mode="manual", secret="fake-new")
    assert list(isolated_vault.values.values()) == ["fake-old"]


def test_rename_delete_and_transaction_rollback(tmp_path, isolated_vault):
    store = CredentialStore(tmp_path, vault=isolated_vault)
    store.update("one", mode="manual", secret="fake-rename")
    with pytest.raises(RuntimeError), store._connection() as conn:
        store.reconcile({"two"}, {"one": "two"}, conn)
        raise RuntimeError("rollback")
    assert store.snapshot({"one": provider()})["one"] == "fake-rename"
    store.reconcile({"two"}, {"one": "two"})
    assert store.snapshot({"two": provider()})["two"] == "fake-rename"
    store.reconcile(set(), {})
    store.update("two", mode="manual")
    assert store.snapshot({"two": provider()})["two"] is None
    assert not isolated_vault.values


@pytest.mark.parametrize("kind", ["openai", "gemini", "opencode-go"])
def test_clients_snapshot_credentials_without_mutating_environment(
    tmp_path, monkeypatch, isolated_vault, kind
):
    monkeypatch.setenv("WENYI_TEST_FAKE_KEY", "fake-env")
    config = Config.from_dict(
        {
            "llm": {
                "providers": {"one": {"kind": kind, "api_key_env": "WENYI_TEST_FAKE_KEY"}},
                "models": {"model": {"provider": "one", "model": "fake-model"}},
                "tiers": {"strong": "model", "cheap": "model", "fast": "model"},
            }
        }
    )
    store = CredentialStore(tmp_path, vault=isolated_vault)
    first = build_local_client(config, store)
    store.update("one", mode="manual", secret="fake-new")
    second = build_local_client(config, store)
    store.update("one", mode="manual", clear=True)
    missing = build_local_client(config, store)
    with pytest.raises(RuntimeError, match="credential"):
        missing.validate_credentials()
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(lambda client: client.adapter("one").api_key(), [first, second])) == [
            "fake-env",
            "fake-new",
        ]
    first.validate_credentials()
    second.validate_credentials()
    assert "fake-new" not in config.model_dump_json()
    assert "fake-env" not in first.config.model_dump_json()


def test_explicit_environment_does_not_fall_back(tmp_path, monkeypatch):
    monkeypatch.delenv("WENYI_TEST_FAKE_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-other-key")
    cfg = ProviderConfig(kind="gemini", api_key_env="WENYI_TEST_FAKE_KEY")
    assert CredentialStore(tmp_path).snapshot({"one": cfg})["one"] is None


@pytest.mark.parametrize("kind", ["openai", "gemini", "opencode-go"])
def test_sdk_creation_receives_snapshot_not_environment(kind, monkeypatch):
    import openai
    from google import genai
    from wenyi_core.llm.registry import provider_spec

    captured = []

    def sdk(**kwargs):
        captured.append(kwargs)
        return object()

    monkeypatch.setattr(openai, "OpenAI", sdk)
    monkeypatch.setattr(genai, "Client", sdk)
    monkeypatch.setenv("WENYI_TEST_FAKE_KEY", "fake-wrong-env")
    adapter = provider_spec(kind).adapter_type()(
        ProviderConfig(kind=kind, api_key_env="WENYI_TEST_FAKE_KEY"),
        credentials=("fake-snapshot",),
    )
    adapter._ensure_client()
    assert captured[0]["api_key"] == "fake-snapshot"
    assert len(captured) == 1


def test_router_is_local_authenticated_origin_restricted_and_never_echoes(
    tmp_path, desktop_context
):
    client = TestClient(create_app(context=desktop_context))
    assert client.get("/desktop/credentials").status_code == 401
    headers = {"Authorization": "Bearer fake-local-token", "Origin": "tauri://localhost"}
    assert (
        client.get(
            "/desktop/credentials", headers={**headers, "Origin": "https://evil.invalid"}
        ).status_code
        == 403
    )
    response = client.get("/desktop/credentials", headers=headers)
    assert response.status_code == 200
    connection = next(iter(response.json()))
    route = f"/desktop/credentials/{connection}"
    response = client.put(
        route, headers=headers, json={"mode": "manual", "secret": "fake-router-secret"}
    )
    assert response.status_code == 200
    assert response.json()["available"]
    assert "fake-router-secret" not in response.text
    response = client.put(
        route, headers=headers, json={"mode": "bad", "secret": "fake-router-secret"}
    )
    assert response.status_code == 422
    assert "fake-router-secret" not in response.text
    assert all(b"fake-router-secret" not in path.read_bytes() for path in tmp_path.iterdir())


def test_non_system_keyring_is_rejected(monkeypatch):
    import keyring

    monkeypatch.setattr(keyring, "get_keyring", lambda: FakeVault())
    assert system_keyring() is None


def test_registry_rename_and_delete_are_atomic_with_preferences(desktop_context, isolated_vault):
    from wenyi_backend.config_documents import global_document
    from wenyi_backend.global_settings import load_settings, save_settings

    current = load_settings()
    document = global_document(current.config)
    document["llm"]["preset"] = None
    old = next(iter(document["llm"]["providers"]))
    store = local_credentials.credential_store()
    store.update(old, mode="manual", secret="fake-global-rename")
    document["llm"]["providers"]["renamed"] = document["llm"]["providers"].pop(old)
    for model in document["llm"]["models"].values():
        if model["provider"] == old:
            model["provider"] = "renamed"
    saved = save_settings(
        json.dumps(document),
        current.default_template,
        current.revision,
        provider_renames={old: "renamed"},
    )
    assert store.snapshot(saved.config.llm.providers)["renamed"] == "fake-global-rename"
    assert old not in store._records()
    document["llm"]["providers"]["unused"] = {"kind": "fake"}
    saved = save_settings(json.dumps(document), saved.default_template, saved.revision)
    store.update("unused", mode="manual", secret="fake-delete")
    del document["llm"]["providers"]["unused"]
    save_settings(json.dumps(document), saved.default_template, saved.revision)
    assert list(isolated_vault.values.values()) == ["fake-global-rename"]


@pytest.mark.parametrize("operation", ["read", "write"])
def test_vault_wait_does_not_hold_catalog_writer(desktop_context, isolated_vault, operation):
    from threading import Event

    backend = desktop_context.repository
    store = desktop_context.settings_store.credentials
    name = next(iter(credentials.load_settings().config.llm.providers))
    store.update(name, mode="manual", secret="fake-before")
    entered, release = Event(), Event()
    original = isolated_vault.get_password if operation == "read" else isolated_vault.set_password

    def blocking(*args):
        entered.set()
        assert release.wait(10)
        return original(*args)

    if operation == "read":
        isolated_vault.get_password = blocking
        action = credentials.statuses
    else:
        isolated_vault.set_password = blocking

        def action():
            return credentials.save_credential(
                name, credentials.CredentialInput(mode="manual", secret="fake-after")
            )

    def catalog_write():
        with backend.transaction() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS unrelated_write (id INTEGER)")

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(copy_context().run, action)
        try:
            assert entered.wait(3)
            pool.submit(catalog_write).result(timeout=1)
        finally:
            release.set()
        pending.result(timeout=3)


@pytest.mark.parametrize("edit", ["rename", "delete", "provider", "revision"])
def test_vault_write_cannot_bind_to_changed_connection(desktop_context, isolated_vault, edit):
    from fastapi import HTTPException
    from wenyi_backend.config_documents import global_document
    from wenyi_backend.global_settings import load_settings, save_settings

    current = load_settings()
    document = global_document(current.config)
    document["llm"]["preset"] = None
    name = next(iter(document["llm"]["providers"]))
    store = local_credentials.credential_store()
    store.update(name, mode="manual", secret="fake-old")
    original = isolated_vault.set_password

    def concurrent_edit(*args):
        original(*args)
        renames = {}
        if edit in {"rename", "delete"}:
            document["llm"]["providers"]["replacement"] = document["llm"]["providers"].pop(name)
            for model in document["llm"]["models"].values():
                if model["provider"] == name:
                    model["provider"] = "replacement"
            if edit == "rename":
                renames[name] = "replacement"
        elif edit == "provider":
            document["llm"]["providers"][name]["base_url"] = "https://changed.invalid/v1"
        save_settings(
            json.dumps(document),
            current.default_template,
            current.revision,
            provider_renames=renames,
        )

    isolated_vault.set_password = concurrent_edit
    with pytest.raises(HTTPException) as caught:
        credentials.save_credential(
            name, credentials.CredentialInput(mode="manual", secret="fake-new")
        )
    assert caught.value.status_code == 409
    assert "fake-new" not in isolated_vault.values.values()
    assert list(isolated_vault.values.values()) == ([] if edit == "delete" else ["fake-old"])


def test_commit_failure_cleans_staged_key_and_preserves_old(
    desktop_context, isolated_vault, monkeypatch
):
    from fastapi import HTTPException

    store = desktop_context.settings_store.credentials
    name = next(iter(credentials.load_settings().config.llm.providers))
    store.update(name, mode="manual", secret="fake-old")
    previous = store._records()

    def fail(*args):
        raise OSError("fake-failure")

    monkeypatch.setattr(store, "_commit", fail)
    with pytest.raises(HTTPException) as caught:
        credentials.save_credential(
            name, credentials.CredentialInput(mode="manual", secret="fake-staged")
        )
    assert caught.value.status_code == 503
    assert store._records() == previous
    assert list(isolated_vault.values.values()) == ["fake-old"]


def test_release_drops_session_values_before_reopening_same_workspace(tmp_path, desktop_context):
    store = desktop_context.settings_store.credentials
    with TestClient(create_app(context=desktop_context)):
        store.update("one", mode="manual", storage="session", secret="fake-session")
    assert not store.snapshot({"one": provider()})["one"]
    context = create_context(tmp_path, credentials=CredentialStore(tmp_path))
    try:
        reopened = context.settings_store.credentials
        assert reopened is not store
        assert not reopened.status("one", provider())["available"]
    finally:
        context.repository.close()


def test_concurrent_credential_edit_keeps_winning_key(desktop_context, isolated_vault):
    from fastapi import HTTPException

    store = desktop_context.settings_store.credentials
    name = next(iter(credentials.load_settings().config.llm.providers))
    store.update(name, mode="manual", secret="fake-old")
    original = isolated_vault.set_password

    def concurrent_write(*args):
        original(*args)
        isolated_vault.set_password = original
        store.update(name, mode="manual", secret="fake-winner")

    isolated_vault.set_password = concurrent_write
    with pytest.raises(HTTPException) as caught:
        credentials.save_credential(
            name, credentials.CredentialInput(mode="manual", secret="fake-loser")
        )
    assert caught.value.status_code == 409
    assert list(isolated_vault.values.values()) == ["fake-winner"]


def test_transaction_exit_failure_cleans_new_key_only(desktop_context, isolated_vault, monkeypatch):
    from contextlib import contextmanager

    from fastapi import HTTPException

    store = desktop_context.settings_store.credentials
    name = next(iter(credentials.load_settings().config.llm.providers))
    store.update(name, mode="manual", secret="fake-old")
    previous = store._records()
    original = credentials.registry_guard
    calls = 0

    @contextmanager
    def fail_commit():
        nonlocal calls
        calls += 1
        with original() as conn:
            yield conn
            if calls == 2:
                raise OSError("fake-commit-failure")

    monkeypatch.setattr(credentials, "registry_guard", fail_commit)
    with pytest.raises(HTTPException) as caught:
        credentials.save_credential(
            name, credentials.CredentialInput(mode="manual", secret="fake-staged")
        )
    assert caught.value.status_code == 503
    assert store._records() == previous
    assert list(isolated_vault.values.values()) == ["fake-old"]
