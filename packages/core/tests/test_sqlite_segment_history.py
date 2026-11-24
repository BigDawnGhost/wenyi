"""Desktop segment history uses the event ledger without scanning unrelated records."""

import json
from contextlib import closing

import pytest
from wenyi_core.models.document import Chapter, Segment
from wenyi_core.storage.sqlite import SCHEMA_VERSION, SqliteStorage

_EVENT_INDEX = "manual_translation_events_segment"
_EVENT_KEY = "events.jsonl"
_TS = "2025-01-02T03:04:05+00:00"


@pytest.fixture
def store(tmp_path):
    storage = SqliteStorage(str(tmp_path))
    storage.save_chapter(
        Chapter(
            index=3,
            title="Chapter",
            segments=[Segment(index=index, source="Text") for index in (7, 8)],
        )
    )
    yield storage
    storage.close_all()


def _manual_edit(**overrides):
    return {
        "ts": _TS,
        "event": "manual_translation_edited",
        "chapter": 3,
        "index": 7,
        "before": None,
        "after": "",
        **overrides,
    }


def test_segment_history_lookup_work_does_not_grow_with_unrelated_events(store):
    store.append_artifact_record(_EVENT_KEY, _manual_edit())
    conn = store._connection()
    with store.state_lock():
        for unrelated in (
            _manual_edit(event="translation_batch"),
            _manual_edit(chapter=4),
            _manual_edit(index=9),
        ):
            conn.executemany(
                "INSERT INTO artifact_records(key,value) VALUES(?,?)",
                [(_EVENT_KEY, json.dumps(unrelated))] * 4000,
            )

    # Bound SQLite VM work, not elapsed time. This rejects a scan even if
    # irrelevant values are filtered in SQL instead of decoded in Python.
    statements = []
    conn.set_trace_callback(statements.append)
    conn.set_progress_handler(lambda: 1, 2000)
    try:
        assert store.load_segment_history(3, 7) == [
            {"id": "event-1", "kind": "manual", "before": None, "after": "", "created_at": _TS}
        ]
        assert store.load_segment_history(3, 8) == []
    finally:
        conn.set_progress_handler(None, 0)
        conn.set_trace_callback(None)

    queries = [sql for sql in statements if "FROM artifact_records" in sql]
    assert len(queries) == 2
    for sql in queries:
        details = [row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}")]
        seeks = [
            detail for detail in details if detail.startswith("SEARCH ") and _EVENT_INDEX in detail
        ]
        assert seeks, details


def test_legacy_manual_edits_merge_with_revisions_without_changing_identity(store):
    chapter = store.load_chapter(3)
    chapter.segments[0].target = ""
    store.save_chapter(chapter, revision_kind="manual")
    store._connection().execute("UPDATE segment_revisions SET created_at=?", (_TS,))

    older = "2024-01-02T03:04:05+00:00"
    missing_after = _manual_edit()
    del missing_after["after"]
    for event in (
        _manual_edit(before="", after=None),
        _manual_edit(history_recorded=False),
        _manual_edit(ts=older, after="Older", history_recorded=None),
        _manual_edit(history_recorded=True),
        _manual_edit(history_recorded="false"),  # Preserve Python truthiness.
        _manual_edit(chapter="3"),
        _manual_edit(index="7"),
        _manual_edit(before="", after=""),
        missing_after,
    ):
        store.append_artifact_record(_EVENT_KEY, event)
    store.append_artifact_record("reviews/run/events.jsonl", _manual_edit())

    assert [
        (row["id"], row["kind"], row["before"], row["after"], row["created_at"])
        for row in store.load_segment_history(3, 7)
    ] == [
        ("event-2", "manual", None, "", _TS),
        ("revision-1", "manual", None, "", _TS),
        ("event-1", "manual", "", None, _TS),
        ("event-3", "manual", None, "Older", older),
    ]


def _index_exists(conn):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (_EVENT_INDEX,)
    ).fetchone()
    return row is not None


@pytest.mark.parametrize("old_version", [1, 3])
def test_legacy_manual_edits_recover_from_wal_before_optional_index_install(store, old_version):
    conn = store._connection()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute(f"DROP INDEX {_EVENT_INDEX}")
    conn.execute(f"PRAGMA user_version={old_version}")
    store.append_artifact_record(_EVENT_KEY, _manual_edit())
    before = [
        tuple(row) for row in conn.execute("SELECT sequence, key, value FROM artifact_records")
    ]

    # Recovery can open and see committed WAL entries even while another
    # connection writes. Its fallback must not create the optional index.
    with pytest.raises(RuntimeError, match="discard"):
        with store.state_lock():
            store.append_artifact_record(_EVENT_KEY, _manual_edit(after="Uncommitted"))
            with closing(SqliteStorage(store.run_dir, create=False)) as recovery:
                expected = recovery.load_segment_history(3, 7)
                assert [row["id"] for row in expected] == ["event-1"]
                assert recovery._connection().execute("PRAGMA user_version").fetchone()[0] == (
                    old_version
                )
                assert not _index_exists(recovery._connection())
            raise RuntimeError("discard uncommitted edit")

    with closing(SqliteStorage(store.run_dir, create=False, upgrade=True)) as upgraded:
        assert upgraded.load_segment_history(3, 7) == expected
        upgraded_conn = upgraded._connection()
        assert upgraded_conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert _index_exists(upgraded_conn)
        assert [
            tuple(row)
            for row in upgraded_conn.execute("SELECT sequence, key, value FROM artifact_records")
        ] == before
