"""Local shared storage contracts, using only temporary synthetic book state."""

from __future__ import annotations

import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from wenyi_core.config import Config
from wenyi_core.glossary.store import GlossaryTerm
from wenyi_core.ingest.models import Chapter, Document, Segment
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.llm.usage import UsageSample, UsageTracker, empty_usage
from wenyi_core.pipeline.orchestrator import Orchestrator
from wenyi_core.pipeline.runstore import RunStore, source_sha256
from wenyi_core.review.models import ReviewOutcome
from wenyi_core.review.run_store import ReviewRunStore
from wenyi_core.storage.protocol import Storage
from wenyi_core.storage.sqlite import SqliteStorage, StorageBusyError


@pytest.fixture
def document(tmp_path):
    source = tmp_path / "original.txt"
    source.write_text("Synthetic source.", encoding="utf-8")
    return Document(
        title="Synthetic",
        source_lang="en",
        target_lang="zh",
        source_path=str(source),
        fmt="text",
        meta={"epub_annotation_contexts": {"note": "source note"}},
        chapters=[
            Chapter(
                index=3,
                title="Chapter",
                href="chapter.xhtml",
                meta={"toc_entry_id": "toc-3"},
                segments=[
                    Segment(index=7, source="First", anchor="s7", meta={"babeldoc_id": 12}),
                    Segment(index=9, source="Second", target=""),
                ],
            )
        ],
    )


@pytest.fixture
def store(tmp_path):
    storage = SqliteStorage(str(tmp_path / "run"))
    yield storage
    storage.close()


def test_missing_discovery_never_creates_state(tmp_path):
    root = tmp_path / "missing"
    store = SqliteStorage(str(root), create=False)
    assert isinstance(store, Storage)
    assert not store.exists()
    assert store.load_context() is None
    assert store.all_terms() == []
    assert store.list_artifacts() == []
    assert store.read_artifact_records("events.jsonl") == []
    assert store.stats() == {"terms": 0, "open_conflicts": 0}
    with pytest.raises(FileNotFoundError, match="not initialized"):
        store.load_manifest()
    with pytest.raises(FileNotFoundError):
        store.save_context({})
    store.close()
    assert not root.exists()


def test_events_keep_persisted_identity_and_legacy_payload(store):
    from datetime import datetime, timedelta

    legacy = {"ts": "2025-01-01T08:00:00+08:00", "event": "review_started", "review_id": "old"}
    store.append_artifact_record("events.jsonl", legacy)
    store.append_artifact_record("other.jsonl", {"event": "unrelated"})
    store.log_event("checkpoint", chapter=3)
    store.log_event("review_started", review_id="new")
    records = store.read_artifact_records("events.jsonl")
    events = store.list_events(limit=0)
    assert [row["_id"] for row in events] == [1, 3, 4]
    assert {k: v for k, v in events[0].items() if not k.startswith("_")} == legacy
    assert events[0]["_ts"] == legacy["ts"]
    timestamp = datetime.fromisoformat(events[-1]["_ts"])
    assert timestamp.utcoffset() == timedelta(0)
    assert len(events[-1]["_ts"].split(".")[1].split("+")[0]) == 6
    assert store.list_events(limit=2) == events[-2:]
    assert store.list_events(event_type="review_started", limit=1) == events[-1:]
    assert store.list_events(event_type="missing") == []
    store.close()
    assert store.list_events(limit=0) == events
    assert store.read_artifact_records("events.jsonl") == records


def test_discovery_waits_for_initial_schema_commit(tmp_path, monkeypatch):
    import sqlite3
    from concurrent.futures import TimeoutError
    from threading import Event

    root = tmp_path / "initializing"
    schema_started, allow_commit, read_started = Event(), Event(), Event()
    real_connect = sqlite3.connect

    class PausingConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql.startswith("CREATE TABLE IF NOT EXISTS artifacts"):
                schema_started.set()
                assert allow_commit.wait(5)
            return super().execute(sql, *args, **kwargs)

    def connect(*args, **kwargs):
        return real_connect(*args, factory=PausingConnection, **kwargs)

    def discover():
        reader = SqliteStorage(str(root), create=False)
        try:
            read_started.set()
            return reader.exists()
        finally:
            reader.close()

    monkeypatch.setattr(sqlite3, "connect", connect)
    with ThreadPoolExecutor(max_workers=2) as executor:
        creation = executor.submit(SqliteStorage, str(root))
        assert schema_started.wait(5)
        assert (root / "state.sqlite3").is_file()
        try:
            discovery = executor.submit(discover)
            assert read_started.wait(5)
            with pytest.raises(TimeoutError):
                discovery.result(timeout=0.1)
        finally:
            allow_commit.set()
            creation.result(timeout=5).close()
        # An initialized schema without a committed manifest is not yet a book.
        assert discovery.result(timeout=5) is False


