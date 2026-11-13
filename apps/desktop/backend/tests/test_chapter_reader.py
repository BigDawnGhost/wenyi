"""HTTP reader windows and conditional refreshes use temporary real SQLite state."""

from fastapi.testclient import TestClient
from wenyi_core.models.document import Chapter, Document, Segment
from wenyi_desktop.main import create_app


def test_reader_refreshes_only_changed_translations(desktop_context, tmp_path):
    backend = desktop_context.repository
    pid = backend.create_project("Notes", "en", "zh", {})
    source = tmp_path / "source.txt"
    source.write_text("Synthetic source")
    store = backend.storage_for(pid)
    chapter = Chapter(
        index=7,
        title="Notes",
        segments=[
            Segment(index=2, source="Heading", kind="heading"),
            Segment(index=5, source="\n\u3000", target="ignored"),
            Segment(index=12, source="First", target=None),
            Segment(index=42, source="Second", target=""),
        ],
    )
    store.init_from_document(
        Document(
            source_path=str(source),
            source_lang="en",
            target_lang="zh",
            fmt="text",
            chapters=[chapter],
        )
    )
    with TestClient(create_app(context=desktop_context)) as client:
        url = f"/projects/{pid}/chapters/7"
        response = client.get(f"{url}/window", params={"limit": 2, "around": 42})
        assert response.status_code == 200
        linked = response.json()
        assert linked["offset"] == linked["position"] == 2
        assert linked["total"] == 3
        assert linked["segments"][0]["index"] == 42
        assert "_meta" not in linked
        assert client.get(f"{url}/window?limit=257").status_code == 422
        assert client.get(f"{url}/window?offset=-1").status_code == 422
        assert client.get(f"{url}/window?around=99").status_code == 404
        assert client.get(f"/projects/{pid}/chapters/99/window").status_code == 404
        first = client.get(f"{url}/window").json()
        known = {
            "segments": [
                {"index": s["index"], "revision": s["revision"]} for s in first["segments"]
            ]
        }
        assert client.post(f"{url}/updates", json=known).json() == []
        chapter.segments[2].target = "New translation"
        chapter.segments[2].target_before_polish = "Draft"
        store.save_chapter(chapter)
        changed = client.post(f"{url}/updates", json=known).json()
        assert len(changed) == 1
        patch = changed[0]
        assert patch["index"] == 12
        assert patch["target"] == "New translation"
        assert patch["target_before_polish"] == "Draft"
        assert "source" not in patch and "position" not in patch
        assert patch["previous_revision"] == known["segments"][1]["revision"]
        assert patch["revision"] != patch["previous_revision"]
        known["segments"][1]["revision"] = patch["revision"]
        assert client.post(f"{url}/updates", json=known).json() == []
        # A saved empty translation must also be delivered, not treated as missing.
        chapter.segments[2].target = ""
        store.save_chapter(chapter)
        assert client.post(f"{url}/updates", json=known).json()[0]["target"] == ""
        assert (
            client.post(f"{url}/updates", json={"segments": known["segments"] * 100}).status_code
            == 422
        )
        store.write_artifact(
            "reviews/review-1/result.json",
            {
                "status": "completed",
                "finished_at": "2026-01-01T00:00:00+00:00",
                "issues": [{"chapter": 7, "index": 1, "detail": "A review note"}],
            },
        )
        issues = client.get(f"{url}/issues").json()
        assert issues[0]["text_position"] == 1
        assert issues[0]["index"] == 12
    store.close_all()
