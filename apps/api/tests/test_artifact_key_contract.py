"""The artifact naming contract is shared by all three adapters."""

from contextlib import nullcontext
from unittest.mock import Mock

import pytest
from psycopg_pool import ConnectionPool
from wenyi_api.storage_pg import PostgresStorage
from wenyi_core.storage.artifacts import FileArtifacts
from wenyi_core.storage.sqlite import SqliteStorage


@pytest.fixture(params=["file", "sqlite", "postgres"])
def artifact_adapter(request, tmp_path):
    if request.param == "file":
        yield FileArtifacts(str(tmp_path)), None
    elif request.param == "sqlite":
        store = SqliteStorage(str(tmp_path))
        try:
            yield store, None
        finally:
            store.close()
    else:
        pool = Mock(spec=ConnectionPool)
        pool.connection.side_effect = AssertionError("Unexpected PostgreSQL connection")
        yield PostgresStorage("project", pool), pool


@pytest.mark.parametrize("key", ["", "reports/", "reports/drive:summary.json"])
def test_artifact_operations_reject_nonportable_keys(artifact_adapter, key):
    store, _ = artifact_adapter
    for operation, args in (
        (store.read_artifact, ()),
        (store.write_artifact, ({},)),
        (store.delete_artifact, ()),
        (store.append_artifact_record, ({},)),
        (store.read_artifact_records, ()),
    ):
        with pytest.raises(ValueError):
            operation(key, *args)


@pytest.mark.parametrize("prefix", ["reports//", "reports:/", None])
def test_artifact_listing_rejects_nonportable_prefixes(artifact_adapter, prefix):
    store, _ = artifact_adapter
    with pytest.raises(ValueError):
        store.list_artifacts(prefix)


@pytest.mark.parametrize(
    ("prefix", "expected"),
    [
        ("reports/su", ["reports/summary.json"]),
        ("reports/", ["reports/summary.json"]),
        ("reports", ["reports-archive/summary.json", "reports/summary.json"]),
        ("", ["other.json", "reports-archive/summary.json", "reports/summary.json"]),
    ],
)
def test_artifact_listing_preserves_literal_prefix(artifact_adapter, prefix, expected):
    store, pool = artifact_adapter
    if pool is None:
        for key in ("reports/summary.json", "reports-archive/summary.json", "other.json"):
            store.write_artifact(key, {})
    else:
        # Only the database execution is stubbed; the adapter must pass the prefix unchanged.
        conn = Mock()
        conn.execute.return_value.fetchall.return_value = [(key,) for key in expected]
        pool.connection.side_effect = None
        pool.connection.return_value = nullcontext(conn)

    assert store.list_artifacts(prefix) == expected
    if pool is not None:
        assert conn.execute.call_args.args[1] == ("project", prefix, "project", prefix)
