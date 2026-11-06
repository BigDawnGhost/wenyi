"""Desktop chapter summaries stay linear for very long books."""

import pytest
from wenyi_core.models.document import Chapter, Document, Segment
from wenyi_core.storage.sqlite import SqliteStorage


def _project(backend, tmp_path, count):
    pid = backend.create_project("Long", "en", "zh", {"template": "标准翻译"})
    source = tmp_path / "long.txt"
    source.write_text("Synthetic source.", encoding="utf-8")
    store = backend.storage_for(pid)
    document = Document(
        title="Long",
        source_path=str(source),
        fmt="txt",
        source_lang="en",
        target_lang="zh",
        chapters=[
            Chapter(
                index=index,
                title=f"Chapter {index}",
                segments=[
                    Segment(index=0, source="First"),
                    Segment(index=1, source="Second"),
                    Segment(index=2, source=""),
                    Segment(index=3, source=" "),
                    Segment(index=4, source="Heading", kind="heading"),
                ],
            )
            for index in range(count)
        ],
    )
    manifest = store.init_from_document(document)
    manifest["chapters"][0]["title_translated"] = "第一章"
    store.save_manifest(manifest)
    chapter = store.load_chapter(1)
    chapter.segments[0].target = "译文"
    chapter.segments[1].target = ""
    store.save_chapter_with_status(chapter, "done")
    store.write_artifact(
        "reviews/review-1/result.json",
        {
            "status": "completed",
            "finished_at": "2026-01-01T00:00:00+00:00",
            "issues": [{"chapter": 0}, {"chapter": 0}, {"chapter": 2}, {"chapter": "x"}],
        },
    )
    return pid, store


def _expected(count):
    return [
        {
            "index": index,
            "title": f"Chapter {index}",
            "title_translated": "第一章" if index == 0 else None,
            "status": "done" if index == 1 else "pending",
            # Headings are translated, so they count; blank sources do not.
            "word_count": 3,
            "target_word_count": 2 if index == 1 else 0,
            "review_status": "completed",
            "review_issue_count": {0: 2, 2: 1}.get(index, 0),
        }
        for index in range(count)
    ]


def test_summaries_read_the_manifest_once(desktop_context, tmp_path, monkeypatch):
    backend = desktop_context.repository
    pid, store = _project(backend, tmp_path, 30)
    store.close()
    reads = []
    read_artifact = SqliteStorage.read_artifact

    def track(storage, key):
        reads.append(key)
        return read_artifact(storage, key)

    def per_chapter(*args, **kwargs):
        pytest.fail("chapter summaries re-read the manifest for a chapter")

    monkeypatch.setattr(SqliteStorage, "read_artifact", track)
    monkeypatch.setattr(SqliteStorage, "load_chapter", per_chapter)
    assert backend.chapter_summaries(pid) == _expected(30)
    # A constant number of manifest reads (the existence check plus one snapshot),
    # never one per chapter.
    assert reads.count("manifest.json") <= 2
    assert backend.total_word_count(pid) == 90