def test_stage_manifest_last_and_all_major_state(store, document):
    digest = source_sha256(document.source_path)
    store.begin_initialization(digest)
    manifest = store.stage_document(document)
    assert not store.exists()
    assert store.load_chapter(3) == document.chapters[0]
    assert store.load_annotation_contexts() == {"note": "source note"}
    assert "epub_annotation_contexts" not in manifest["meta"]
    for name in ("context", "analysis", "report", "usage"):
        getattr(store, f"save_{name}")({"name": name})
        assert getattr(store, f"load_{name}")() == {"name": name}
    store.upsert_term(GlossaryTerm("First", "第一"), chapter=3)
    assert not store.exists()
    store.save_manifest(manifest)
    store.finish_initialization()
    assert store.exists()
    assert store.pending_chapters() == [3]
    assert store.ensure_source_identity(document.source_path) == digest
    with pytest.raises(ValueError, match="does not match"):
        store.ensure_source_identity(document.source_path, actual_sha256="0" * 64)
    assert store.load_chapter(3).segments[0].target is None
    assert store.load_chapter(3).segments[1].target == ""
    store.save_chapter_with_status(document.chapters[0], "done")
    assert store.pending_chapters() == []
    store.record_timing({"id": "one", "elapsed_seconds": 3})
    store.record_timing({"id": "one", "elapsed_seconds": 4})
    store.record_timing({"id": "two", "elapsed_seconds": 2})
    assert store.load_timing()["total_seconds"] == 6
    store.close()
    manifest["chapters"][0]["status"] = "done"
    assert store.load_manifest() == manifest
    assert store.load_chapter(3) == document.chapters[0]
    assert {p.name for p in Path(store.run_dir).iterdir()} <= {
        ".schema.lock",
        "state.sqlite3",
        "state.sqlite3-wal",
        "state.sqlite3-shm",
    }


@pytest.mark.parametrize("matching_source", [True, False])
def test_initialization_preserves_only_matching_source_preview(store, document, matching_source):
    digest = source_sha256(document.source_path)
    parsed = {
        "source_sha256": digest if matching_source else "0" * 64,
        "document": document.model_dump(mode="json"),
    }
    store.write_artifact("parsed_document.json", parsed)
    store.write_artifact("preview.json", {"title": document.title})
    store.write_artifact("analysis.json", {"stale": True})
    for _attempt in range(2):
        store.begin_initialization(digest)
        assert store.read_artifact("parsed_document.json") == (parsed if matching_source else None)
        assert store.read_artifact("preview.json") == (
            {"title": document.title} if matching_source else None
        )
        assert store.load_analysis() is None
        assert not store.exists()


def test_initialization_failure_retry_and_source_isolation(store, document, monkeypatch):
    digest = source_sha256(document.source_path)
    store.begin_initialization(digest)
    store.stage_document(document)
    store.save_analysis({"stale": True})
    store.upsert_term(GlossaryTerm("stale", "old"))
    store.log_event("failed")
    store.write_artifact("reviews/review-1/result.json", {"failed": True})
    cache = Path(store.source_dir) / "cache.bin"
    cache.parent.mkdir()
    cache.write_bytes(b"expensive parser cache")
    store.begin_initialization(digest)
    assert store.load_analysis() is None
    assert store.all_terms() == []
    with pytest.raises(FileNotFoundError):
        store.load_chapter(3)
    assert len(store.list_events()) == 1
    assert store.load_latest_review_result() == {"failed": True}
    store.begin_initialization("0" * 64)
    assert store.list_events() == []
    assert store.load_latest_review_result() is None
    assert cache.read_bytes() == b"expensive parser cache"

    def fail(_manifest):
        raise RuntimeError("manifest failed")

    monkeypatch.setattr(store, "save_manifest", fail)
    with pytest.raises(RuntimeError, match="manifest failed"):
        store.init_from_document(document)
    assert not store.exists()
    with pytest.raises(FileNotFoundError):
        store.load_chapter(3)


