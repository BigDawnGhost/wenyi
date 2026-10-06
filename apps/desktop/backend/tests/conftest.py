"""Offline guards and explicit Desktop application contexts."""

from dataclasses import replace
from importlib.util import find_spec

import pytest
from fastapi.testclient import TestClient
from tests.fake_llm import MeteredFakeClient, routing_handler
from wenyi_backend.context import use_context
from wenyi_desktop import main
from wenyi_desktop.local_credentials import CredentialStore


@pytest.fixture(autouse=True)
def no_external_services(monkeypatch):
    import keyring

    def forbidden(*args, **kwargs):
        raise AssertionError("Offline Desktop attempted an external service connection")

    # Web dependencies are absent in Desktop-only installs, but must remain
    # guarded when the tests run in a full workspace environment.
    if find_spec("psycopg") is not None:
        import psycopg

        monkeypatch.setattr(psycopg, "connect", forbidden)
    if find_spec("psycopg_pool") is not None:
        import psycopg_pool

        monkeypatch.setattr(psycopg_pool.ConnectionPool, "__init__", forbidden)
    if find_spec("redis") is not None:
        import redis
        import redis.asyncio

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


@pytest.fixture
def desktop(tmp_path):
    fake = MeteredFakeClient(routing_handler)
    context = replace(
        main.create_context(tmp_path / "desktop", api_token="offline-test-token"),
        build_client=lambda config: fake,
    )
    backend = context.repository
    try:
        with (
            use_context(context),
            TestClient(
                main.create_app(context=context),
                headers={"Authorization": "Bearer offline-test-token"},
            ) as client,
        ):
            yield client, backend, fake
    finally:
        backend.close()
