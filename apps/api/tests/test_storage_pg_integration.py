"""Storage contracts plus actual PostgreSQL transaction and concurrency checks.

Set WENYI_TEST_DATABASE_URL to an isolated PostgreSQL service. Every module run
creates and drops its own schema; no existing project rows are touched.
"""

from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path
from typing import Any, cast

import psycopg
import pytest
from psycopg import sql
from psycopg_pool import ConnectionPool
from type_helpers import must
from wenyi_api.storage_pg import PostgresStorage, ProjectBusyError
from wenyi_core.glossary.store import GlossaryTerm
from wenyi_core.ingest.models import Chapter, Document, Segment
from wenyi_core.llm.usage import empty_usage
from wenyi_core.pipeline.runstore import source_sha256
from wenyi_core.storage.file import FileStorage


@pytest.fixture(scope="module")
def pg_pool():
    dsn = os.environ.get("WENYI_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("Set WENYI_TEST_DATABASE_URL for real PostgreSQL storage tests")
    schema = "test_wenyi_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public")
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    pool = ConnectionPool(
        dsn,
        min_size=1,
        max_size=8,
        open=True,
        kwargs={"options": f"-c search_path={schema},public", "client_encoding": "UTF8"},
    )
    pool.wait()
    try:
        schema_sql = Path(__file__).parents[1] / "wenyi_api" / "db" / "schema.sql"
        with pool.connection() as conn:
            conn.execute(cast(Any, schema_sql.read_text(encoding="utf-8")))
        yield pool
    finally:
        pool.close()
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def pg_storage(pg_pool, tmp_path):
    pid = uuid.uuid4().hex
    with pg_pool.connection() as conn:
        conn.execute(
            "INSERT INTO projects(id,name,source_lang,target_lang) VALUES(%s,'test','en','zh')",
            (pid,),
        )
    return PostgresStorage(pid, pg_pool, run_dir=str(tmp_path / "resources"))


@pytest.fixture(params=["file", "postgres"])
def storage(request, tmp_path):
    if request.param == "postgres":
        result = request.getfixturevalue("pg_storage")
    else:
        result = FileStorage(str(tmp_path / "file-state"))
    yield result
    result.close()