def test_outer_transaction_includes_glossary_and_nested_rollback(store, document):
    store.init_from_document(document)
    reader = SqliteStorage(store.run_dir, create=False)
    try:
        with pytest.raises(RuntimeError):
            with store.state_lock():
                store.save_context({"temporary": True})
                store.upsert_term(GlossaryTerm("First", "第一"))
                store.upsert_term(GlossaryTerm("First", "另译"))
                chapter = store.load_chapter(3)
                chapter.segments[0].target = "Uncommitted"
                store.save_chapter_with_status(chapter, "done")
                assert reader.load_context() is None
                assert reader.get_term("First") is None
                assert reader.load_chapter(3).segments[0].target is None
                assert reader.chapter_statistics()[0]["status"] == "pending"
                snapshot = reader.create_export_snapshot(
                    actual_sha256=source_sha256(document.source_path)
                )
                assert snapshot.load_chapter(3).segments[0].target is None
                raise RuntimeError("rollback")
        assert store.load_context() is None
        assert store.stats() == {"terms": 0, "open_conflicts": 0}
        with store.state_lock():
            store.save_context({"kept": True})
            with pytest.raises(ValueError):
                with store.state_lock():
                    store.save_context({"discarded": True})
                    raise ValueError("inner")
            assert store.load_context() == {"kept": True}
        assert reader.load_context() == {"kept": True}
    finally:
        reader.close()


@pytest.mark.parametrize("key", ["", "../x", "/x", "a/../x", "a//x", "a\\x", "C:/x", "a/./x"])
def test_artifact_key_validation(store, key):
    for operation in (
        lambda: store.write_artifact(key, {}),
        lambda: store.read_artifact(key),
        lambda: store.delete_artifact(key),
        lambda: store.append_artifact_record(key, {}),
        lambda: store.read_artifact_records(key),
    ):
        with pytest.raises(ValueError, match="Artifact key"):
            operation()


def test_artifact_prefix_append_order_and_review_recovery(store):
    for key in ("reviews/review-1/result.json", "reviews/review-2/result.json", "review_X"):
        store.write_artifact(key, {"key": key})
    assert store.list_artifacts("reviews/review-1/") == ["reviews/review-1/result.json"]
    store.write_artifact("review_%/a", 1)
    assert store.list_artifacts("review_%") == ["review_%/a"]
    assert store.load_latest_review_result() == {"key": "reviews/review-2/result.json"}
    for index in range(20):
        store.append_artifact_record("reviews/review-2/rounds.jsonl", {"index": index})
    store.write_artifact("reviews/review-2/autofix/publication.json", {"status": "pending"})
    store.close()
    assert store.read_artifact_records("reviews/review-2/rounds.jsonl") == [
        {"index": index} for index in range(20)
    ]
    assert store.read_artifact("reviews/review-2/autofix/publication.json") == {"status": "pending"}
    store.delete_artifact("reviews/review-2/rounds.jsonl")
    assert store.read_artifact_records("reviews/review-2/rounds.jsonl") == []
    assert "reviews/review-2/rounds.jsonl" not in store.list_artifacts()
    store.log_event("batch_glossary_extracted", chapter=3, start_index=7, count=2)
    assert store.completed_batch_glossary_keys(3) == {"7:2"}
    store.log_event("other", chapter=3)
    assert len(store.list_events(event_type="batch_glossary_extracted")) == 1
    assert store.list_events(limit=1)[0]["event"] == "other"


def test_usage_commit_is_atomic_idempotent_and_detects_interference(store):
    tracker = UsageTracker()
    tracker.record("fast", UsageSample(10, 5, 15))
    usage = tracker.summary()
    store.save_usage(empty_usage())
    store.prepare_usage_commit({"usage.json": usage, "reviews/review-1/usage.json": usage})
    store.close()
    store.recover_usage()
    store.recover_usage()
    assert store.load_usage() == usage
    assert store.read_artifact("reviews/review-1/usage.json") == usage
    assert store.read_artifact("usage-pending.json") is None
    store.prepare_usage_commit({"reviews/review-2/usage.json": usage, "usage.json": usage})
    store.save_usage(empty_usage())
    with pytest.raises(ValueError, match="changed outside"):
        store.recover_usage()
    assert store.read_artifact("reviews/review-2/usage.json") is None
    assert store.read_artifact("usage-pending.json") is not None
    with pytest.raises(ValueError, match="Invalid usage"):
        store.prepare_usage_commit({"context.json": usage})


