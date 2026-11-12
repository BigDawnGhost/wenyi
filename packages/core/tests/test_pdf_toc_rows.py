"""Directory rows keep one bridge identity but independent translation boundaries."""

import hashlib
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from wenyi_core.ingest.pdf_babeldoc import (
    _chapters_from_paragraphs,
    annotations_from_store,
    translations_from_store,
)
from wenyi_core.pdf_bridge.client import BabeldocBridgeClient, BabeldocBridgeError


def directory_paragraph():
    lines = ["3.1 First . . . 6", "3.2 Real x . . . 8", "3.3 Last . . . 9"]
    source = " ".join(lines)
    rows = []
    cursor = 0
    for index, line in enumerate(lines):
        rows.append(
            {
                "source_start": cursor,
                "source_end": cursor + len(line),
                "box": [60, 700 - index * 14, 400, 712 - index * 14],
            }
        )
        cursor += len(line) + 1
    x = source.index(" x ") + 1
    return {
        "id": "0:11",
        "page": 0,
        "index": 11,
        "source": source,
        "layout_label": "plain text",
        "rows": rows,
        "annotations": [
            {
                "id": "formula:1",
                "kind": "formula",
                "mode": "range",
                "source_start": x,
                "source_end": x + 1,
                "composition_index": 1,
            }
        ],
    }


def row_store():
    [chapter] = _chapters_from_paragraphs(
        [directory_paragraph()], book_title="Contents", toc_starts=[]
    )
    return SimpleNamespace(
        load_manifest=lambda: {
            "meta": {"babeldoc_session_id": "session", "babeldoc_bridge_url": "http://bridge"},
            "chapters": [{"index": 0}],
        },
        load_chapter=lambda index: chapter,
    ), chapter


def test_directory_rows_are_segments_without_renumbering_parent_ids():
    para = directory_paragraph()
    [chapter] = _chapters_from_paragraphs(
        [para, {"id": "0:12", "source": "Next paragraph", "page": 0}],
        book_title="Contents",
        toc_starts=[],
    )
    assert [s.source for s in chapter.segments] == [
        "3.1 First . . . 6",
        "3.2 Real x . . . 8",
        "3.3 Last . . . 9",
        "Next paragraph",
    ]
    assert [s.meta["babeldoc_id"] for s in chapter.segments] == ["0:11"] * 3 + ["0:12"]
    assert [s.meta["babeldoc_row"] for s in chapter.segments[:3]] == [
        {"index": index, "count": 3} for index in range(3)
    ]
    item = chapter.segments[1].meta["pdf_annotations"]["items"][0]
    assert item["source_start"] == chapter.segments[1].source.index("x")
    assert item["id"] == "formula:1"


def test_fillback_reassembles_rows_and_offsets_annotations_after_resume():
    from wenyi_core.ingest.pdf_babeldoc import rows_from_store

    store, chapter = row_store()
    first, second, third = chapter.segments
    first.target = "3.1 第一项……6"
    second.target = "3.2 实数 x……8"
    third.target = None
    metadata = second.meta["pdf_annotations"]
    metadata["target_digest"] = hashlib.sha256(second.target.encode()).hexdigest()
    x = second.target.index("x")
    metadata["placements"] = [
        {
            "id": "formula:1",
            "mode": "range",
            "target_start": x,
            "target_end": x + 1,
            "status": "aligned",
        }
    ]
    _, _, mapping = translations_from_store(store)
    text = "\n".join([first.target, second.target, third.source])
    assert mapping == {"0:11": text}
    ranges = rows_from_store(store, mapping)["0:11"]
    assert [text[r["target_start"] : r["target_end"]] for r in ranges] == [
        first.target,
        second.target,
        third.source,
    ]
    payload = annotations_from_store(store, mapping)["0:11"]
    assert payload["target_digest"] == hashlib.sha256(text.encode()).hexdigest()
    assert payload["placements"][0]["target_start"] == len(first.target) + 1 + x
    metadata["target_digest"] = "stale"
    assert annotations_from_store(store, mapping)["0:11"]["placements"] == []
    assert third.target is None


@pytest.mark.parametrize("corruption", ["missing", "duplicate", "null", "index_type", "count_type"])
def test_missing_or_duplicate_saved_rows_are_not_silently_overwritten(corruption):
    store, chapter = row_store()
    if corruption == "missing":
        chapter.segments.pop()
    elif corruption == "duplicate":
        chapter.segments[1].meta["babeldoc_row"]["index"] = 0
    elif corruption == "null":
        chapter.segments[1].meta["babeldoc_row"] = None
    elif corruption == "index_type":
        chapter.segments[1].meta["babeldoc_row"]["index"] = "1"
    else:
        chapter.segments[1].meta["babeldoc_row"]["count"] = "3"
    with pytest.raises(BabeldocBridgeError, match="row"):
        translations_from_store(store)


@pytest.mark.parametrize("adapter", ["file", "sqlite"])
def test_row_identity_survives_storage_reload_and_export_only_fallback(tmp_path, adapter):
    from wenyi_core.ingest.models import Document
    from wenyi_core.storage.file import FileStorage
    from wenyi_core.storage.sqlite import SqliteStorage

    _, chapter = row_store()
    chapter.segments[0].target = "第一项"
    document = Document(
        title="Contents",
        source_lang="en",
        target_lang="zh",
        source_path="contents.pdf",
        fmt="pdf",
        chapters=[chapter],
        meta={"babeldoc_session_id": "session", "babeldoc_bridge_url": "http://bridge"},
    )
    storage = FileStorage if adapter == "file" else SqliteStorage
    store = storage(str(tmp_path / "run"))
    try:
        manifest = store.stage_document(document, source_hash="0" * 64)
        store.save_manifest(manifest)
        before = store.load_chapter(0).model_dump()
        mapping = translations_from_store(store)[2]
        assert mapping["0:11"].splitlines() == [
            "第一项",
            "3.2 Real x . . . 8",
            "3.3 Last . . . 9",
        ]
        assert store.load_chapter(0).model_dump() == before
    finally:
        store.close()


def test_http_client_requires_row_capability_and_forwards_ranges(tmp_path):
    client = BabeldocBridgeClient("http://bridge")
    rows = {"0:11": [{"target_start": 0, "target_end": 4}]}
    with patch.object(client, "health", return_value={"capabilities": []}):
        with pytest.raises(BabeldocBridgeError, match="row"):
            client.fillback("session", {"0:11": "text"}, out_path=tmp_path / "out.pdf", rows=rows)
    response = Mock(status_code=200, content=b"%PDF", headers={})
    with (
        patch.object(client, "health", return_value={"capabilities": ["toc-rows-v1"]}),
        patch("wenyi_core.pdf_bridge.client.httpx.post", return_value=response) as post,
    ):
        client.fillback("session", {"0:11": "text"}, out_path=tmp_path / "out.pdf", rows=rows)
    assert post.call_args.kwargs["json"]["rows"] == rows
