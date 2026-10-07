"""Precision draft inspection uses real temporary archives and the public HTTP contract."""

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from tests.fake_llm import MeteredFakeClient
from tests.precision_fixtures import (
    PrecisionHandler,
    precision_config,
    precision_plan,
    precision_store,
)
from wenyi_api import main
from wenyi_backend.routers import chapters
from wenyi_core.pipeline.precision import PrecisionBatchExecutor
from wenyi_core.storage.precision_archive import PrecisionArchive


@pytest.fixture
def draft_api(monkeypatch, tmp_path):
    store, config = precision_store(tmp_path), precision_config(tmp_path)
    client = MeteredFakeClient(handler=PrecisionHandler())
    result = PrecisionBatchExecutor(client, config).execute(precision_plan(store), store)
    chapter = store.load_chapter(0)
    for segment, target, before in zip(chapter.text_segments, result.targets, result.before_polish):
        segment.target, segment.target_before_polish = target, before
    store.save_chapter(chapter)
    PrecisionBatchExecutor.mark_published(store, result)
    project = {"id": "draft-test", "fmt": "text", "initialized": store.exists()}
    monkeypatch.setattr(main, "settings", replace(main.settings, api_token=None))
    monkeypatch.setattr(chapters, "require_project", lambda _: project)
    monkeypatch.setattr(chapters, "read_storage_for", lambda _: store)
    http = TestClient(main.create_app())
    yield http, store, result, client, project, tmp_path
    http.close()


def test_http_drafts_use_stable_identity_and_do_not_modify_archives(draft_api):
    http, store, result, client, _, tmp_path = draft_api
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    response = http.get("/projects/draft-test/chapters/0/segments/11/precision-drafts")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["chapter_index"] == 0 and data["segment_index"] == 11
    assert data["available"] is True and data["reason"] is None
    assert data["before_polish_candidate"] == "T1"
    assert data["synthesized_target"] == result.targets[1]
    archive = PrecisionArchive(store)
    expected = []
    for name in ("T1", "T2", "T3"):
        payload = store.read_artifact(f"{result.precision_key}/drafts/{name}.json")["payload"]
        expected.append({"id": name, "target": archive.get(payload["targets_ref"])[1]})
    assert data["candidates"] == expected
    assert len(client.calls) == 4
    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_http_drafts_preserve_original_synthesis_after_manual_edit(draft_api):
    http, store, result, _, _, _ = draft_api
    chapter = store.load_chapter(0)
    chapter.segments[1].target = "A manually revised translation."
    store.save_chapter(chapter)
    response = http.get("/projects/draft-test/chapters/0/segments/11/precision-drafts")
    assert response.status_code == 200, response.text
    assert response.json()["available"] is True
    assert response.json()["synthesized_target"] == result.targets[1]
    assert store.load_chapter(0).segments[1].target == "A manually revised translation."


@pytest.mark.parametrize("ci,si", [(0, 1), (1, 11)])
def test_http_drafts_missing_chapter_or_stable_segment_is_404(draft_api, ci, si):
    http, _, _, _, _, _ = draft_api
    assert (
        http.get(f"/projects/draft-test/chapters/{ci}/segments/{si}/precision-drafts").status_code
        == 404
    )


def test_http_drafts_reject_subtitles(draft_api):
    http, _, _, _, project, _ = draft_api
    project["fmt"] = "srt"
    assert (
        http.get("/projects/draft-test/chapters/0/segments/11/precision-drafts").status_code == 422
    )


def test_http_drafts_report_missing_candidate_without_fabricating_it(draft_api):
    http, store, result, _, _, _ = draft_api
    store.delete_artifact(f"{result.precision_key}/drafts/T2.json")
    response = http.get("/projects/draft-test/chapters/0/segments/11/precision-drafts")
    assert response.status_code == 200, response.text
    assert response.json()["available"] is False
    assert response.json()["reason"] == "incomplete"
    assert response.json()["candidates"] == []


def test_http_drafts_require_api_authentication(draft_api, monkeypatch):
    monkeypatch.setattr(main, "settings", replace(main.settings, api_token="test-token"))
    authenticated = TestClient(main.create_app())
    try:
        assert (
            authenticated.get(
                "/projects/draft-test/chapters/0/segments/11/precision-drafts"
            ).status_code
            == 401
        )
    finally:
        authenticated.close()