def test_glossary_rules_and_wal_readonly_snapshot(store):
    term = GlossaryTerm("Ann", "安", aliases=["Annie"])
    assert store.upsert_term(term, chapter=3) == "inserted"
    assert store.upsert_term(GlossaryTerm("Ann", "安", aliases=["Anne"])) == "unchanged"
    assert store.upsert_term(GlossaryTerm("Ann", "安妮"), chapter=4) == "conflict"
    assert store.get_term("Ann").target == "安"
    assert store.get_term("Ann").first_chapter == 3
    assert store.get_term("Ann").aliases == ["Anne", "Annie"]
    assert store.terms_in_text("Anna") == []
    assert len(store.terms_in_text("Annie")) == 1
    snapshot = SqliteStorage(store.run_dir, create=False)
    paths = [Path(store.database_path), Path(store.database_path + "-wal")]
    before = [(path.stat().st_mtime_ns, path.read_bytes()) for path in paths]
    assert snapshot.all_terms() == store.all_terms()
    assert [(path.stat().st_mtime_ns, path.read_bytes()) for path in paths] == before
    assert snapshot._conn is None
    store.resolve_term("Ann", "安妮")
    store.mark_conflicts_resolved("Ann")
    assert store.open_conflicts() == []
    assert store.stats() == {"terms": 1, "open_conflicts": 0}
    assert store.delete_term("Ann")
    assert not store.delete_term("Ann")


def test_export_snapshot_is_consistent_and_detached(store, document):
    manifest = store.init_from_document(document)
    snapshot = store.create_export_snapshot(actual_sha256=manifest["source_sha256"])
    chapter = store.load_chapter(3)
    chapter.segments[0].target = "new"
    store.save_chapter_with_status(chapter, "done")
    assert snapshot.load_chapter(3).segments[0].target is None
    assert snapshot.load_manifest()["chapters"][0]["status"] == "pending"
    snapshot.load_chapter(3).segments[0].target = "accidental"
    assert snapshot.load_chapter(3).segments[0].target is None
    assert snapshot.source_dir == store.source_dir
    with pytest.raises(ValueError, match="does not match"):
        store.create_export_snapshot(actual_sha256="0" * 64)


def _lock_attempt(run_dir, kind, queue):
    storage = SqliteStorage(run_dir, create=False)
    try:
        lock = {
            "run": lambda: storage.lock(False),
            "export": lambda: storage.export_lock(1, False),
            "history": lambda: storage.export_history_lock(False),
        }[kind]()
        with lock:
            queue.put("acquired")
    except StorageBusyError:
        queue.put("busy")
    finally:
        storage.close()


@pytest.mark.parametrize("kind", ["run", "export", "history"])
def test_cross_process_workflow_lock_contention(store, kind):
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    make_lock = {
        "run": lambda: store.lock(False),
        "export": lambda: store.export_lock(1, False),
        "history": lambda: store.export_history_lock(False),
    }[kind]
    lock = make_lock()
    with lock:
        child = context.Process(target=_lock_attempt, args=(store.run_dir, kind, queue))
        child.start()
        try:
            assert queue.get(timeout=15) == "busy"
        finally:
            child.join(timeout=15)
        assert child.exitcode == 0
        # Long locks must not retain a SQLite write transaction.
        other = SqliteStorage(store.run_dir, create=False)
        other.save_context({"independent": True})
        other.close()
    with make_lock():
        pass
    queue.close()


def test_export_history_lock_is_independent_of_rendering_and_other_projects(store, tmp_path):
    other = SqliteStorage(store.run_dir, create=False)
    unrelated = SqliteStorage(str(tmp_path / "unrelated"))
    try:
        with store.export_history_lock(), store.export_history_lock(False):
            with other.assemble_lock(), other.export_lock(1, False), other.lock(False):
                other.save_context({"rendering": True})
            with unrelated.export_history_lock(False):
                pass
            with pytest.raises(StorageBusyError):
                with other.export_history_lock(False):
                    pass
    finally:
        other.close()
        unrelated.close()


