"""The local adapter preserves glossary lifetime and read-only Review semantics."""

import sqlite3
from contextlib import nullcontext
from pathlib import Path

import pytest
from wenyi_core.glossary.store import GlossaryStore, GlossaryTerm
from wenyi_core.llm.usage import UsageSample, UsageTracker
from wenyi_core.storage.file import FileStorage


@pytest.mark.parametrize("marker", [None, "{invalid", "{}", '{"source_sha256": 1}'])
def test_initialization_without_verified_identity_discards_usage(tmp_path, marker):
    storage = FileStorage(str(tmp_path))
    tracker = UsageTracker()
    tracker.record("fast", UsageSample(10, 5, 15), stage="terminology")
    with storage.lock():
        storage.save_usage(tracker.summary())
        tracker.record("fast", UsageSample(20, 10, 30), stage="terminology")
        storage.prepare_usage_commit({"usage.json": tracker.summary()})
        if marker is not None:
            (tmp_path / ".initializing.json").write_text(marker, encoding="utf-8")
        storage.begin_initialization("a" * 64)
        storage.recover_usage()
        assert storage.load_usage() is None
        assert storage.read_artifact("usage-pending.json") is None


@pytest.mark.parametrize("published", [False, True])
def test_initialization_retry_recovers_first_usage_commit(tmp_path, published):
    storage = FileStorage(str(tmp_path))
    tracker = UsageTracker()
    tracker.record("fast", UsageSample(10, 5, 15), stage="terminology")
    ledger = tracker.summary()
    with storage.lock():
        storage.begin_initialization("a" * 64)
        storage.prepare_usage_commit({"usage.json": ledger})
        if published:
            storage.save_usage(ledger)
    # Reopen the adapter to exercise recovery without relying on in-memory state.
    storage = FileStorage(str(tmp_path))
    with storage.lock():
        for _ in range(3):
            storage.begin_initialization("a" * 64)
            assert storage.load_usage() == ledger
            assert storage.read_artifact("usage-pending.json") is None


@pytest.mark.parametrize("interrupted", [False, True])
def test_run_lock_closes_glossary_and_allows_reuse(tmp_path, interrupted):
    storage = FileStorage(str(tmp_path))
    term = GlossaryTerm(source="Alice", target="爱丽丝")
    with pytest.raises(RuntimeError, match="interrupted") if interrupted else nullcontext():
        with storage.lock():
            storage.upsert_term(term)
            connection = storage._g.conn
            if interrupted:
                raise RuntimeError("interrupted")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    with storage.lock():
        assert storage.get_term(term.source) == term


def test_reading_terms_keeps_formal_glossary_and_wal_unchanged(tmp_path):
    storage = FileStorage(str(tmp_path))
    writer = GlossaryStore(storage.glossary_path)
    try:
        writer.conn.execute("PRAGMA wal_autocheckpoint=0")
        term = GlossaryTerm(source="Alice", target="爱丽丝")
        writer.upsert_term(term)

        def snapshot():
            return {
                path.name: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in tmp_path.glob("glossary.db*")
            }

        before = snapshot()
        assert storage.all_terms() == [term]
        assert storage._glossary is None
        assert snapshot() == before
    finally:
        storage.close()
        writer.close()


def test_reading_missing_glossary_does_not_create_database(tmp_path):
    storage = FileStorage(str(tmp_path))
    try:
        assert storage.all_terms() == []
        assert not Path(storage.glossary_path).exists()
    finally:
        storage.close()
