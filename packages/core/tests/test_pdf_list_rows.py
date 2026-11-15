"""List items reuse explicit rows without changing parent or protected-object identities."""

import hashlib

import pytest
from wenyi_core.ingest.models import Document
from wenyi_core.ingest.pdf_babeldoc import (
    _chapters_from_paragraphs,
    annotations_from_store,
    rows_from_store,
    translations_from_store,
)


def list_paragraph():
    chunks = ["First item body.", "Second item body.", "Third item body."]
    rows, annotations = [], []
    cursor = 0
    for index, chunk in enumerate(chunks):
        rows.append(
            {
                "source_start": cursor,
                "source_end": cursor + len(chunk),
                "box": [20, 600 - index * 24, 400, 622 - index * 24],
            }
        )
        annotations.append(
            {
                "id": f"formula:{index}",
                "kind": "formula",
                "mode": "point",
                "protected": True,
                "object_source": "(cid:23)",
                "source_start": cursor,
                "source_end": cursor,
                "composition_index": index,
            }
        )
        cursor += len(chunk) + 1
    # A full expression remains one object inside the second item.
    position = len(chunks[0]) + 1 + len("Second ")
    annotations.append(
        {
            "id": "formula:expression",
            "kind": "formula",
            "mode": "point",
            "protected": True,
            "object_source": "x^(n+1)",
            "source_start": position,
            "source_end": position,
            "composition_index": 3,
        }
    )
    return {
        "id": "25:5",
        "page": 25,
        "index": 5,
        "source": " ".join(chunks),
        "rows": rows,
        "annotations": annotations,
    }


@pytest.mark.parametrize("adapter", ["file", "sqlite"])
def test_list_items_resume_and_export_with_stable_parent_and_object_ids(tmp_path, adapter):
    from wenyi_core.storage.file import FileStorage
    from wenyi_core.storage.sqlite import SqliteStorage

    [chapter] = _chapters_from_paragraphs(
        [list_paragraph()], book_title="Synthetic list", toc_starts=[]
    )
    assert [s.source for s in chapter.segments] == [
        "First item body.",
        "Second item body.",
        "Third item body.",
    ]
    assert [s.meta["babeldoc_id"] for s in chapter.segments] == ["25:5"] * 3
    for index, segment in enumerate(chapter.segments):
        assert segment.meta["babeldoc_row"] == {"index": index, "count": 3}
        items = segment.meta["pdf_annotations"]["items"]
        assert items[0]["id"] == f"formula:{index}"
        assert items[0]["source_start"] == items[0]["source_end"] == 0
        assert items[0]["object_source"] == "(cid:23)"
        assert items[0]["object_tag"] == "PDF protected object"
    expression = chapter.segments[1].meta["pdf_annotations"]["items"][1]
    assert expression["id"] == "formula:expression"
    assert expression["source_start"] == len("Second ")
    assert expression["object_source"] == "x^(n+1)"
    chapter.segments[0].target = "第一项正文。\n仍是第一项。"
    chapter.segments[1].target = "第二项正文。"
    for segment in chapter.segments[:2]:
        assert segment.target is not None
        metadata = segment.meta["pdf_annotations"]
        metadata["objects_resolved_for_export"] = True
        metadata["target_digest"] = hashlib.sha256(segment.target.encode()).hexdigest()
        metadata["placements"] = [
            {
                "id": item["id"],
                "mode": "point",
                "target_start": 0 if item["object_source"] == "(cid:23)" else 3,
                "target_end": 0 if item["object_source"] == "(cid:23)" else 3,
                "target_order": order,
                "status": "aligned",
            }
            for order, item in enumerate(metadata["items"])
        ]
    storage = FileStorage if adapter == "file" else SqliteStorage
    path = str(tmp_path / "run")
    store = storage(path)
    try:
        manifest = store.stage_document(
            Document(
                title="Synthetic list",
                source_lang="en",
                target_lang="zh",
                source_path="synthetic-list.pdf",
                fmt="pdf",
                chapters=[chapter],
                meta={"babeldoc_session_id": "session", "babeldoc_bridge_url": "http://bridge"},
            ),
            source_hash="0" * 64,
        )
        store.save_manifest(manifest)
    finally:
        store.close()
    restored = storage(path)
    try:
        before = restored.load_chapter(0).model_dump()
        _, _, mapping = translations_from_store(restored)
        text = mapping["25:5"]
        ranges = rows_from_store(restored, mapping)["25:5"]
        assert [text[r["target_start"] : r["target_end"]] for r in ranges] == [
            chapter.segments[0].target,
            chapter.segments[1].target,
            chapter.segments[2].source,
        ]
        aligned = annotations_from_store(restored, mapping)["25:5"]
        assert aligned["target_digest"] == hashlib.sha256(text.encode()).hexdigest()
        expression_placement = next(
            row for row in aligned["placements"] if row["id"] == "formula:expression"
        )
        assert expression_placement["target_start"] == ranges[1]["target_start"] + 3
        assert restored.load_chapter(0).model_dump() == before
        assert restored.load_chapter(0).segments[-1].target is None
    finally:
        restored.close()
