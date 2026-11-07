"""Project prompt selection reaches the real local runner and survives interrupted jobs."""

import json

import pytest
from desktop_test_support import wait_job
from wenyi_backend.context import current_context


@pytest.mark.parametrize("genre", ["literary", "academic"])
def test_project_genre_reaches_preparation_translation_review_and_resume(desktop, genre):
    client, backend, fake = desktop
    created = client.post(
        "/projects",
        data={
            "project": json.dumps(
                {
                    "name": "Prompt family",
                    "source_lang": "en",
                    "target_lang": "zh",
                    "prepare": True,
                    "prompt_genre": genre,
                }
            )
        },
        files={"file": ("book.txt", b"The argument distinguishes evidence from inference.\n")},
    )
    assert created.status_code == 201, created.text
    pid = created.json()["id"]
    root = f"/projects/{pid}"
    repository = current_context().repository
    prepared = wait_job(repository.list_jobs(pid)[0]["run_id"])
    assert prepared["status"] == "done", prepared
    assert prepared["params"]["config_snapshot"]["pipeline"]["prompt_genre"] == genre
    assert client.get(root + "/config").json()["effective"]["pipeline"]["prompt_genre"] == genre

    original_handler = fake.handler

    def interrupt_translation(messages, tier, json_mode):
        if f"{genre} translator" in messages[0]["content"]:
            raise RuntimeError("Offline interruption")
        return original_handler(messages, tier, json_mode)

    fake.handler = interrupt_translation
    started = client.post(root + "/translate")
    assert started.status_code == 200, started.text
    interrupted = wait_job(started.json()["job_id"])
    assert interrupted["status"] == "error", interrupted
    assert interrupted["params"]["config_snapshot"]["pipeline"]["prompt_genre"] == genre
    fake.handler = original_handler
    resumed = client.post(root + "/resume")
    assert resumed.status_code == 200, resumed.text
    finished = wait_job(resumed.json()["job_id"])
    assert finished["status"] == "done", finished
    assert finished["params"]["config_snapshot"]["pipeline"]["prompt_genre"] == genre
    systems = [call["messages"][0]["content"] for call in fake.calls]
    assert any(f"{genre} translator" in system for system in systems)
    assert not any(
        f"{'literary' if genre == 'academic' else 'academic'} translator" in system
        for system in systems
    )
    assert any(call["operation"].startswith("review.") for call in fake.calls)
    store = backend.storage_for(pid)
    try:
        checkpoint = store.read_artifact("language-policies/checkpoint.json")
        assert checkpoint
        for reference in checkpoint.values():
            assert store.read_artifact(reference)["context"]["genre"] == genre
        assert store.load_chapter(0).text_segments[0].target is not None
    finally:
        store.close()
    calls = len(fake.calls)
    skipped = client.post(root + "/translate")
    assert skipped.status_code == 200, skipped.text
    assert wait_job(skipped.json()["job_id"])["status"] == "done"
    # A new run may review again, but completed translation batches must be skipped.
    assert not any(
        call["operation"] in {"translation.body", "polish.body"} for call in fake.calls[calls:]
    )
    exported = client.post(root + "/exports", json={"format": "txt"})
    assert exported.status_code == 200, exported.text
    export_job = wait_job(exported.json()["job_id"])
    assert export_job["status"] == "done", export_job
    assert export_job["params"]["config_snapshot"]["pipeline"]["prompt_genre"] == genre
    downloaded = client.get(root + f"/exports/{exported.json()['export_id']}/download")
    assert downloaded.status_code == 200, downloaded.text
    assert "润0" in downloaded.text