def document(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("Book\nOriginal paragraph", encoding="utf-8")
    return Document(
        title="Book",
        fmt="text",
        source_lang="en",
        target_lang="zh",
        source_path=str(source),
        meta={
            "format_metadata": {"nested": [1, 2]},
            "epub_annotation_contexts": {"note": {"source": "Original note"}},
        },
        chapters=[
            Chapter(
                index=0,
                title="Chapter",
                href="chapter.xhtml",
                template="<p id='a'></p>",
                meta={"toc_entry_id": "toc-a"},
                segments=[
                    Segment(
                        index=0,
                        source="Original paragraph",
                        target="润色译文",
                        target_before_polish="原始译文",
                        anchor="a",
                        resource_href="chapter.xhtml",
                        meta={"style": {"bold": True}},
                    )
                ],
            )
        ],
    )


def initialize(storage, tmp_path):
    doc = document(tmp_path)
    digest = source_sha256(doc.source_path)
    storage.begin_initialization(digest)
    manifest = storage.stage_document(doc, source_hash=digest)
    storage.save_manifest(manifest)
    storage.finish_initialization()
    return doc, digest


def test_initialization_marker_and_complete_metadata_round_trip(storage, tmp_path):
    doc = document(tmp_path)
    digest = source_sha256(doc.source_path)
    assert not storage.exists()
    storage.begin_initialization(digest)
    manifest = storage.stage_document(doc, source_hash=digest)
    assert not storage.exists(), "Staged chapters must not mark interrupted preparation complete"
    manifest["custom_checkpoint"] = {"phase": 2}
    manifest["chapters"][0]["custom_field"] = "retain"
    storage.save_manifest(manifest)
    storage.finish_initialization()
    assert storage.exists()
    loaded = storage.load_manifest()
    assert loaded["custom_checkpoint"] == {"phase": 2}
    assert loaded["chapters"][0]["toc_entry_id"] == "toc-a"
    assert loaded["chapters"][0]["custom_field"] == "retain"
    assert loaded["meta"] == {"format_metadata": {"nested": [1, 2]}}
    assert storage.load_chapter(0).model_dump() == doc.chapters[0].model_dump()
    assert storage.load_annotation_contexts() == {"note": {"source": "Original note"}}
    assert storage.ensure_source_identity(doc.source_path) == digest
    with pytest.raises(ValueError):
        storage.ensure_source_identity(doc.source_path, actual_sha256="0" * 64)


def test_atomic_chapter_and_export_snapshot(storage, tmp_path):
    doc, digest = initialize(storage, tmp_path)
    before = storage.create_export_snapshot(actual_sha256=digest)
    changed = doc.chapters[0].model_copy(deep=True)
    changed.segments[0].target = "新译文"
    storage.save_chapter_with_status(changed, "done")
    after = storage.create_export_snapshot(actual_sha256=digest)
    assert before.load_chapter(0).segments[0].target == "润色译文"
    assert before.load_manifest()["chapters"][0]["status"] == "pending"
    assert after.load_chapter(0).segments[0].target == "新译文"
    assert after.load_manifest()["chapters"][0]["status"] == "done"
    copy = after.load_chapter(0)
    copy.segments[0].target = "corrupted copy"
    assert after.load_chapter(0).segments[0].target == "新译文"


def test_glossary_order_conflict_and_batch_checkpoint(storage, tmp_path):
    initialize(storage, tmp_path)
    storage.upsert_term(GlossaryTerm(source="Zed", target="泽德"))
    storage.upsert_term(GlossaryTerm(source="Amy", target="艾米"))
    assert [term.source for term in storage.all_terms()] == ["Zed", "Amy"]
    assert storage.upsert_term(GlossaryTerm(source="Zed", target="另一译名")) == "conflict"
    assert storage.get_term("Zed").target == "泽德"
    assert storage.resolve_term("Zed", "确认译名")
    storage.mark_conflicts_resolved("Zed")
    assert storage.open_conflicts() == []
    assert [term.source for term in storage.all_terms()] == ["Zed", "Amy"]
    storage.log_event("batch_glossary_extracted", chapter=0, start_index=2, count=4)
    storage.log_event("batch_glossary_extracted", chapter=1, start_index=0, count=2)
    assert storage.completed_batch_glossary_keys(0) == {"2:4"}


def test_review_artifacts_and_timing_are_durable_and_idempotent(storage, tmp_path):
    initialize(storage, tmp_path)
    key = "reviews/review-2026/result.json"
    result = {"status": "interrupted", "issues": [{"chapter": 0, "index": 0}]}
    storage.write_artifact(key, result)
    storage.write_artifact("reviews/review-2026/checkpoint.json", {"round": 2})
    storage.append_artifact_record("reviews/review-2026/events.jsonl", {"seq": 1, "event": "start"})
    storage.append_artifact_record("reviews/review-2026/events.jsonl", {"seq": 2, "event": "pause"})
    assert storage.load_latest_review_result() == result
    assert len(storage.read_artifact_records("reviews/review-2026/events.jsonl")) == 2
    assert key in storage.list_artifacts("reviews/")
    record = {"id": "run-a", "elapsed_seconds": 3, "status": "completed"}
    storage.record_timing(record)
    ledger = storage.record_timing(record)
    assert ledger["total_seconds"] == 3
    assert len(ledger["runs"]) == 1


def test_usage_commit_recovery_does_not_double_charge(storage, tmp_path):
    initialize(storage, tmp_path)
    ledger = empty_usage()
    key = "reviews/review-2026/usage.json"
    storage.prepare_usage_commit({"usage.json": ledger, key: ledger})
    storage.recover_usage()
    storage.recover_usage()
    assert storage.load_usage() == ledger
    assert storage.read_artifact(key) == ledger
    assert storage.read_artifact("usage-pending.json") is None


def test_postgres_initialization_rollback_and_project_isolation(pg_storage, pg_pool, tmp_path):
    doc = document(tmp_path)
    digest = source_sha256(doc.source_path)
    pg_storage.begin_initialization(digest)
    pg_storage.stage_document(doc, source_hash=digest)
    pg_storage.upsert_term(GlossaryTerm(source="stale", target="旧"))
    pg_storage.begin_initialization(digest)
    assert not pg_storage.exists()
    assert pg_storage.all_terms() == []
    assert pg_storage.load_manifest()["chapters"] == []
    other_pid = uuid.uuid4().hex
    with pg_pool.connection() as conn:
        conn.execute("INSERT INTO projects(id,name) VALUES(%s,'other')", (other_pid,))
    other = PostgresStorage(other_pid, pg_pool, run_dir=str(tmp_path / "other"))
    pg_storage.write_artifact("reviews/review-a/result.json", {"status": "done"})
    assert other.list_artifacts() == []
    assert not Path(pg_storage.run_dir).exists(), (
        "DB writes must not create a shadow state directory"
    )


def test_postgres_lock_is_session_scoped_and_nonblocking(pg_storage, pg_pool):
    other = PostgresStorage(pg_storage.project_id, pg_pool)
    with pg_storage.lock():
        with pg_storage.lock():  # Same-thread nested workflow calls are reentrant.
            with pytest.raises(ProjectBusyError):
                with other.lock(blocking=False):
                    pytest.fail("Concurrent writer entered the project")
        with pg_pool.connection() as conn:
            rows = conn.execute(
                """SELECT a.xact_start FROM pg_stat_activity a JOIN pg_locks l
                ON a.pid=l.pid WHERE l.locktype='advisory' AND l.objid=(%s::bigint & 4294967295)::oid
                AND l.granted""",
                (pg_storage._lock_key("write"),),
            ).fetchall()
        assert rows and all(row[0] is None for row in rows), "LLM wait must not hold a transaction"
    with other.lock(blocking=False):
        pass


def test_postgres_export_is_one_snapshot_during_concurrent_publish(
    pg_storage, pg_pool, tmp_path, monkeypatch
):
    doc, digest = initialize(pg_storage, tmp_path)
    writer = PostgresStorage(pg_storage.project_id, pg_pool)
    ready = threading.Event()
    published = threading.Event()
    errors = []
    original = pg_storage.load_chapter

    def delayed_load(ci):
        ready.set()
        assert published.wait(5), "Writer should commit without blocking on export rendering"
        return original(ci)

    def publish():
        try:
            assert ready.wait(5)
            chapter = doc.chapters[0].model_copy(deep=True)
            chapter.segments[0].target = "concurrent translation"
            writer.save_chapter_with_status(chapter, "done")
        except BaseException as error:
            errors.append(error)
        finally:
            published.set()

    monkeypatch.setattr(pg_storage, "load_chapter", delayed_load)
    thread = threading.Thread(target=publish)
    thread.start()
    snapshot = pg_storage.create_export_snapshot(actual_sha256=digest)
    thread.join(5)
    assert not errors
    assert snapshot.load_manifest()["chapters"][0]["status"] == "pending"
    assert snapshot.load_chapter(0).segments[0].target == "润色译文"
    assert writer.load_chapter(0).segments[0].target == "concurrent translation"


def test_postgres_state_transaction_rolls_back_complete_chapter(pg_storage, tmp_path):
    doc, _ = initialize(pg_storage, tmp_path)
    changed = doc.chapters[0].model_copy(deep=True)
    changed.segments[0].target = "should roll back"
    with pytest.raises(RuntimeError):
        with pg_storage.state_lock():
            pg_storage.save_chapter_with_status(changed, "done")
            raise RuntimeError("injected publication failure")
    assert pg_storage.load_chapter(0).segments[0].target == "润色译文"
    assert pg_storage.load_manifest()["chapters"][0]["status"] == "pending"


def test_postgres_complete_workflow_review_reuse_and_export(pg_storage, tmp_path):
    from tests.fake_llm import MeteredFakeClient, routing_handler
    from wenyi_core.config import Config
    from wenyi_core.pipeline.orchestrator import Orchestrator

    doc = document(tmp_path)
    config = Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {"preset": "fake"},
            "pipeline": {
                "book_understanding": True,
                "polish": True,
                "review": True,
                "review_autofix": True,
            },
            "paths": {"state_dir": str(tmp_path / "unused-cli-state")},
        }
    )
    client = MeteredFakeClient(handler=routing_handler)
    orch = Orchestrator(config, client=client, storage=pg_storage)
    prepared = orch.prepare_for_translation(doc.source_path)
    assert prepared is pg_storage and prepared.exists()
    translated = orch.run(doc.source_path)
    assert translated is pg_storage
    assert pg_storage.pending_chapters() == []
    assert any(
        segment.target_before_polish
        for entry in pg_storage.load_manifest()["chapters"]
        for segment in pg_storage.load_chapter(entry["index"]).segments
    )
    first = orch.run_review(doc.source_path)
    reviewer_calls = len([call for call in client.calls if call["operation"].startswith("review.")])
    second = orch.run_review(doc.source_path)
    assert second["review_dir"] == first["review_dir"]
    assert (
        len([call for call in client.calls if call["operation"].startswith("review.")])
        == reviewer_calls
    )
    assert pg_storage.load_latest_review_result()["status"] == "completed"
    assert pg_storage.load_usage()["totals"]["calls"] > 0
    outcome = orch.run_steps(
        doc.source_path,
        {"report", "assemble"},
        out_format="txt",
        out_path=str(tmp_path / "translated.txt"),
    )
    assert outcome["report"] is not None
    assert all(Path(path).is_file() for path in outcome["outputs"])
    assert pg_storage.read_artifact("timing.json")["total_seconds"] > 0
    assert not list((tmp_path / "resources").rglob("*.json"))
    assert not list((tmp_path / "resources").rglob("*.db"))


