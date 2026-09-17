"""New Web API contracts exercised against actual PostgreSQL persistence."""

from __future__ import annotations

import pytest
import test_storage_pg_integration as storage_tests
from fastapi import FastAPI
from fastapi.testclient import TestClient
from type_helpers import must
from wenyi_api import dal, project_service
from wenyi_api.routers import chapters, glossary, report, review, style, subtitles
from wenyi_core.glossary.store import GlossaryTerm
from wenyi_core.ingest.models import Chapter, Segment
from wenyi_core.srt.store import SrtRunStore

pg_pool = storage_tests.pg_pool
pg_storage = storage_tests.pg_storage
initialize = storage_tests.initialize


@pytest.fixture
def domain_client(pg_storage, pg_pool, monkeypatch):
    monkeypatch.setattr(dal, "get_pool", lambda: pg_pool)
    monkeypatch.setattr(project_service, "storage_for", lambda pid: pg_storage)
    for module in (chapters, glossary, report, review, style, subtitles):
        monkeypatch.setattr(module, "storage_for", lambda pid: pg_storage)
    monkeypatch.setattr(glossary, "get_pool", lambda: pg_pool)
    queued = []

    async def start(pid, kind, *, params=None):
        queued.append({"pid": pid, "kind": kind, "params": params})
        return {"job_id": "queued-task", "project_id": pid, "kind": kind}

    monkeypatch.setattr(chapters, "start_job", start)
    monkeypatch.setattr(review, "start_job", start)
    app = FastAPI()
    for module in (chapters, glossary, report, review, style, subtitles):
        app.include_router(module.router)
    with TestClient(app) as client:
        yield client, pg_storage, queued


def test_review_run_listing_and_sparse_segment_mapping(domain_client, tmp_path):
    client, storage, queued = domain_client
    initialize(storage, tmp_path)
    storage.save_chapter(
        Chapter(
            index=0,
            title="chapter",
            segments=[
                Segment(index=0, source="", target=None),
                Segment(index=12, source="one", target="一"),
                Segment(index=18, source="two", target="二"),
            ],
        )
    )
    storage.write_artifact(
        "reviews/review-2026/result.json",
        {
            "review_id": "review-2026",
            "status": "completed",
            "started_at": "2026-09-15T01:00:00Z",
            "summary": {"issue_count": 1},
            "issues": [{"chapter": 0, "index": 1, "type": "missing"}],
            "changes": [],
        },
    )
    storage.write_artifact(
        "reviews/review-2026/autofix/index.json", {"records": [{"status": "failed"}]}
    )
    root = f"/projects/{storage.project_id}"
    run = client.get(root + "/review/runs").json()[0]
    assert run["id"] == "review-2026" and run["autofix"]["records"][0]["status"] == "failed"
    detail = client.get(root + "/review/runs/review-2026").json()
    assert detail["result"]["status"] == "completed"
    assert detail["items"][0]["location"]["segment_index"] == 18
    assert detail["items"][0]["location"]["text_index"] == 1
    assert detail["items"][0]["location"]["source"] == "two"
    assert detail["items"][0]["status"] == "pending"
    assert detail["items"][1]["status"] == "failed"
    assert client.get(root + "/review/runs/missing").status_code == 404
    chapter = client.get(root + "/chapters/0").json()
    assert chapter["review_issues"][0]["index"] == 18
    assert chapter["review_issues"][0]["text_position"] == 1
    assert client.post(root + "/review/run", json={"force": True}).status_code == 422
    assert client.post(root + "/review/run", json={}).status_code == 409
    storage.set_chapter_status(0, "done")
    assert client.post(root + "/review/run", json={"autofix": False}).status_code == 200
    assert queued[-1] == {"pid": storage.project_id, "kind": "review", "params": {"autofix": False}}