def test_run_lock_reenters_only_on_the_owning_thread(store):
    def contender():
        with pytest.raises(StorageBusyError):
            with store.lock(False):
                pass

    with store.lock():
        with store.state_lock():
            with store.lock(False):
                store.save_context({"nested": True})
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(contender).result(timeout=5)
        other = SqliteStorage(store.run_dir, create=False)
        try:
            with pytest.raises(StorageBusyError):
                with other.lock(False):
                    pass
        finally:
            other.close()
    assert store.load_context() == {"nested": True}


def test_export_lock_uses_the_api_integer_identity(store):
    with store.export_lock(1):
        with store.export_lock(1, False), store.export_lock(2, False):
            pass
    for invalid in (True, False, 0, -1, "1", None):
        with pytest.raises(ValueError, match="export"):
            store.export_lock(invalid)


@pytest.mark.skipif(os.name == "nt", reason="Windows lock compatibility is exercised in CI")
def test_run_lock_compatible_with_legacy_runstore(store):
    legacy = RunStore(store.run_dir)
    with legacy.lock():
        with pytest.raises(StorageBusyError):
            with store.lock(False):
                pass
    with legacy.assemble_lock():
        with pytest.raises(StorageBusyError):
            with store._file_lock(".assemble.lock", blocking=False):
                pass


def test_threads_and_instances_serialize_state_and_event_appends(store):
    def work(index):
        other = store if index % 2 else SqliteStorage(store.run_dir, create=False)
        try:
            for _ in range(5):
                with other.state_lock():
                    value = other.load_context() or {"count": 0}
                    other.save_context({"count": value["count"] + 1})
                other.log_event("thread", worker=index)
        finally:
            if other is not store:
                other.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(work, range(8)))
    assert store.load_context() == {"count": 40}
    assert len(store.list_events(limit=0)) == 40
    assert store._connection().execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store._connection().execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert store._connection().execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_compact_chapter_statistics_track_atomic_edits(store, document, monkeypatch):
    document.chapters[0].segments.extend(
        [
            Segment(index=10, source="", target="ignored"),
            Segment(index=11, source="Heading", kind="heading", target="ignored"),
            Segment(index=12, source=" ", target=""),
        ]
    )
    manifest = store.init_from_document(document)
    manifest["chapters"][0]["title_translated"] = "第一章"
    store.save_manifest(manifest)
    chapter = store.load_chapter(3)
    chapter.segments[0].target = "First translation"
    chapter.meta["review"] = {"manual": True}
    with pytest.raises(RuntimeError):
        with store.state_lock():
            store.save_chapter_with_status(chapter, "done")
            raise RuntimeError("rollback summary too")
    assert store.chapter_statistics()[0]["target_word_count"] == 2
    store.save_chapter_with_status(chapter, "done")
    monkeypatch.setattr(store, "load_chapter", lambda _: pytest.fail("loaded chapter body"))
    assert store.chapter_statistics() == [
        {
            "index": 3,
            "title": "Chapter",
            "title_translated": "第一章",
            "status": "done",
            "review_status": "pending",
            "word_count": 3,
            "target_word_count": 3,
            "meta": chapter.meta,
        }
    ]


def test_segment_revisions_manual_polish_and_rollback(store, document):
    store.init_from_document(document)
    assert store.load_segment_history(3, 7) == []
    assert store.load_segment_history(3, 9)[0]["after"] == ""
    chapter = store.load_chapter(3)
    chapter.segments[0].target_before_polish = "draft"
    chapter.segments[0].target = "polished"
    store.save_chapter(chapter)
    history = store.load_segment_history(3, 7)
    assert [(row["kind"], row["before"], row["after"]) for row in history] == [
        ("polish", "draft", "polished"),
        ("translation", None, "draft"),
    ]
    assert all(row["created_at"] and row["id"].startswith("revision-") for row in history)
    store.save_chapter(chapter)
    assert store.load_segment_history(3, 7) == history
    chapter.segments[0].target = ""
    with pytest.raises(RuntimeError):
        with store.state_lock():
            store.save_chapter(chapter, revision_kind="manual")
            raise RuntimeError("discard edit and revision")
    assert store.load_segment_history(3, 7) == history
    store.save_chapter(chapter, revision_kind="manual")
    store.set_chapter_review_status(3, "completed")
    store.log_event(
        "manual_translation_edited",
        chapter=3,
        index=7,
        before="polished",
        after="",
        history_recorded=True,
    )
    assert len(store.load_segment_history(3, 7)) == 3
    assert store.load_segment_history(3, 7)[0]["kind"] == "manual"
    assert store.chapter_statistics()[0]["review_status"] == "completed"
    assert store.load_chapter(3) == chapter
    with pytest.raises(KeyError, match="segment not found"):
        store.load_segment_history(3, 888)
    store.close()
    assert len(store.load_segment_history(3, 7)) == 3