def test_postgres_subtitle_translation_resume_and_dual_output(pg_storage, tmp_path):
    import json

    from wenyi_core.config import Config
    from wenyi_core.llm.providers.fake import FakeClient
    from wenyi_core.srt.store import SrtRunStore
    from wenyi_core.srt.translate import translate_srt

    source = tmp_path / "clip.srt"
    source.write_text(
        "1\n00:00:00,100 --> 00:00:01,200\nHello\n\n2\n00:00:01,300 --> 00:00:02,400\nGoodbye\n",
        encoding="utf-8",
    )
    config = Config.from_dict(
        {"language": {"source": "en", "target": "zh"}, "llm": {"preset": "fake"}}
    )
    client = FakeClient(
        handler=lambda *_: json.dumps({"1": "你好", "2": "再见"}, ensure_ascii=False)
    )
    first = translate_srt(
        str(source),
        config,
        client=client,
        storage=pg_storage,
        out=str(tmp_path / "translated.srt"),
        mono=True,
        bilingual=True,
    )
    call_count = len(client.calls)
    second = translate_srt(
        str(source),
        config,
        client=client,
        storage=pg_storage,
        out=str(tmp_path / "translated-again.srt"),
        mono=True,
        bilingual=True,
    )
    assert len(client.calls) == call_count
    assert first["cue_count"] == second["translated"] == 2
    cues = SrtRunStore(pg_storage.run_dir, storage=pg_storage).load_cues()
    assert cues["1"]["timestamp"] == "00:00:00,100 --> 00:00:01,200"
    assert cues["2"]["target"] == "再见"
    assert pg_storage.load_manifest()["fmt"] == "srt"
    assert pg_storage.exists()
    for path in first["outputs"]:
        content = Path(path).read_text(encoding="utf-8")
        assert "00:00:01,300 --> 00:00:02,400" in content
        assert "再见" in content
    assert not list((tmp_path / "resources").rglob("*.json"))
    assert not list((tmp_path / "resources").rglob("*.jsonl"))


