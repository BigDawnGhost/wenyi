"""Offline HTTP integration against the Desktop catalog, workers and domain SQLite."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.fake_llm import MeteredFakeClient, routing_handler
from wenyi_backend import dal
from wenyi_backend.context import use_context
from wenyi_core.ingest.models import Chapter, Document, Segment
from wenyi_core.storage.sqlite import SqliteStorage
from wenyi_desktop.main import create_context


@pytest.fixture
def desktop(tmp_path, monkeypatch):
    from wenyi_desktop import main

    fake = MeteredFakeClient(routing_handler)
    context = replace(
        create_context(tmp_path / "desktop", api_token="offline-test-token"),
        build_client=lambda config: fake,
    )
    backend = context.repository
    try:
        with (
            use_context(context),
            TestClient(
                main.create_app(context=context),
                headers={"Authorization": "Bearer offline-test-token"},
            ) as client,
        ):
            yield client, backend, fake
    finally:
        backend.close()


def wait_job(run_id, *, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = dal.get_job_by_arq_id(run_id)
        if job and job["status"] not in {"queued", "running"}:
            return job
        time.sleep(0.01)
    pytest.fail(f"Local task did not finish within {timeout}s: {job}")


def upload(client, *, name="book.txt", content=b"A short paragraph.\n"):
    response = client.post(
        "/projects",
        data={"project": json.dumps({"name": "Offline", "source_lang": "en", "target_lang": "zh"})},
        files={"file": (name, content, "application/octet-stream")},
    )
    assert response.status_code == 201, response.text
    pid = response.json()["id"]
    job = wait_job(dal.list_jobs(pid)[0]["run_id"])
    assert job["status"] == "done", job
    return pid


def initialize(backend):
    pid = dal.create_project("Offline", "en", "zh", {"template": "标准翻译"})
    source = backend.project_dir(pid) / "source.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("A short paragraph.\n", encoding="utf-8")
    store = backend.storage_for(pid)
    assert isinstance(store, SqliteStorage)
    doc = Document(
        title="Offline",
        source_path=str(source),
        fmt="txt",
        source_lang="en",
        target_lang="zh",
        chapters=[
            Chapter(index=0, title="Chapter", segments=[Segment(index=0, source="A paragraph.")])
        ],
    )
    store.init_from_document(doc)
    chapter = store.load_chapter(0)
    chapter.segments[0].target = "译文"
    store.save_chapter_with_status(chapter, "done")
    dal.set_project_source(
        pid,
        source_path=str(source),
        book_title="Offline",
        source_sha256=store.load_manifest()["source_sha256"],
        fmt="txt",
    )
    dal.set_project_status(pid, "done")
    return pid, store


def test_events_http_and_review_job_identity(desktop, monkeypatch):
    client, backend, _ = desktop
    pid, store = initialize(backend)
    from wenyi_desktop import local_backend

    def job(at, run, kind="review"):
        monkeypatch.setattr(local_backend, "_now", lambda: at)
        return backend.create_job(pid, kind, run, run_id=run)

    old = job("2025-01-01T00:00:00.100000+00:00", "old")
    store.append_artifact_record(
        "events.jsonl",
        {"event": "review_started", "ts": "2025-01-01T08:00:00+08:00", "review_id": "old"},
    )
    current = job("2025-01-01T00:00:02.100000+00:00", "current")
    job("2025-01-01T00:00:02.150000+00:00", "export", "export")
    assert backend.job_review_id(old) == "old"
    assert backend.job_review_id(current) is None
    store.append_artifact_record(
        "events.jsonl",
        {
            "event": "review_started",
            "ts": "2025-01-01T08:00:02.200000+08:00",
            "review_id": "current",
        },
    )
    assert backend.job_review_id(current) == "current"
    assert backend.job_review_id(old) == "old"
    response = client.get(f"/projects/{pid}/events", params={"type": "review_started", "limit": 1})
    assert response.status_code == 200, response.text
    event = response.json()[0]
    assert event["id"] == store.list_events(limit=1)[0]["_id"]
    assert event["created_at"] == "2025-01-01T08:00:02.200000+08:00"
    assert event["payload"]["review_id"] == "current"
    assert client.get(f"/projects/{pid}/workflow").json()["review_id"] == "current"
    next_run = job("2025-01-01T00:00:02.300000+00:00", "next")
    # A truncated timestamp cannot distinguish the two runs within this second.
    store.append_artifact_record(
        "events.jsonl",
        {"event": "review_started", "ts": "2025-01-01T00:00:02+00:00", "review_id": "ambiguous"},
    )
    assert backend.job_review_id(current) == "current"
    assert backend.job_review_id(next_run) is None
    assert client.get(f"/projects/{pid}/workflow").json()["review_id"] is None
    store.append_artifact_record(
        "events.jsonl",
        {
            "event": "review_autofix_finished",
            "ts": "2025-01-01T00:00:02.300000+00:00",
            "review_id": "next",
        },
    )
    assert backend.job_review_id(current) == "current"
    assert backend.job_review_id(next_run) == "next"
    assert client.get(f"/projects/{pid}/workflow").json()["review_id"] == "next"
    other_pid, other_store = initialize(backend)
    other_job = backend.create_job(other_pid, "review", "other", run_id="other")
    other_store.log_event("review_started", review_id="other")
    other_store.close()
    assert backend.job_review_id(other_job) == "other"
    assert backend.job_review_id(next_run) == "next"
    records = store.read_artifact_records("events.jsonl")
    before = client.get(f"/projects/{pid}/events").json()
    store.close()
    assert client.get(f"/projects/{pid}/events").json() == before
    assert all(row["payload"]["review_id"] != "other" for row in before)
    assert store.read_artifact_records("events.jsonl") == records
    store.close()


def test_upload_parse_preview_and_workflow_use_sqlite(desktop):
    client, backend, _ = desktop
    pid = upload(client)
    preview = client.get(f"/projects/{pid}/preview")
    assert preview.status_code == 200, preview.text
    assert preview.json()["total_word_count"] == 1
    store = backend.storage_for(pid)
    assert isinstance(store, SqliteStorage)
    assert store.read_artifact("preview.json") == preview.json()
    workflow = client.get(f"/projects/{pid}/workflow")
    assert workflow.status_code == 200, workflow.text
    assert backend.database.is_file()
    assert not list(backend.workspace.rglob("preview.json"))
    assert not list(backend.workspace.rglob("manifest.json"))
    store.close()


def test_desktop_does_not_use_or_modify_cli_state(desktop, tmp_path, monkeypatch):
    from wenyi_core.config import Config

    client, backend, _ = desktop
    cli = tmp_path / "cli"
    cli.mkdir()
    monkeypatch.chdir(cli)
    (cli / "config.yaml").write_text("not: desktop configuration", encoding="utf-8")
    (cli / "state").mkdir()
    sentinel = cli / "state" / "manifest.json"
    sentinel.write_text('{"owner": "cli"}', encoding="utf-8")
    before = {path.relative_to(cli): path.read_bytes() for path in cli.rglob("*") if path.is_file()}
    pid = upload(client)
    assert Path(backend.storage_for(pid).run_dir).is_relative_to(backend.workspace)
    assert Config().state_dir == "state"
    after = {path.relative_to(cli): path.read_bytes() for path in cli.rglob("*") if path.is_file()}
    assert after == before


@pytest.mark.parametrize("route", ["chapters/0", "review/0"])
def test_chapter_content_routes_use_shared_review_policy(desktop, route):
    client, backend, _ = desktop
    pid, store = initialize(backend)
    try:
        response = client.get(f"/projects/{pid}/{route}")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["index"] == 0
        assert payload["segments"][0]["target"] == "译文"
        assert payload["review_issues"] == []
    finally:
        store.close()


def test_chapter_edits_history_glossary_and_review_are_project_scoped(desktop):
    client, backend, _ = desktop
    pid, store = initialize(backend)
    other, other_store = initialize(backend)
    root = f"/projects/{pid}"
    assert client.get(root + "/chapters").json()[0]["status"] == "done"
    title = {"title_translated": "人工标题", "expected_title_translated": None}
    assert client.put(root + "/chapters/0/title", json=title).status_code == 200
    assert client.put(root + "/chapters/0/title", json=title).status_code == 409
    segment = root + "/review/0/segments/0"
    body = {"target": "人工译文", "expected_target": "译文"}
    assert client.put(segment, json=body).status_code == 200
    history = client.get(segment + "/history").json()
    assert history[0]["kind"] == "manual"
    assert history[0]["after"] == "人工译文"
    assert client.put(segment, json=body).status_code == 409
    assert client.get(segment + "/history").json() == history
    assert other_store.load_chapter(0).segments[0].target == "译文"
    assert store.load_chapter(0).meta["title_translated"] == "人工标题"
    glossary = root + "/glossary/terms"
    assert client.post(glossary, json={"source": "Amy", "target": "艾米"}).status_code == 201
    response = client.put(glossary + "/Amy", json={"source": "Alice", "target": "爱丽丝"})
    assert response.status_code == 200, response.text
    assert store.get_term("Amy") is None
    assert store.get_term("Alice").target == "爱丽丝"
    assert client.get(f"/projects/{other}/glossary/terms").json() == []
    assert client.delete(glossary + "/Alice").status_code == 200
    store.write_artifact(
        "reviews/review-offline/result.json",
        {"status": "completed", "issues": [{"chapter": 0, "index": 0, "type": "missing"}]},
    )
    assert client.get(root + "/review/runs").json()[0]["id"] == "review-offline"
    assert client.get(root + "/review/runs/review-offline").status_code == 200
    assert client.get(f"/projects/{other}/review/runs/review-offline").status_code == 404
    store.close()
    other_store.close()


def test_snapshot_export_download_and_ownership(desktop):
    client, backend, _ = desktop
    pid, store = initialize(backend)
    other, other_store = initialize(backend)
    snapshot = store.create_export_snapshot(actual_sha256=store.load_manifest()["source_sha256"])
    chapter = store.load_chapter(0)
    chapter.segments[0].target = "后续编辑"
    store.save_chapter(chapter)
    assert snapshot.load_chapter(0).segments[0].target == "译文"
    response = client.post(f"/projects/{pid}/exports", json={"format": "txt", "about_page": False})
    assert response.status_code == 200, response.text
    job = wait_job(response.json()["job_id"])
    assert job["status"] == "done", job
    eid = response.json()["export_id"]
    download = client.get(f"/projects/{pid}/exports/{eid}/download")
    assert download.status_code == 200, download.text
    assert "后续编辑" in download.text
    assert client.get(f"/projects/{other}/exports/{eid}/download").status_code == 404
    assert client.get(f"/projects/{other}/exports").json() == []
    store.close()
    other_store.close()


def test_fake_book_translation_commits_domain_state(desktop):
    client, backend, fake = desktop
    pid = upload(client)
    response = client.put(
        f"/projects/{pid}/config",
        json={
            "yaml": json.dumps(
                {"pipeline": {"polish": False, "review": False, "book_understanding": False}}
            )
        },
    )
    assert response.status_code == 200, response.text
    response = client.post(f"/projects/{pid}/translate")
    assert response.status_code == 200, response.text
    job = wait_job(response.json()["job_id"])
    assert job["status"] == "done", job
    store = backend.storage_for(pid)
    assert store.exists()
    assert store.load_chapter(0).text_segments[0].target == "译0"
    assert store.pending_chapters() == []
    assert fake.calls
    assert not list(Path(store.run_dir).rglob("manifest.json"))
    store.close()


def test_fake_srt_translation_edit_and_export(desktop):
    client, backend, fake = desktop

    def subtitles(messages, tier, json_mode):
        return json.dumps({"1": "字幕"}, ensure_ascii=False) if json_mode else "字幕"

    fake.handler = subtitles
    pid = upload(client, name="clip.srt", content=b"1\n00:00:00,100 --> 00:00:01,200\nHello\n")
    response = client.post(f"/projects/{pid}/translate")
    assert response.status_code == 200, response.text
    job = wait_job(response.json()["job_id"])
    assert job["status"] == "done", job
    root = f"/projects/{pid}"
    cues = client.get(root + "/subtitles").json()
    assert cues["completed"] == 1
    assert client.put(root + "/subtitles/1", json={"target": "人工字幕"}).status_code == 200
    response = client.post(root + "/exports", json={"format": "srt"})
    assert response.status_code == 200, response.text
    assert wait_job(response.json()["job_id"])["status"] == "done"
    text = client.get(root + f"/exports/{response.json()['export_id']}/download").text
    assert "00:00:00,100 --> 00:00:01,200" in text
    assert "人工字幕" in text
    assert all(call["operation"] == "srt.translate" for call in fake.calls)
    assert not list(backend.workspace.rglob("manifest.json"))


def test_shutdown_waits_for_saved_translation_checkpoint(desktop, monkeypatch):
    import asyncio
    import threading

    client, backend, fake = desktop
    pid = upload(client)
    response = client.put(
        f"/projects/{pid}/config",
        json={
            "yaml": json.dumps(
                {"pipeline": {"polish": False, "review": False, "book_understanding": False}}
            )
        },
    )
    assert response.status_code == 200, response.text
    saved, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    original = SqliteStorage.save_chapter
    cancel = fake.cancel

    def observe_cancel():
        cancel()
        cancelled.set()

    monkeypatch.setattr(fake, "cancel", observe_cancel)

    def checkpoint(store, chapter):
        original(store, chapter)
        if any(segment.target is not None for segment in chapter.text_segments):
            saved.set()
            assert release.wait(5), "Test did not release the committed batch boundary"

    monkeypatch.setattr(SqliteStorage, "save_chapter", checkpoint)
    response = client.post(f"/projects/{pid}/translate")
    assert response.status_code == 200, response.text
    try:
        assert saved.wait(5), "Translation did not commit its first batch"
        runtime = client.app.state.backend.telemetry.runtime
        stopped = client.portal.start_task_soon(runtime.stop)
        client.portal.call(asyncio.wait_for, runtime._stopping.wait(), 3)
        assert cancelled.wait(3), "Shutdown did not request cooperative client cancellation"
        assert not stopped.done(), "Shutdown must wait for the active checkpoint boundary"
    finally:
        release.set()
    stopped.result(timeout=5)
    job = wait_job(response.json()["job_id"])
    assert job["status"] == "paused", job
    assert dal.get_project(pid)["status"] == "paused"
    store = backend.storage_for(pid)
    assert store.load_chapter(0).text_segments[0].target == "译0"
    assert store.list_events(event_type="task_paused")
    store.close()


def test_two_apps_isolate_same_project_requests_workers_and_thread_context(tmp_path, monkeypatch):
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import ExitStack
    from threading import Barrier

    from wenyi_backend.config_documents import config_document
    from wenyi_backend.context import current_context
    from wenyi_backend.global_settings import load_settings, save_settings
    from wenyi_backend.workers import tasks
    from wenyi_desktop.local_credentials import CredentialStore, credential_store
    from wenyi_desktop.main import create_app

    contexts, clients, lifespans = [], [], []
    pid, run_id = "same-project", "same-run"
    entered = Barrier(2)
    parse = tasks._parse_source

    def parse_with_owner_check(project_id, storage, config, progress):
        context = current_context()
        owner = contexts.index(context)
        assert context.repository.get_project(project_id)["name"] == f"Owner {owner}"
        assert config.pipeline.polish is bool(owner)
        providers = load_settings().config.llm.providers
        name = next(iter(providers))
        assert credential_store().snapshot(providers)[name] == f"fake-owner-{owner}"
        assert Path(storage.run_dir).is_relative_to(context.repository.workspace)
        entered.wait(timeout=5)
        return parse(project_id, storage, config, progress)

    monkeypatch.setattr(tasks, "_parse_source", parse_with_owner_check)
    with ExitStack() as stack:
        for owner in range(2):
            workspace = tmp_path / str(owner)
            workspace.mkdir()
            context = create_context(
                workspace,
                api_token=f"fake-token-{owner}",
                credentials=CredentialStore(workspace),
            )
            contexts.append(context)
            stack.callback(context.repository.close)
            with use_context(context):
                settings = load_settings()
                document = config_document(settings.config)
                document["pipeline"]["polish"] = bool(owner)
                save_settings(json.dumps(document), settings.default_template, settings.revision)
                name = next(iter(settings.config.llm.providers))
                context.settings_store.credentials.update(
                    name, mode="manual", storage="session", secret=f"fake-owner-{owner}"
                )
                backend = context.repository
                backend.create_project(f"Owner {owner}", "en", "zh", {}, project_id=pid)
                source = backend.project_dir(pid) / "source.txt"
                source.parent.mkdir(parents=True)
                source.write_text(f"Distinct source for owner {owner}.", encoding="utf-8")
                backend.set_project_source(pid, str(source), f"Owner {owner}", fmt="txt")
            lifespan = stack.enter_context(ExitStack())
            lifespans.append(lifespan)
            client = lifespan.enter_context(
                TestClient(
                    create_app(context=context),
                    headers={"Authorization": f"Bearer fake-token-{owner}"},
                )
            )
            clients.append(client)
            backend.create_job(pid, "parse", run_id, run_id=run_id)

        def requests(owner):
            client = clients[owner]
            for _ in range(3):
                assert client.get(f"/projects/{pid}").json()["name"] == f"Owner {owner}"
                assert client.get("/settings").json()["effective"]["pipeline"]["polish"] is bool(
                    owner
                )
                response = client.get("/desktop/credentials")
                assert response.status_code == 200
                assert all(item["available"] for item in response.json().values())
                assert (
                    client.get(
                        "/desktop/credentials",
                        headers={"Authorization": f"Bearer fake-token-{1 - owner}"},
                    ).status_code
                    == 401
                )

        async def workers():
            # Deliberately bind the wrong ambient owner for one worker: ctx must win.
            with use_context(contexts[0]):
                await asyncio.gather(
                    *(
                        tasks.run_parse({"backend": context}, project_id=pid, run_id=run_id)
                        for context in contexts
                    )
                )
                assert current_context() is contexts[0]

        with ThreadPoolExecutor(max_workers=3) as pool:
            pending = [pool.submit(requests, owner) for owner in range(2)]
            pending.append(pool.submit(asyncio.run, workers()))
            for future in pending:
                future.result(timeout=15)

        for owner, context in enumerate(contexts):
            assert context.repository.get_job_by_arq_id(run_id)["status"] == "done"
            store = context.repository.storage_for(pid, create=False)
            try:
                document = store.read_artifact("parsed_document.json")["document"]
                assert document["chapters"][0]["segments"][0]["source"] == (
                    f"Distinct source for owner {owner}."
                )
                assert store.list_events(event_type="task_completed")
            finally:
                store.close()
        # Closing one app must not clear another application's in-memory keys or stop its runner.
        lifespans[0].close()
        assert not contexts[0].settings_store.credentials._session
        assert contexts[1].settings_store.credentials._session
        assert clients[1].get(f"/projects/{pid}").json()["name"] == "Owner 1"
        assert contexts[1].telemetry.runtime.accepting