def test_glossary_explicit_edit_preserves_order_and_rolls_back(store):
    store.upsert_term(GlossaryTerm("first", "one"), chapter=3)
    store.upsert_term(GlossaryTerm("second", "two"))
    store.upsert_term(GlossaryTerm("first", "conflict"))
    with pytest.raises(RuntimeError):
        with store.state_lock():
            store.update_term("first", GlossaryTerm("renamed", "edited"))
            raise RuntimeError("rollback edit")
    assert store.get_term("renamed") is None
    assert len(store.open_conflicts()) == 1
    assert store.update_term("first", GlossaryTerm("renamed", "edited", aliases=["alias"]))
    assert [term.source for term in store.all_terms()] == ["renamed", "second"]
    assert store.get_term("renamed").first_chapter == 3
    assert store.get_term("renamed").status == "ok"
    assert store.open_conflicts() == []
    assert not store.update_term("missing", GlossaryTerm("missing", "none"))


def test_review_store_resumes_checkpoints_and_chunks_without_files(store, document):
    store.init_from_document(document)
    review = ReviewRunStore(store.run_dir, storage=store)
    review.start(reviewed_content_digest="digest", metadata={"config": {"rounds": 2}})
    review.mark_chunk_done("r1-ch3-base0-n2", {"initial_issues": [{"index": 0, "note": "check"}]})
    review.save_checkpoint({"round": 1, "phase": "scan_done"})
    with review.round_scope(1):
        review.write_json("shadow.json", {"targets": ["影子", ""]})
    review.mark_interrupted()
    store.close()
    resumed = ReviewRunStore.find_resumable(
        store.run_dir, content_digest="digest", config={"rounds": 2}, storage=store
    )
    assert resumed is not None
    assert resumed.review_id == review.review_id
    assert resumed.load_checkpoint() == {"round": 1, "phase": "scan_done"}
    assert resumed.load_chunk_result("r1-ch3-base0-n2") == {
        "initial_issues": [{"index": 0, "note": "check"}]
    }
    resumed.rebuild_snapshots_from_chunks(1)
    assert resumed.result_snapshots()[0][0]["note"] == "check"
    with resumed.round_scope(1):
        assert resumed.load_json("shadow.json") == {"targets": ["影子", ""]}
    resumed.log_event("continued")
    events = store.read_artifact_records(f"reviews/{review.review_id}/events.jsonl")
    assert [row["seq"] for row in events] == list(range(1, len(events) + 1))
    assert store.load_chapter(3) == document.chapters[0]
    assert not Path(store.reviews_dir).exists()


def _process_writes(run_dir, worker):
    store = SqliteStorage(run_dir, create=False)
    try:
        for sequence in range(10):
            with store.state_lock():
                counter = store.load_context() or {"count": 0}
                store.save_context({"count": counter["count"] + 1})
                store.log_event("process", worker=worker, sequence=sequence)
    finally:
        store.close()


def _process_abandon_transaction(run_dir):
    store = SqliteStorage(run_dir, create=False)
    with store.state_lock():
        store.save_context({"uncommitted": True})
        store.upsert_term(GlossaryTerm("uncommitted", "discard"))
        os._exit(0)


def test_process_writers_and_crash_recovery(store):
    context = multiprocessing.get_context("spawn")
    children = [
        context.Process(target=_process_writes, args=(store.run_dir, worker)) for worker in range(2)
    ]
    for child in children:
        child.start()
    for child in children:
        child.join(timeout=15)
        assert child.exitcode == 0
    assert store.load_context() == {"count": 20}
    events = store.list_events(limit=0)
    for worker in range(2):
        assert [row["sequence"] for row in events if row["worker"] == worker] == list(range(10))
    child = context.Process(target=_process_abandon_transaction, args=(store.run_dir,))
    child.start()
    child.join(timeout=15)
    assert child.exitcode == 0
    assert store.load_context() == {"count": 20}
    assert store.get_term("uncommitted") is None
    store.save_context({"after_crash": True})


