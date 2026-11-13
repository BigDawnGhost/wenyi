"""Indexed reader windows stay bounded after one-time projection construction."""

import pytest
from wenyi_core.models.document import Chapter, Document, Segment
from wenyi_core.storage.sqlite import SCHEMA_VERSION, SqliteStorage


@pytest.fixture
def reader(tmp_path):
    source = tmp_path / "source.txt"
    source.write_text("Offline source")
    store = SqliteStorage(str(tmp_path / "state"))
    store.init_from_document(
        Document(
            source_path=str(source),
            source_lang="en",
            target_lang="zh",
            fmt="text",
            chapters=[
                Chapter(
                    index=3,
                    title="Notes",
                    segments=[
                        Segment(index=2, source="Heading", kind="heading"),
                        Segment(index=7, source=" \n\u3000", target="ignored"),
                        Segment(index=10, source="First", target=""),
                        Segment(index=18, source="Second unique note"),
                    ],
                )
            ],
        )
    )
    yield store
    store.close_all()


def test_window_and_deep_link_use_positions_not_segment_ids(reader, monkeypatch):
    monkeypatch.setattr(reader, "load_chapter", lambda *_: pytest.fail("whole chapter loaded"))
    window = reader.read_chapter_window(3, offset=1, limit=1)
    assert window["total"] == 3
    assert window["offset"] == 1
    assert [(s["position"], s["index"], s["target"]) for s in window["segments"]] == [(1, 10, "")]
    linked = reader.read_chapter_window(3, offset=0, limit=2, around=18)
    assert linked["position"] == 2
    assert linked["offset"] == 2
    assert linked["segments"][0]["index"] == 18
    with pytest.raises(KeyError):
        reader.read_chapter_window(99, offset=0, limit=2)
    with pytest.raises(KeyError):
        reader.read_chapter_window(3, offset=0, limit=2, around=999)


def test_target_projection_is_bounded_and_preserves_saved_empty_values(reader, monkeypatch):
    monkeypatch.setattr(reader, "load_chapter", lambda *_: pytest.fail("whole chapter loaded"))
    rows = reader.read_chapter_targets(3, [10, 18])
    assert rows == [
        {"index": 10, "target": "", "target_before_polish": None},
        {"index": 18, "target": None, "target_before_polish": None},
    ]
    assert reader.read_chapter_targets(3, []) == []


def test_large_window_does_not_decode_chapter_artifact(reader, monkeypatch):
    chapter = Chapter(
        index=3,
        title="Large notes",
        segments=[Segment(index=i * 2, source=f"Note {i}") for i in range(65_000)],
    )
    reader.save_chapter(chapter)
    original = reader.read_artifact

    def read(key):
        assert not key.startswith("chapters/")
        return original(key)

    monkeypatch.setattr(reader, "read_artifact", read)
    window = reader.read_chapter_window(3, offset=64_980, limit=128)
    assert window["total"] == 65_000
    assert len(window["segments"]) == 20
    assert window["segments"][-1]["index"] == 129_998
    # A SQL instruction budget catches an accidental full scan without a flaky
    # wall-clock SLA. Both position and ID reads must seek, even near the end.
    connection = reader._connection()
    connection.set_progress_handler(lambda: 1, 25_000)
    try:
        assert reader.read_chapter_window(3, offset=64_980, limit=16)["total"] == 65_000
        assert len(reader.read_chapter_targets(3, [0, 64_000, 129_998])) == 3
    finally:
        connection.set_progress_handler(None, 0)