def test_human_translation_and_style_writes_obey_busy_guard(domain_client, tmp_path):
    client, storage, _ = domain_client
    initialize(storage, tmp_path)
    root = f"/projects/{storage.project_id}"
    dal.set_project_status(storage.project_id, "translating")
    for method, path, body in [
        ("put", "/review/0/segments/0", {"target": "blocked", "expected_target": "润色译文"}),
        ("post", "/review/0/complete", {}),
        ("put", "/analysis", {"analysis": {"style_guide": "blocked"}}),
        ("put", "/chapter-digests/0", {"digest": "blocked"}),
        ("post", "/glossary/terms", {"source": "name", "target": "名字"}),
    ]:
        assert client.request(method, root + path, json=body).status_code == 409
    assert storage.load_chapter(0).segments[0].target == "润色译文"
    dal.set_project_status(storage.project_id, "done")
    assert (
        client.put(
            root + "/review/0/segments/0",
            json={"target": "人工修订", "expected_target": "润色译文"},
        ).status_code
        == 200
    )
    assert storage.load_chapter(0).segments[0].target_before_polish == "原始译文"
    assert client.post(root + "/review/0/complete").status_code == 200
    assert storage.load_chapter(0).meta["review_passed"]
    assert (
        client.put(root + "/analysis", json={"analysis": {"style_guide": "自然"}}).status_code
        == 200
    )
    assert client.put(root + "/chapter-digests/0", json={"digest": "摘要"}).status_code == 200
    payload = client.get(root + "/analysis").json()
    assert payload["analysis"]["style_guide"] == "自然"
    assert payload["chapter_digests"][0]["digest"] == "摘要"


def test_partial_chapter_is_readable_after_each_saved_batch(domain_client, tmp_path):
    client, storage, _ = domain_client
    initialize(storage, tmp_path)
    chapter = Chapter(
        index=0,
        title="Partial chapter",
        segments=[
            Segment(index=12, source="First paragraph", target=None),
            Segment(index=18, source="Next paragraph", target=None),
        ],
    )
    storage.save_chapter_with_status(chapter, "translating")
    dal.set_project_status(storage.project_id, "translating")
    root = f"/projects/{storage.project_id}"
    assert client.get(root + "/chapters").json()[0]["target_word_count"] == 0
    chapter.segments[0].target = "First saved translation"
    with storage.lock():
        storage.save_chapter(chapter)
        response = client.get(root + "/review/0")
        assert response.status_code == 200
        assert [s["target"] for s in response.json()["segments"]] == [
            "First saved translation",
            None,
        ]
        summary = client.get(root + "/chapters").json()[0]
        assert summary["status"] == "translating"
        assert summary["target_word_count"] == 1
    dal.set_project_status(storage.project_id, "paused")
    assert client.post(root + "/review/0/complete").status_code == 409
    assert (
        client.put(
            root + "/review/0/segments/12",
            json={"target": "Human edit", "expected_target": "First saved translation"},
        ).status_code
        == 200
    )
    assert storage.load_chapter(0).segments[1].target is None


def test_saved_empty_translation_counts_as_complete_for_proofreading(domain_client, tmp_path):
    client, storage, _ = domain_client
    initialize(storage, tmp_path)
    storage.save_chapter_with_status(
        Chapter(index=0, segments=[Segment(index=12, source="Parser noise", target="")]),
        "done",
    )
    root = f"/projects/{storage.project_id}"
    summary = client.get(root + "/chapters").json()[0]
    assert summary["word_count"] == summary["target_word_count"] == 1
    assert client.post(root + "/review/0/complete").status_code == 200