def test_postgres_review_interrupt_restores_same_run(pg_storage, tmp_path, monkeypatch):
    from tests.fake_llm import MeteredFakeClient, routing_handler
    from wenyi_core.config import Config
    from wenyi_core.pipeline.orchestrator import Orchestrator

    doc = document(tmp_path)
    config = Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {"preset": "fake"},
            "pipeline": {"review_autofix": False},
        }
    )
    first = Orchestrator(
        config, client=MeteredFakeClient(handler=routing_handler), storage=pg_storage
    )
    first.run(doc.source_path)
    with monkeypatch.context() as patcher:
        patcher.setattr(
            first._review._chunks,
            "review_chapter",
            lambda *_a, **_kw: (_ for _ in ()).throw(KeyboardInterrupt("pause")),
        )
        with pytest.raises(KeyboardInterrupt):
            first.run_review(doc.source_path)
    interrupted = pg_storage.load_latest_review_result()
    assert interrupted["status"] == "interrupted"
    second = Orchestrator(
        config, client=MeteredFakeClient(handler=routing_handler), storage=pg_storage
    )
    result = second.run_review(doc.source_path)
    assert result["review_result"]["review_id"] == interrupted["review_id"]
    assert result["review_result"]["status"] == "completed"
    usage = pg_storage.load_usage()
    second.run_review(doc.source_path)
    assert pg_storage.load_usage() == usage


