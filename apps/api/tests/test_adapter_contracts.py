"""Offline checks for the API's concrete adapter contracts."""

from contextlib import contextmanager

import pytest
from psycopg_pool import ConnectionPool
from wenyi_api import dal
from wenyi_api.adapters import PostgresRepository, postgres_repository
from wenyi_api.config import Settings
from wenyi_api.storage_pg import PostgresStorage
from wenyi_api.telemetry import RedisTelemetry
from wenyi_api.workers.recovery import RecoveryStorage
from wenyi_backend.context import Repository, current_context
from wenyi_core.storage.file import FileStorage
from wenyi_core.storage.sqlite import SqliteStorage


def test_repository_contract_retains_dynamic_dal_lookup(monkeypatch):
    repository: Repository = PostgresRepository(Settings())
    first, second = {"id": "first"}, {"id": "second"}
    monkeypatch.setattr(dal, "get_project", lambda pid: first)
    assert repository.get_project("project") is first
    monkeypatch.setattr(dal, "get_project", lambda pid: second)
    assert repository.get_project("project") is second
    assert postgres_repository(current_context()) is current_context().repository


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

    telemetry = RedisTelemetry("redis://fixture/0")
    monkeypatch.setattr(telemetry, "connect", connect)
    assert telemetry.progress_snapshot("project") == expected
    assert keys == ["project:project:progress"]


def test_recovery_lock_contract_is_specific_to_database_storage(tmp_path):
    # An unopened pool and lazy SQLite store exercise contracts without services or writes.
    pool: ConnectionPool = ConnectionPool(open=False)
    postgres: RecoveryStorage = PostgresStorage("project", pool, run_dir=str(tmp_path))
    sqlite: RecoveryStorage = SqliteStorage(str(tmp_path), create=False)
    try:
        assert isinstance(postgres, RecoveryStorage)
        assert isinstance(sqlite, RecoveryStorage)
        assert not isinstance(FileStorage(str(tmp_path)), RecoveryStorage)
    finally:
        sqlite.close()
        pool.close()
