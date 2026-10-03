"""Offline guards and explicit Desktop application contexts."""

import pytest
from wenyi_backend.context import use_context
from wenyi_desktop import main
from wenyi_desktop.local_credentials import CredentialStore


@pytest.fixture(autouse=True)
def no_external_services(monkeypatch):
    import keyring
    import psycopg
    import psycopg_pool
    import redis
    import redis.asyncio

    def forbidden(*args, **kwargs):
        raise AssertionError("Offline Desktop attempted an external service connection")

    monkeypatch.setattr(psycopg, "connect", forbidden)
    monkeypatch.setattr(psycopg_pool.ConnectionPool, "__init__", forbidden)
    monkeypatch.setattr(redis, "from_url", forbidden)
    monkeypatch.setattr(redis.Redis, "from_url", forbidden)
    monkeypatch.setattr(redis.asyncio.Redis, "from_url", forbidden)
    monkeypatch.setattr(redis.connection.Connection, "connect", forbidden)
    monkeypatch.setattr(redis.asyncio.connection.Connection, "connect", forbidden)
    for name in ("get_password", "set_password", "delete_password", "get_keyring"):
        monkeypatch.setattr(keyring, name, forbidden)
    monkeypatch.setattr(main, "system_keyring", lambda: None)


@pytest.fixture
def desktop_context(tmp_path):
    workspace = tmp_path / "desktop"
    workspace.mkdir()
    context = main.create_context(workspace, credentials=CredentialStore(workspace), api_token=None)
    try:
        with use_context(context):
            yield context
    finally:
        context.settings_store.credentials.clear_session()
        context.repository.close()