@pytest.mark.parametrize("external_edit", [False, True])
def test_postgres_autofix_publication_resume_protects_manual_edit(
    pg_storage, tmp_path, monkeypatch, external_edit
):
    from wenyi_core.config import Config
    from wenyi_core.llm.providers.fake import FakeClient
    from wenyi_core.pipeline.orchestrator import Orchestrator
    from wenyi_core.review.models import ReviewOutcome
    from wenyi_core.review.run_store import ReviewRunStore

    initialize(pg_storage, tmp_path)
    debug = ReviewRunStore(pg_storage.run_dir, storage=pg_storage)
    debug.start(
        reviewed_content_digest="baseline", metadata={"config": {}, "glossary_fingerprint": "g"}
    )
    changes = [{"chapter": 0, "index": 0, "suggested_target": "发布修订译文"}]
    result = debug.finish(
        status="completed",
        termination="clean_confirmed",
        summary={"issue_count": 0, "change_count": 1, "review_round_count": 1},
        issues=[],
        changes=changes,
    )
    outcome = ReviewOutcome(run_dir=debug.run_dir, result=result, usage={})
    config = Config.from_dict(
        {
            "language": {"source": "en", "target": "zh"},
            "llm": {"preset": "fake"},
            "pipeline": {"review_autofix": True},
        }
    )
    original = ReviewRunStore.write_json

    def stop_after_index(self, name, data):
        original(self, name, data)
        if name == "autofix/index.json":
            raise KeyboardInterrupt("publication index persisted")

    with monkeypatch.context() as patcher:
        patcher.setattr(ReviewRunStore, "write_json", stop_after_index)
        with pytest.raises(KeyboardInterrupt):
            Orchestrator(config, client=FakeClient(), storage=pg_storage)._review_autofix.run(
                pg_storage, outcome, []
            )
    if external_edit:
        chapter = pg_storage.load_chapter(0)
        chapter.segments[0].target = "人工修改"
        pg_storage.save_chapter(chapter)
    client = FakeClient()
    resumed = Orchestrator(config, client=client, storage=pg_storage)._review_autofix
    result = resumed.resume_pending(pg_storage)
    assert result is not None
    assert pg_storage.load_chapter(0).segments[0].target == (
        "人工修改" if external_edit else "发布修订译文"
    )
    assert result.result["autofix"]["status"] == ("partial" if external_edit else "completed")
    assert resumed.resume_pending(pg_storage) is None
    assert client.calls == []
    assert not list((tmp_path / "resources").rglob("*.json"))