def test_glossary_edit_keeps_order_and_conflicts_resolve(domain_client, tmp_path):
    client, storage, _ = domain_client
    initialize(storage, tmp_path)
    root = f"/projects/{storage.project_id}/glossary"
    assert client.post(root + "/terms", json={"source": "Zed", "target": "泽德"}).status_code == 201
    assert client.post(root + "/terms", json={"source": "Amy", "target": "艾米"}).status_code == 201
    assert (
        client.put(
            root + "/terms/Zed", json={"source": "Zed", "target": "泽德二", "type": "person"}
        ).status_code
        == 200
    )
    assert [term.source for term in storage.all_terms()] == ["Zed", "Amy"]
    assert (
        client.post(root + "/terms", json={"source": "Zed", "target": "different"}).status_code
        == 409
    )
    assert client.post(root + "/terms/Zed/lock").status_code == 404
    storage.upsert_term(GlossaryTerm(source="Zed", target="泽德三"))
    conflict = client.get(root + "/conflicts").json()[0]
    assert (
        client.post(
            root + f"/conflicts/{conflict['id']}/resolve", json={"decision": "custom"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            root + f"/conflicts/{conflict['id']}/resolve", json={"decision": "proposed"}
        ).status_code
        == 200
    )
    assert storage.get_term("Zed").target == "泽德三"
    assert storage.open_conflicts() == []
    exported = client.get(root + "/export?format=csv")
    assert exported.status_code == 200
    assert "confidence" not in exported.text and "locked" not in exported.text
    assert (
        client.post(root + "/import", json={"terms": [{"source": "Zed", "target": "冲突"}]}).json()[
            "conflicts"
        ]
        == 1
    )
    assert storage.get_term("Zed").target == "泽德三"


def test_subtitle_routes_preserve_timeline_and_overlapping_cache(domain_client, tmp_path):
    client, storage, _ = domain_client
    source = tmp_path / "clip.srt"
    source.write_text("1\n00:00:00,100 --> 00:00:01,200\nHello\n", encoding="utf-8")
    store = SrtRunStore(storage.run_dir, storage=storage)
    store.ensure_manifest(str(source), cue_count=1, source_lang="en", target_lang="zh")
    store.ensure_cues([("1", "00:00:00,100 --> 00:00:01,200", "Hello")])
    store.save_batch(0, {"1": "缓存一"})
    store.save_batch(10, {"1": "缓存二"})
    root = f"/projects/{storage.project_id}"
    cue = client.get(root + "/subtitles").json()["cues"][0]
    assert cue["id"] == "1" and cue["start"] == "00:00:00,100"
    assert cue["end"] == "00:00:01,200"
    assert client.put(root + "/subtitles/1", json={"target": "人工字幕"}).status_code == 200
    assert must(store.load_batch(0))["1"] == must(store.load_batch(10))["1"] == "人工字幕"
    assert client.get(root + "/subtitles").json()["completed"] == 1
    for path in ("/chapters", "/review/runs", "/glossary/terms", "/analysis"):
        assert client.get(root + path).status_code == 422
    assert client.post(root + "/review/run", json={}).status_code == 422
    assert client.get(root + "/report").json()["summary"]["cues_done"] == 1
    dal.set_project_status(storage.project_id, "translating")
    assert client.put(root + "/subtitles/1", json={"target": "blocked"}).status_code == 409


def test_report_counts_unreviewed_and_preserves_usage(domain_client, tmp_path):
    client, storage, _ = domain_client
    initialize(storage, tmp_path)
    root = f"/projects/{storage.project_id}"
    before = client.get(root + "/report").json()
    assert "review" not in before
    assert before["summary"]["chapters_done"] == 0
    assert client.get(root + "/chapters").json()[0]["review_status"] == "pending"
    storage.record_timing({"id": "run", "elapsed_seconds": 3})
    response = client.post(root + "/report")
    assert response.status_code == 200
    assert response.json()["timing"]["total_seconds"] == 3
    assert storage.load_report() == response.json()


def test_review_status_follows_edit_manual_completion_and_new_ai_review(domain_client, tmp_path):
    from datetime import datetime, timedelta, timezone

    client, storage, _ = domain_client
    initialize(storage, tmp_path)
    root = f"/projects/{storage.project_id}"
    old = {
        "status": "completed",
        "finished_at": "2020-01-01T00:00:00Z",
        "issues": [{"chapter": 0, "index": 0, "type": "missing"}],
    }
    storage.write_artifact("reviews/review-2020/result.json", old)
    assert client.get(root + "/chapters").json()[0]["review_status"] == "completed"
    assert client.get(root + "/chapters/0").json()["review_issues"]
    assert (
        client.put(
            root + "/review/0/segments/0",
            json={"target": "编辑后待复核", "expected_target": "润色译文"},
        ).status_code
        == 200
    )
    summary = client.get(root + "/chapters").json()[0]
    assert summary["review_status"] == "pending" and summary["review_issue_count"] == 0
    assert client.get(root + "/chapters/0").json()["review_issues"] == []
    assert client.get(root + "/review/runs/review-2020").json()["issues"], (
        "History remains inspectable"
    )
    assert client.post(root + "/review/0/complete").status_code == 200
    summary = client.get(root + "/chapters").json()[0]
    assert summary["review_status"] == "completed" and summary["review_issue_count"] == 0
    assert (
        client.put(
            root + "/review/0/segments/0",
            json={"target": "再次编辑", "expected_target": "编辑后待复核"},
        ).status_code
        == 200
    )
    assert client.get(root + "/chapters").json()[0]["review_status"] == "pending"
    storage.write_artifact(
        "reviews/review-2099/result.json",
        {**old, "finished_at": (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()},
    )
    summary = client.get(root + "/chapters").json()[0]
    assert summary["review_status"] == "completed" and summary["review_issue_count"] == 1
    assert client.get(root + "/chapters/0").json()["review_issues"][0]["index"] == 0


def test_subtitle_preview_visible_before_first_translation(domain_client, pg_pool):
    client, storage, _ = domain_client
    with pg_pool.connection() as conn:
        conn.execute("UPDATE projects SET fmt='srt' WHERE id=%s", (storage.project_id,))
    storage.write_artifact(
        "subtitle_preview.json",
        [{"index": "1", "timestamp": "00:00:00,100 --> 00:00:01,200", "source": "Hello"}],
    )
    root = f"/projects/{storage.project_id}"
    response = client.get(root + "/subtitles")
    assert response.status_code == 200
    assert response.json()["total"] == 1 and response.json()["completed"] == 0
    assert response.json()["cues"][0]["source"] == "Hello"
    assert client.put(root + "/subtitles/1", json={"target": "not initialized"}).status_code == 404


def test_workflow_review_association_is_scoped_to_job_interval(pg_storage, pg_pool, monkeypatch):
    monkeypatch.setattr(dal, "get_pool", lambda: pg_pool)
    pid = pg_storage.project_id
    pg_storage.log_event("review_started", review_id="before-the-job")
    first = dal.create_job(pid, "review", "first-review")
    assert dal.job_review_id(first) is None
    pg_storage.log_event("review_started", review_id="review-a")
    assert dal.job_review_id(first) == "review-a"

    # An export neither steals the review nor terminates the review task's interval.
    dal.create_job(pid, "export", "export-between-reviews")
    pg_storage.log_event("review_autofix_finished", review_id="review-a")
    assert dal.job_review_id(first) == "review-a"
    second = dal.create_job(pid, "review", "second-review")
    assert dal.job_review_id(second) is None
    pg_storage.log_event("review_started", review_id="review-b")
    assert dal.job_review_id(second) == "review-b"
    assert dal.job_review_id(first) == "review-a"

    with pg_pool.connection() as conn:
        conn.execute("INSERT INTO projects(id,name) VALUES ('other-review-project','other')")
        conn.execute(
            """INSERT INTO events(project_id,type,payload)
               VALUES ('other-review-project','review_started','{"review_id":"other"}')"""
        )
    assert dal.job_review_id(second) == "review-b"
    # Resuming may reuse the review directory but still belongs to the new job.
    resumed = dal.create_job(pid, "review", "resumed-review")
    pg_storage.log_event("review_started", review_id="review-b")
    assert dal.job_review_id(resumed) == "review-b"
