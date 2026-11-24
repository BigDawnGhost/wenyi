"""Checkpoint-boundary durability for append-only event ledgers.

Only a record whose loss would repeat completed work pays for a sync. The tests
pin the boundary itself, so an added durable event has to be an explicit choice
rather than a side effect of logging more.
"""

from __future__ import annotations

import json
import os
from threading import Thread

import pytest
from wenyi_core.storage.artifacts import FileArtifacts, is_durable_record
from wenyi_core.storage.run_store import RunStore
from wenyi_core.storage.sqlite import SqliteStorage


@pytest.fixture
def syncs(monkeypatch):
    """Record every fsync the storage layer issues."""

    calls: list[int] = []
    real_fsync = os.fsync

    def record(descriptor: int) -> None:
        calls.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", record)
    return calls


def test_only_the_glossary_checkpoint_is_a_durable_record():
    assert is_durable_record({"event": "batch_glossary_extracted", "chapter": 0})
    for event in (
        "translated_batch",
        "assembled",
        "usage_summary",
        "book_synopsis_saved",
        "review_agent_finished",
    ):
        assert not is_durable_record({"event": event})
    assert not is_durable_record({})


def test_file_events_sync_only_at_the_checkpoint_boundary(tmp_path, syncs):
    store = RunStore(str(tmp_path / "run"))
    for index in range(3):
        store.log_event("translated_batch", chapter=0, start_index=index, count=1)
    assert syncs == [], "ordinary progress events must stay buffered"

    store.log_event("batch_glossary_extracted", chapter=0, start_index=0, count=3, summary={})
    assert len(syncs) == 1, "the checkpoint record must reach the disk before returning"

    with open(store.event_log_path, encoding="utf-8") as handle:
        rows = [json.loads(line) for line in handle]
    assert [row["event"] for row in rows] == [
        "translated_batch",
        "translated_batch",
        "translated_batch",
        "batch_glossary_extracted",
    ]
    # The sync must not change what a resumed run reads back.
    reopened = RunStore(store.run_dir, create=False)
    assert reopened.completed_batch_glossary_keys(0) == {"0:3"}


def test_file_artifact_appends_sync_only_a_checkpoint_record(tmp_path, syncs):
    storage = FileArtifacts(str(tmp_path / "run"))
    storage.append_artifact_record("events.jsonl", {"event": "hint"})
    assert syncs == []

    storage.append_artifact_record("events.jsonl", {"event": "batch_glossary_extracted"})
    assert len(syncs) == 1

    storage.append_artifact_record("events.jsonl", {"event": "hint-2"})
    assert len(syncs) == 1, "only the checkpoint record pays for a sync"

    assert [row["event"] for row in storage.read_artifact_records("events.jsonl")] == [
        "hint",
        "batch_glossary_extracted",
        "hint-2",
    ]


def test_concurrent_checkpoint_appends_keep_one_record_per_line(tmp_path):
    store = RunStore(str(tmp_path / "run"))

    def append(worker: int) -> None:
        for sequence in range(25):
            store.log_event(
                "batch_glossary_extracted",
                chapter=worker,
                start_index=sequence,
                count=1,
                payload="x" * 200,
            )

    threads = [Thread(target=append, args=(worker,)) for worker in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    with open(store.event_log_path, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    assert len(lines) == 75
    rows = [json.loads(line) for line in lines]
    assert all(row["event"] == "batch_glossary_extracted" for row in rows)
    assert store.completed_batch_glossary_keys(0) == {
        RunStore.batch_glossary_key(sequence, 1) for sequence in range(25)
    }


@pytest.fixture
def sqlite_store(tmp_path):
    storage = SqliteStorage(str(tmp_path / "run"))
    yield storage
    storage.close_all()


def test_sqlite_connections_commit_checkpoints_synchronously(sqlite_store):
    connection = sqlite_store._connection()
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    # 2 is FULL: a committed checkpoint event is on the disk before log_event returns,
    # whatever a database left by another tool asked for.
    assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_sqlite_checkpoint_records_stay_readable_after_reopen(tmp_path):
    storage = SqliteStorage(str(tmp_path / "run"))
    storage.log_event("translated_batch", chapter=0, start_index=0, count=1)
    storage.log_event("batch_glossary_extracted", chapter=0, start_index=0, count=1)
    storage.close_all()

    reopened = SqliteStorage(str(tmp_path / "run"), create=False)
    try:
        assert [event["event"] for event in reopened.list_events()] == [
            "translated_batch",
            "batch_glossary_extracted",
        ]
        assert reopened.completed_batch_glossary_keys(0) == {"0:1"}
    finally:
        reopened.close_all()