def test_postgres_subtitle_pause_persists_usage_and_manual_edit(pg_storage, tmp_path):
    import json

    from tests.fake_llm import MeteredFakeClient
    from wenyi_core.config import Config
    from wenyi_core.srt.store import SrtRunStore
    from wenyi_core.srt.translate import translate_srt

    source = tmp_path / "pause.srt"
    source.write_text(
        "1\n00:00:00,100 --> 00:00:01,200\nHello\n\n2\n00:00:01,300 --> 00:00:02,400\nGoodbye\n",
        encoding="utf-8",
    )
    config = Config.from_dict(
        {"language": {"source": "en", "target": "zh"}, "llm": {"preset": "fake"}}
    )
    client = MeteredFakeClient(
        handler=lambda *_: json.dumps({"1": "你好", "2": "再见"}, ensure_ascii=False)
    )

    def pause(current, *_):
        if current > 0:
            raise KeyboardInterrupt("safe pause")

    with pytest.raises(KeyboardInterrupt):
        translate_srt(
            str(source),
            config,
            client=client,
            storage=pg_storage,
            progress=pause,
            out=str(tmp_path / "pause-output.srt"),
        )
    store = SrtRunStore(pg_storage.run_dir, storage=pg_storage)
    assert store.load_manifest()["status"] == "interrupted"
    usage = pg_storage.load_usage()
    assert usage["totals"]["calls"] > 0
    cues = store.load_cues()
    assert cues["1"]["target"]
    cues["1"]["target"] = "用户调整"
    store.save_cues(cues)
    translate_srt(
        str(source),
        config,
        client=client,
        storage=pg_storage,
        out=str(tmp_path / "resume-output.srt"),
    )
    assert pg_storage.load_usage() == usage
    assert store.load_cues()["1"]["target"] == "用户调整"


def test_postgres_dal_config_job_identity_errors_and_review_summary(
    pg_storage, pg_pool, tmp_path, monkeypatch
):
    from wenyi_api import dal

    monkeypatch.setattr(dal, "get_pool", lambda: pg_pool)
    pid = pg_storage.project_id
    doc, digest = initialize(pg_storage, tmp_path)
    config = {"pipeline": {"review_autofix": False}}
    dal.set_project_config(pid, config)
    dal.set_project_source(
        pid, doc.source_path, "中文书名", source_sha256=digest, source_meta={"parsed": True}
    )
    assert dal.get_project_config(pid) == config
    project = must(dal.get_project(pid))
    assert project["initialized"] and project["source_sha256"] == digest
    assert project["source_meta"] == {"parsed": True}
    dal.set_project_status(pid, "error", error="Service failed")
    assert must(dal.get_project(pid))["error"] == "Service failed"
    dal.set_project_status(pid, "reviewing")
    assert must(dal.get_project(pid))["error"] is None
    job_id = dal.create_job(
        pid, "review", "queue-1", params={"chapter": 0}, config_snapshot=config, run_id="run-1"
    )
    assert must(dal.get_job_by_arq_id("queue-1"))["id"] == job_id
    assert must(dal.get_job(job_id))["params"]["config_snapshot"] == config
    dal.set_job_status(job_id, "paused")
    assert must(dal.latest_resumable_job(pid))["run_id"] == "run-1"
    later = dal.create_job(pid, "review", "queue-2", run_id="run-2")
    dal.set_job_status(later, "done")
    assert dal.latest_resumable_job(pid) is None
    assert dal.chapter_summaries(pid)[0]["review_status"] == "pending"
    pg_storage.write_artifact(
        "reviews/review-2026/result.json",
        {"status": "completed", "issues": [{"chapter": 0, "index": 0}]},
    )
    summary = dal.chapter_summaries(pid)[0]
    assert summary["review_status"] == "completed" and summary["review_issue_count"] == 1


def test_postgres_initialization_preserves_only_matching_preview(pg_storage, pg_pool, tmp_path):
    doc = document(tmp_path)
    digest = source_sha256(doc.source_path)
    with pg_pool.connection() as conn:
        conn.execute(
            "UPDATE projects SET source_sha256=%s WHERE id=%s", (digest, pg_storage.project_id)
        )
    pg_storage.write_artifact(
        "parsed_document.json", {"source_sha256": digest, "document": doc.model_dump()}
    )
    pg_storage.write_artifact("preview.json", {"title": "Book"})
    pg_storage.write_artifact("reviews/review-obsolete/result.json", {"status": "failed"})
    pg_storage.begin_initialization(digest)
    assert pg_storage.read_artifact("parsed_document.json")["source_sha256"] == digest
    assert pg_storage.read_artifact("preview.json") == {"title": "Book"}
    assert set(pg_storage.list_artifacts()) == {"parsed_document.json", "preview.json"}
    assert pg_storage.read_artifact("reviews/review-obsolete/result.json") is None
