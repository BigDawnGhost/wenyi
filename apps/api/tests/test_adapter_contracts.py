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
