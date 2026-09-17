"""Persistent paragraph history and optimistic human edits use the same transaction."""

# ruff: noqa: F811

import pytest
from test_domain_routes_integration import domain_client, initialize  # noqa: F401
from test_storage_pg_integration import pg_pool, pg_storage  # noqa: F401
from wenyi_core.ingest.models import Chapter, Segment


def test_batch_translation_polishing_and_manual_changes_share_history(domain_client, tmp_path):
    client, storage, _ = domain_client
    initialize(storage, tmp_path)
    chapter = Chapter(index=2, segments=[Segment(index=12, source="Source", target=None)])
    storage.save_chapter(chapter)
    root = f"/projects/{storage.project_id}/review/2/segments/12"
    assert client.get(root + "/history").json() == []
    chapter.segments[0].target = "Polished translation"
    chapter.segments[0].target_before_polish = "First translation"
    storage.save_chapter(chapter)
    storage.save_chapter(chapter)
    history = client.get(root + "/history").json()
    assert [entry["kind"] for entry in history] == ["polish", "translation"]
    assert (history[0]["before"], history[0]["after"]) == (
        "First translation",
        "Polished translation",
    )
    assert all(entry["created_at"] for entry in history)
    edited = client.put(
        root, json={"target": "Human edit", "expected_target": "Polished translation"}
    )
    assert edited.status_code == 200, edited.text
    history = client.get(root + "/history").json()
    assert [entry["kind"] for entry in history] == ["manual", "polish", "translation"]
    assert history[0]["before"] == "Polished translation" and history[0]["after"] == "Human edit"
    assert storage.load_chapter(2).segments[0].target_before_polish == "First translation"
    # Idempotent saves and metadata changes must not manufacture revisions.
    assert (
        client.put(root, json={"target": "Human edit", "expected_target": "Human edit"}).status_code
        == 200
    )
    assert client.get(root + "/history").json() == history


def test_stale_edits_preserve_saved_translation_and_history(domain_client, tmp_path):
    client, storage, _ = domain_client
    initialize(storage, tmp_path)
    root = f"/projects/{storage.project_id}/review/0/segments/0"
    before = client.get(root + "/history").json()
    response = client.put(root, json={"target": "Stale replacement", "expected_target": "Outdated"})
    assert response.status_code == 409
    assert storage.load_chapter(0).segments[0].target == "润色译文"
    assert client.get(root + "/history").json() == before
    assert client.put(root, json={"target": "Missing precondition"}).status_code == 422


def test_history_and_targets_roll_back_together_and_ignore_empty_noops(domain_client, tmp_path):
    client, storage, _ = domain_client
    initialize(storage, tmp_path)
    root = f"/projects/{storage.project_id}/review/0/segments/0"
    before = client.get(root + "/history").json()
    with pytest.raises(RuntimeError):
        with storage.state_lock():
            chapter = storage.load_chapter(0)
            chapter.segments[0].target = "Not committed"
            storage.save_chapter(chapter)
            raise RuntimeError("Interrupted publication")
    assert client.get(root + "/history").json() == before
    assert storage.load_chapter(0).segments[0].target == "润色译文"
    assert client.put(root, json={"target": "", "expected_target": "润色译文"}).status_code == 200
    assert client.get(root + "/history").json()[0]["after"] == ""
    assert client.get(root.replace("segments/0", "segments/999") + "/history").status_code == 404


def test_existing_snapshots_and_edit_events_have_honest_history(domain_client, pg_pool, tmp_path):
    client, storage, _ = domain_client
    initialize(storage, tmp_path)
    with pg_pool.connection() as conn:
        conn.execute("DELETE FROM segment_revisions WHERE project_id=%s", (storage.project_id,))
        conn.execute(
            "UPDATE segments SET target='Old manual edit' WHERE project_id=%s AND chapter_seq=0 AND seg_seq=0",
            (storage.project_id,),
        )
    storage.log_event(
        "manual_translation_edited", chapter=0, index=0, before="润色译文", after="Old manual edit"
    )
    root = f"/projects/{storage.project_id}/review/0/segments/0"
    history = client.get(root + "/history").json()
    assert [entry["kind"] for entry in history] == ["manual", "before_polish"]
    assert history[0]["after"] == "Old manual edit"
    assert history[1]["after"] == "原始译文" and history[1]["created_at"] is None
    assert (
        client.put(
            root, json={"target": "New edit", "expected_target": "Old manual edit"}
        ).status_code
        == 200
    )
    assert [h["kind"] for h in client.get(root + "/history").json()] == [
        "manual",
        "manual",
        "before_polish",
    ]
