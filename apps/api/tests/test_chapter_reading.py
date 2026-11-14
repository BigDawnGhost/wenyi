"""The PostgreSQL reader shares sparse-index, source-only and bounded-window semantics."""

import pytest
from wenyi_core.models.document import Chapter, Document, Segment


def test_postgres_reader_windows_and_target_updates(pg_storage, tmp_path, monkeypatch):
    source = tmp_path / "source.txt"
    source.write_text("Offline source")
    chapter = Chapter(
        index=7,
        title="Notes",
        segments=[
            Segment(index=2, source="Heading", kind="heading"),
            Segment(index=5, source=" \n\u3000", target="ignored"),
            Segment(index=12, source="First", target=""),
            Segment(index=42, source="Second"),
        ],
    )
    pg_storage.init_from_document(
        Document(
            source_path=str(source),
            source_lang="en",
            target_lang="zh",
            fmt="text",
            chapters=[chapter],
        )
    )
    monkeypatch.setattr(pg_storage, "load_chapter", lambda *_: pytest.fail("whole chapter loaded"))
    window = pg_storage.read_chapter_window(7, offset=1, limit=1)
    assert window["total"] == 3
    assert window["saved_count"] == 1
    assert [(s["position"], s["index"]) for s in window["segments"]] == [(1, 12)]
    linked = pg_storage.read_chapter_window(7, offset=0, limit=2, around=42)
    assert linked["offset"] == linked["position"] == 2
    assert linked["segments"][0]["index"] == 42
    assert pg_storage.read_chapter_targets(7, [12, 42]) == [
        {"index": 12, "target": "", "target_before_polish": None},
        {"index": 42, "target": None, "target_before_polish": None},
    ]
    chapter.segments[-1].target = "New translation"
    pg_storage.save_chapter(chapter)
    assert pg_storage.read_chapter_targets(7, [42])[0]["target"] == "New translation"
    assert pg_storage.read_chapter_targets(7, []) == []
    with pytest.raises(KeyError):
        pg_storage.read_chapter_window(99, offset=0, limit=128)
    with pytest.raises(KeyError):
        pg_storage.read_chapter_window(7, offset=0, limit=128, around=99)


def test_postgres_reader_validates_parse_identity(pg_storage):
    binding = {"source_sha256": "a" * 64, "ingest_config": {"source_lang": "en"}}
    pg_storage.write_artifact(
        "parsed_document.json",
        {
            **binding,
            "document": {
                "chapters": [
                    {
                        "index": 7,
                        "title": "Source notes",
                        "segments": [
                            {"index": 19, "kind": "text", "source": "Original", "target": None},
                        ],
                    }
                ]
            },
        },
    )
    window = pg_storage.read_chapter_window(7, offset=0, limit=128, parsed=binding)
    assert window["total"] == 1
    assert window["segments"][0]["index"] == 19
    assert window["segments"][0]["target"] is None
    with pytest.raises(KeyError):
        pg_storage.read_chapter_window(
            7,
            offset=0,
            limit=128,
            parsed={**binding, "source_sha256": "b" * 64},
        )
