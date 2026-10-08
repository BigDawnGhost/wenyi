"""Precision and built-in language policies use the real Desktop HTTP/SQLite stack."""

import json

from desktop_test_support import wait_job
from tests.precision_fixtures import PrecisionHandler
from wenyi_backend import dal
from wenyi_backend.config_documents import config_document
from wenyi_core.config import Config
from wenyi_core.pipeline.language_policies import CHECKPOINT


def test_precision_creation_translation_drafts_edit_and_export(desktop):
    client, backend, fake = desktop
    handler = PrecisionHandler()
    fake.handler = handler
    created = client.post(
        "/projects",
        data={
            "project": json.dumps(
                {
                    "name": "Precision",
                    "source_lang": "en",
                    "target_lang": "zh",
                    "translation_mode": "best_of_three",
                }
            )
        },
        files={"file": ("book.txt", b"The traveler crossed the bridge.\n")},
    )
    assert created.status_code == 201, created.text
    pid = created.json()["id"]
    root = f"/projects/{pid}"
    assert wait_job(dal.list_jobs(pid)[0]["run_id"])["status"] == "done"
    configured = client.put(
        root + "/config",
        json={"yaml": "pipeline: {review: false, book_understanding: false}"},
    )
    assert configured.status_code == 200, configured.text
    pipeline = configured.json()["effective"]["pipeline"]
    assert pipeline["translation_mode"] == "best_of_three"
    assert pipeline["polish"] is True
    translated = client.post(root + "/translate")
    assert translated.status_code == 200, translated.text
    job = wait_job(translated.json()["job_id"])
    assert job["status"] == "done", job
    assert handler.counts == {"translate": 3, "synthesis": 1}
    detail = client.get(root + "/chapters/0").json()
    segment = detail["segments"][0]
    draft_path = root + f"/chapters/0/segments/{segment['index']}/precision-drafts"
    response = client.get(draft_path)
    assert response.status_code == 200, response.text
    drafts = response.json()
    assert drafts["available"] is True
    assert [candidate["id"] for candidate in drafts["candidates"]] == ["T1", "T2", "T3"]
    assert drafts["synthesized_target"] == segment["target"]
    assert segment["target_before_polish"] == drafts["candidates"][0]["target"]
    assert drafts["before_polish_candidate"] == "T1"

    store = backend.storage_for(pid)
    try:
        checkpoint = store.read_artifact(CHECKPOINT)
        assert checkpoint
        assert all(store.read_artifact(key)["schema_version"] == 1 for key in checkpoint.values())
        archived = {key: store.read_artifact(key) for key in store.list_artifacts("precision/")}
    finally:
        store.close()
    calls = len(fake.calls)
    resumed = client.post(root + "/translate")
    assert resumed.status_code == 200, resumed.text
    job = wait_job(resumed.json()["job_id"])
    assert job["status"] == "done", job
    assert len(fake.calls) == calls
    assert client.get(draft_path).json() == drafts
    edited = client.put(
        root + f"/review/0/segments/{segment['index']}",
        json={"target": "人工校订译文", "expected_target": segment["target"]},
    )
    assert edited.status_code == 200, edited.text
    assert client.get(draft_path).json() == drafts
    assert client.get(root + "/chapters/0").json()["segments"][0]["target"] == "人工校订译文"
    exported = client.post(root + "/exports", json={"format": "txt"})
    assert exported.status_code == 200, exported.text
    job = wait_job(exported.json()["job_id"])
    assert job["status"] == "done", job
    assert (
        "人工校订译文"
        in client.get(root + f"/exports/{exported.json()['export_id']}/download").text
    )
    assert len(fake.calls) == calls
    reopened = backend.storage_for(pid)
    try:
        assert reopened.read_artifact(CHECKPOINT) == checkpoint
        assert {
            key: reopened.read_artifact(key) for key in reopened.list_artifacts("precision/")
        } == archived
        plans = [
            reopened.read_artifact(key)
            for key in reopened.list_artifacts("language-policies/")
            if key != CHECKPOINT
        ]
        assert any(plan["context"]["phase"] == "export" for plan in plans)
    finally:
        reopened.close()


def test_precision_keeps_protected_source_when_model_changes_spacing(desktop):
    client, backend, fake = desktop
    source = "10\u2005\u20059\u2005\u20058"
    handler = PrecisionHandler()

    def normalize_spaces(messages, tier, json_mode):
        response = handler(messages, tier, json_mode)
        if "Task (JSON):\n" not in messages[-1]["content"]:
            return response
        data = json.loads(response)
        data["translations"][1] = "10 9 8"
        return json.dumps(data)

    fake.handler = normalize_spaces
    created = client.post(
        "/projects",
        data={
            "project": json.dumps(
                {
                    "name": "Protected spacing",
                    "source_lang": "en",
                    "target_lang": "zh",
                    "translation_mode": "best_of_three",
                }
            )
        },
        files={"file": ("book.txt", f"The traveler crossed the bridge.\n\n{source}\n".encode())},
    )
    assert created.status_code == 201, created.text
    pid = created.json()["id"]
    root = f"/projects/{pid}"
    assert wait_job(dal.list_jobs(pid)[0]["run_id"])["status"] == "done"
    configured = client.put(
        root + "/config",
        json={"yaml": "pipeline: {review: false, book_understanding: false}"},
    )
    assert configured.status_code == 200, configured.text
    translated = client.post(root + "/translate")
    assert translated.status_code == 200, translated.text
    job = wait_job(translated.json()["job_id"])
    assert job["status"] == "done", job
    assert handler.counts == {"translate": 3, "synthesis": 1}
    segments = client.get(root + "/chapters/0").json()["segments"]
    assert segments[1]["source"] == source
    assert segments[1]["target"] == source
    assert segments[1]["target_before_polish"] == source
    calls = len(fake.calls)
    resumed = client.post(root + "/translate")
    assert resumed.status_code == 200, resumed.text
    assert wait_job(resumed.json()["job_id"])["status"] == "done"
    assert len(fake.calls) == calls
    reopened = backend.storage_for(pid)
    try:
        assert reopened.load_chapter(0).text_segments[1].target == source
    finally:
        reopened.close()


def test_desktop_defaults_strip_legacy_precision_and_reject_policy_overrides(desktop):
    client, backend, _ = desktop
    legacy = Config.from_dict(
        {"llm": {"preset": "fake"}, "pipeline": {"translation_mode": "best_of_three"}}
    )
    saved = {"config": config_document(legacy), "template": "快速出稿", "revision": 8}
    with backend.transaction() as connection:
        backend.save_settings(saved, connection)
    response = client.get("/settings")
    assert response.status_code == 200
    defaults = response.json()
    assert defaults["default_template"] == "标准翻译"
    assert defaults["revision"] == 8
    assert "translation_mode" not in defaults["effective"]["pipeline"]
    assert "precision_concurrency" not in defaults["effective"]["pipeline"]
    assert set(defaults["effective"]["language"]) == {"source", "target"}
    assert backend.load_settings() == saved
    for yaml in (
        "pipeline: {translation_mode: best_of_three}",
        "pipeline: {precision_concurrency: 1}",
        "language: {operations: {}}",
        "language: {accepted_policy_fingerprint: {}}",
    ):
        for method, suffix in (("post", "/validate"), ("put", "")):
            rejected = getattr(client, method)(
                "/settings" + suffix,
                json={"yaml": yaml, "default_template": "标准翻译", "revision": 8},
            )
            assert rejected.status_code == 422, rejected.text
            assert backend.load_settings() == saved