def test_legacy_json_is_not_imported(tmp_path):
    root = tmp_path / "legacy"
    root.mkdir()
    legacy = root / "manifest.json"
    legacy.write_text('{"title": "legacy"}', encoding="utf-8")
    readonly = SqliteStorage(str(root), create=False)
    assert not readonly.exists()
    assert sorted(path.name for path in root.iterdir()) == ["manifest.json"]
    storage = SqliteStorage(str(root))
    try:
        assert not storage.exists()
        assert legacy.read_text(encoding="utf-8") == '{"title": "legacy"}'
    finally:
        storage.close()


@pytest.mark.parametrize("boundary", ["index", "chapter"])
def test_autofix_publication_recovers_without_changing_manifest(
    store, document, tmp_path, monkeypatch, boundary
):
    document.chapters[0].segments = [Segment(index=0, source="Source.", target="Before.")]
    manifest = store.init_from_document(document)
    debug = ReviewRunStore(store.run_dir, storage=store)
    debug.start(reviewed_content_digest="baseline", metadata={})
    result = debug.finish(
        status="completed",
        termination="clean_confirmed",
        summary={"issue_count": 0, "change_count": 1, "review_round_count": 1},
        issues=[],
        changes=[{"chapter": 3, "index": 0, "suggested_target": "After."}],
    )
    debug.write_json("rounds/final/unresolved_issues.json", [])
    outcome = ReviewOutcome(run_dir=debug.run_dir, result=result, usage={})
    config = Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {"preset": "fake"},
            "pipeline": {"review_autofix": True, "review_concurrency": 1},
            "output": {"punctuation_normalize": False},
            "paths": {"state_dir": str(tmp_path / "unused")},
        }
    )
    first = Orchestrator(config, client=FakeClient())._review_autofix
    original_save = store.save_chapter
    original_write = ReviewRunStore.write_json

    def save(chapter):
        original_save(chapter)
        if boundary == "chapter":
            raise KeyboardInterrupt("publication boundary")

    def write(review, name, data):
        path = original_write(review, name, data)
        if boundary == "index" and name == "autofix/index.json":
            raise KeyboardInterrupt("publication boundary")
        return path

    with monkeypatch.context() as patcher:
        patcher.setattr(store, "save_chapter", save)
        patcher.setattr(ReviewRunStore, "write_json", write)
        with pytest.raises(KeyboardInterrupt, match="publication boundary"):
            first.run(store, outcome, [])
    store.close()
    client = FakeClient()
    resumed = Orchestrator(config, client=client)._review_autofix
    assert resumed.resume_pending(store) is not None
    assert store.load_chapter(3).segments[0].target == "After."
    assert store.load_manifest() == manifest
    usage = store.load_usage()
    assert resumed.resume_pending(store) is None
    assert store.load_usage() == usage
    assert client.calls == []
    assert not Path(store.reviews_dir).exists()


@pytest.mark.parametrize("cleared", ["", None])
def test_manifest_title_edit_reload_and_export_snapshot(store, document, cleared):
    document.chapters[0].meta["title_translated"] = "Old title"
    manifest = store.init_from_document(document)
    manifest["chapters"][0]["title_translated"] = "Manual title"
    store.save_manifest(manifest)
    store.close()
    assert store.load_chapter(3).meta["title_translated"] == "Manual title"
    snapshot = store.create_export_snapshot(actual_sha256=manifest["source_sha256"])
    assert snapshot.load_chapter(3).meta["title_translated"] == "Manual title"
    assert store.chapter_statistics()[0]["title_translated"] == "Manual title"
    manifest["chapters"][0]["title_translated"] = cleared
    store.save_manifest(manifest)
    store.close()
    assert "title_translated" not in store.load_chapter(3).meta
    cleared_snapshot = store.create_export_snapshot(actual_sha256=manifest["source_sha256"])
    assert "title_translated" not in cleared_snapshot.load_chapter(3).meta
    assert store.chapter_statistics()[0]["title_translated"] is None
    assert snapshot.load_chapter(3).meta["title_translated"] == "Manual title"