def test_warm_reader_uses_indexed_seeks_and_tracks_atomic_writes(reader):
    """Random seeks and translation refresh must not scan chapter JSON again."""
    import sqlite3

    reader.read_chapter_window(3, offset=0, limit=2)
    connection = reader._connection()
    statements = []
    connection.set_trace_callback(statements.append)
    reader.read_chapter_window(3, offset=2, limit=1)
    reader.read_chapter_targets(3, [18])
    connection.set_trace_callback(None)
    assert not any("json_each" in sql.lower() for sql in statements)
    assert not any("offset " in sql.lower() for sql in statements)

    chapter = reader.load_chapter(3)
    chapter.segments[-1].target = "Saved"
    with pytest.raises(RuntimeError):
        with reader.state_lock():
            reader.save_chapter(chapter)
            assert reader.read_chapter_targets(3, [18])[0]["target"] == "Saved"
            raise RuntimeError("Roll back both state and its index")
    assert reader.read_chapter_targets(3, [18])[0]["target"] is None
    reader.save_chapter(chapter)
    reader.close_all()
    reopened = SqliteStorage(reader.run_dir, create=False)
    assert reopened.read_chapter_targets(3, [18])[0]["target"] == "Saved"
    assert reopened.read_chapter_window(3, offset=2, limit=1)["saved_count"] == 2
    reopened.delete_artifact("chapters/ch3.json")
    with pytest.raises(KeyError):
        reopened.read_chapter_window(3, offset=0, limit=2)
    reopened.close_all()
    with sqlite3.connect(reader.database_path) as conn:
        assert conn.execute("SELECT count(*) FROM reader_segments").fetchone()[0] == 0


def test_source_window_validates_parse_binding_and_ignores_staged_targets(reader):
    identity = {"source_sha256": "a" * 64, "ingest_config": {"source_lang": "en"}}
    reader.write_artifact(
        "parsed_document.json",
        {
            **identity,
            "document": {
                "chapters": [
                    {
                        "index": 3,
                        "title": "Source notes",
                        "segments": [
                            {"index": 8, "source": "Original", "target": None, "kind": "text"},
                        ],
                    },
                ],
            },
        },
    )
    window = reader.read_chapter_window(3, offset=0, limit=128, parsed=identity)
    assert window["title"] == "Source notes"
    assert window["segments"][0]["index"] == 8
    assert window["segments"][0]["target"] is None
    with pytest.raises(KeyError):
        reader.read_chapter_window(
            3, offset=0, limit=128, parsed={**identity, "source_sha256": "b" * 64}
        )
    with pytest.raises(KeyError):
        reader.read_chapter_window(
            3, offset=0, limit=128, parsed={**identity, "ingest_config": {"source_lang": "zh"}}
        )
    replaced = reader.read_artifact("parsed_document.json")
    replaced["source_sha256"] = "c" * 64
    replaced["document"]["chapters"][0]["segments"] = [{"index": 15, "source": "Replacement"}]
    reader.write_artifact("parsed_document.json", replaced)
    with pytest.raises(KeyError):
        reader.read_chapter_window(3, offset=0, limit=1, parsed=identity)
    new = reader.read_chapter_window(
        3, offset=0, limit=1, parsed={**identity, "source_sha256": "c" * 64}
    )
    assert new["segments"][0]["index"] == 15


def test_legacy_compact_counts_upgrade_without_changing_chapters(reader):
    import sqlite3

    chapter = reader.read_artifact("chapters/ch3.json")
    reader.close_all()
    with sqlite3.connect(reader.database_path) as connection:
        connection.execute("PRAGMA user_version=1")
        connection.execute("UPDATE chapter_statistics SET word_count=999,target_word_count=999")
    readonly = SqliteStorage(reader.run_dir, create=False)
    assert readonly.chapter_statistics()[0]["word_count"] == 3
    assert readonly.chapter_statistics()[0]["target_word_count"] == 1
    readonly.close_all()
    with sqlite3.connect(reader.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    writable = SqliteStorage(reader.run_dir)
    assert writable.chapter_statistics()[0]["word_count"] == 3
    assert writable.chapter_statistics()[0]["target_word_count"] == 1
    assert writable.read_artifact("chapters/ch3.json") == chapter
    writable.close_all()
    with sqlite3.connect(reader.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
