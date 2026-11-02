"""Offline checks for the API's concrete adapter contracts."""

import inspect
from contextlib import contextmanager

import pytest
from psycopg_pool import ConnectionPool
from wenyi_api import dal
from wenyi_api.adapters import PostgresRepository, postgres_repository
from wenyi_api.config import Settings
from wenyi_api.storage_pg import PostgresStorage
from wenyi_api.telemetry import RedisTelemetry
from wenyi_api.workers.recovery import RecoveryStorage
from wenyi_backend.context import BackendStorage, Repository, current_context
from wenyi_core.storage.file import FileStorage
from wenyi_core.storage.sqlite import SqliteStorage


def test_repository_exposes_only_explicit_typed_operations(monkeypatch):
    repository: Repository = PostgresRepository(Settings())
    assert repository.get_project is dal.get_project
    monkeypatch.setattr(dal, "accidental_export", lambda: None, raising=False)
    assert not hasattr(repository, "accidental_export")
    assert not hasattr(repository, "__getattr__")
    assert postgres_repository(current_context()) is current_context().repository


def test_strategy_and_admission_calls_match_repository():
    repository: Repository = PostgresRepository(Settings())
    inspect.signature(repository.set_project_strategy).bind(
        "project", {"steps": {}}, connection=None
    )
    inspect.signature(repository.admit_job).bind(
        "project",
        "translation",
        "job",
        project_status="translating",
        params={},
        config_snapshot={},
        run_id="run",
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, None), (b"", None), (b'{"kind":"progress"}', {"kind": "progress"})],
)
def test_progress_snapshot_reads_a_synchronous_redis_value(monkeypatch, raw, expected):
    keys = []

    class Cache:
        def get(self, key):
            keys.append(key)
            return raw

    @contextmanager
    def connect():
        yield Cache()

    telemetry = RedisTelemetry("redis://fixture/0", current_context().repository)
    monkeypatch.setattr(telemetry, "connect", connect)
    assert telemetry.progress_snapshot("project") == expected
    assert keys == ["project:project:progress"]


def test_a_silent_progress_channel_still_proves_the_link_is_alive(monkeypatch):
    import asyncio
    import json

    from wenyi_api import telemetry as module

    sent = []
    # A short deadline keeps the test fast; the interval itself is the constant
    # the relay reads, so overriding it still exercises the real branch.
    monkeypatch.setattr(module, "HEARTBEAT_SECONDS", 0.05)

    class PubSub:
        async def subscribe(self, channel):
            return None

        async def get_message(self, **_):
            # An idle channel: the run is inside one long model call.
            return None

        async def aclose(self):
            return None

    class Redis:
        def pubsub(self):
            return PubSub()

        async def aclose(self):
            return None

    class Socket:
        async def send_json(self, payload):
            sent.append(payload)

        async def send_text(self, payload):
            sent.append(json.loads(payload))

        async def receive(self):
            # Heartbeats alone end the relay, so no peer is needed to close it.
            while sum(frame.get("kind") == "heartbeat" for frame in sent) < 2:
                await asyncio.sleep(0.01)
            return {"type": "websocket.disconnect"}

    class Projects:
        def get_project(self, pid):
            return {"id": pid}

        def chapter_summaries(self, pid):
            return []

    monkeypatch.setattr(module.AsyncRedis, "from_url", classmethod(lambda cls, url: Redis()))
    telemetry = module.RedisTelemetry("redis://fixture/0", Projects())
    asyncio.run(telemetry.relay(Socket(), "project"))
    assert sent[0] == {"kind": "snapshot", "project": {"id": "project"}, "chapters": []}
    assert [frame for frame in sent if frame.get("kind") == "heartbeat"]


def test_recovery_lock_contract_is_specific_to_database_storage(tmp_path):
    # An unopened pool and lazy SQLite store exercise contracts without services or writes.
    pool: ConnectionPool = ConnectionPool(open=False)
    postgres: BackendStorage = PostgresStorage("project", pool, run_dir=str(tmp_path))
    sqlite: BackendStorage = SqliteStorage(str(tmp_path), create=False)
    try:
        assert isinstance(postgres, BackendStorage)
        assert isinstance(sqlite, BackendStorage)
        assert isinstance(postgres, RecoveryStorage)
        assert isinstance(sqlite, RecoveryStorage)
        assert not isinstance(FileStorage(str(tmp_path)), BackendStorage)
        assert not isinstance(FileStorage(str(tmp_path)), RecoveryStorage)
    finally:
        sqlite.close()
        pool.close()
