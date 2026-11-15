"""Shared, bounded marker repair uses mechanical diagnostics and preserves prose."""

from __future__ import annotations

import json

import pytest
from wenyi_core.alignment import target_digest
from wenyi_core.alignment.objects import object_marker
from wenyi_core.config import Config
from wenyi_core.llm.providers.fake import FakeClient
from wenyi_core.models.document import Chapter, Segment
from wenyi_core.pipeline.annotate.annotations import AnnotationService
from wenyi_core.pipeline.run.runtime import PipelineRuntime
from wenyi_core.storage.file import FileStorage
from wenyi_core.storage.sqlite import SqliteStorage


@pytest.mark.parametrize("storage", [FileStorage, SqliteStorage])
@pytest.mark.parametrize("metadata_key", ["epub_annotations", "pdf_annotations", "annotations"])
@pytest.mark.parametrize("damage", ["missing", "duplicate", "unclosed"])
def test_end_repair_shares_error_feedback_and_keeps_successful_objects(
    tmp_path, storage, metadata_key, damage
):
    source = "Let and be positive."
    markers = [object_marker(f"math:{i}", source) for i in range(2)]
    bad = {"missing": "", "duplicate": markers[1] * 2, "unclosed": markers[1][:-1]}[damage]
    target = f"令{markers[0]}及{bad}为正数。"
    items = [
        {
            "id": f"math:{i}",
            "marker": marker,
            "mode": "point",
            "source_start": offset,
            "source_end": offset,
            "relation": "inline_object",
            "object_source": f"x{i}",
            **({"protected": True, "kind": "formula"} if metadata_key == "pdf_annotations" else {}),
        }
        for i, (marker, offset) in enumerate(zip(markers, [4, 8]))
    ]
    store = storage(str(tmp_path / "book"))
    chapter = Chapter(
        index=0,
        segments=[
            Segment(
                index=0,
                source=source,
                target=target,
                meta={metadata_key: {"items": items, "source_length": len(source)}},
            )
        ],
    )
    store.save_chapter(chapter)
    store.save_manifest({"chapters": [{"index": 0, "status": "done"}]})

    def answer(messages, *_):
        data = json.loads(messages[-1]["content"].split("INPUT JSON:\n", 1)[1])
        assert data["id"] == "math:1"
        assert data["validation_errors"]
        assert all("code" in error for error in data["validation_errors"])
        expected_code = {
            "missing": "missing_marker",
            "duplicate": "duplicate_marker",
            "unclosed": "unclosed_marker",
        }[damage]
        assert expected_code in {error["code"] for error in data["validation_errors"]}
        assert data["immutable_target"] == "令及为正数。"
        assert data["object_reference"]["source"] == "x1"
        return json.dumps(
            {"id": data["id"], "search": "及为", "replacement": f"及{data['markers'][0]}为"}
        )

    client = FakeClient(handler=answer)
    service = AnnotationService(
        PipelineRuntime(Config.from_dict({"llm": {"preset": "fake"}}), client)
    )
    try:
        service.repair_fallbacks(store)
        saved = store.load_chapter(0)
        metadata = saved.segments[0].meta[metadata_key]
        assert len(client.calls) == 1
        assert metadata["target_digest"] == target_digest("令及为正数。")
        assert [p["target_start"] for p in metadata["placements"]] == [1, 2]
        assert all(p["status"] == "aligned" for p in metadata["placements"])
        service.repair_fallbacks(store)
        assert len(client.calls) == 1
        assert store.load_manifest()["chapters"][0]["status"] == "done"
    finally:
        store.close()


@pytest.mark.parametrize("storage", [FileStorage, SqliteStorage])
@pytest.mark.parametrize("failure", ["abstain", "rewrite", "provider"])
def test_failed_repair_is_recorded_and_resumable_without_charging_again(tmp_path, storage, failure):
    source = "Let be positive."
    item = {
        "id": "math:0",
        "marker": object_marker("math:0", source),
        "mode": "point",
        "source_start": 4,
        "source_end": 4,
        "relation": "inline_object",
        "protected": True,
        "kind": "formula",
        "object_source": "x",
    }
    store = storage(str(tmp_path / "book"))
    store.save_chapter(
        Chapter(
            index=0,
            segments=[
                Segment(
                    index=0,
                    source=source,
                    target="令为正数。",
                    meta={"babeldoc_id": "0:0", "pdf_annotations": {"items": [item]}},
                )
            ],
        )
    )
    store.save_manifest({"chapters": [{"index": 0, "status": "done"}]})

    def answer(messages, *_):
        data = json.loads(messages[-1]["content"].split("INPUT JSON:\n", 1)[1])
        if failure == "provider":
            raise RuntimeError("synthetic provider failure")
        if failure == "abstain":
            return json.dumps({"id": data["id"], "abstain": True})
        return json.dumps(
            {
                "id": data["id"],
                "search": "令为",
                "replacement": f"改写{data['markers'][0]}",
            }
        )

    client = FakeClient(handler=answer)
    config = Config.from_dict({"llm": {"preset": "fake"}})
    try:
        AnnotationService(PipelineRuntime(config, client)).repair_fallbacks(store)
        saved = store.load_chapter(0)
        record = saved.segments[0].meta["pdf_annotations"]["object_repairs"]["math:0"]
        assert record["status"] == "pending"
        assert record["reason"]
        assert record["errors"][0]["code"] == "missing_marker"
        assert saved.segments[0].target == "令为正数。"
        AnnotationService(PipelineRuntime(config, client)).repair_fallbacks(store)
        assert len(client.calls) == 1
        from wenyi_core.ingest.pdf_babeldoc import _fillback_text, annotations_from_store

        assert _fillback_text(saved.segments[0]) == "令为正数。"
        payload = annotations_from_store(store, {"0:0": "令为正数。"})["0:0"]
        assert payload["omitted_protected_objects"] == ["math:0"]
        assert store.load_chapter(0) == saved
    finally:
        store.close()


def test_marker_scanning_preserves_ambiguous_prose_and_literal_tokens():
    from wenyi_core.alignment.objects import ObjectAnchor, resolve_objects

    source = "Literal 〘object:literal〙 and object ."
    anchor = ObjectAnchor("math", object_marker("math", source), len(source) - 1)
    raw = f"字面〘object:literal〙以及{anchor.marker[:-1]}unfinished prose。"
    result = resolve_objects([raw], [anchor], source)
    assert result.targets == (raw,)
    assert [row["target_offset"] for row in result.errors if row["code"] == "malformed_marker"] == [
        raw.index(anchor.marker[:-1])
    ]
    assert not result.placements


def test_cross_slice_unclosed_marker_and_unknown_token_have_mechanical_diagnostics():
    from wenyi_core.alignment.objects import ObjectAnchor, resolve_objects

    anchor = ObjectAnchor("math", object_marker("math", "Let ."), 4)
    result = resolve_objects(["令〘obj", "ect:math", "为正〘object:unknown〙。"], [anchor], "Let .")
    assert "".join(result.targets) == "令为正。"
    assert {row["code"] for row in result.errors} == {
        "unclosed_marker",
        "missing_marker",
        "unknown_marker",
    }


def test_stale_note_in_object_paragraph_is_refreshed_with_object_repair(tmp_path):
    source = "Let be positive."
    items = [
        {
            "id": "math",
            "mode": "point",
            "source_start": 4,
            "source_end": 4,
            "relation": "inline_object",
            "marker": object_marker("math", source),
        },
        {"id": "note", "mode": "point", "source_start": 4, "source_end": 4},
    ]
    store = FileStorage(str(tmp_path / "book"))
    store.save_manifest({"chapters": [{"index": 0, "status": "done"}]})
    metadata = {
        "items": items,
        "target_digest": target_digest("old target"),
        "placements": [
            {
                "id": item["id"],
                "mode": "point",
                "target_start": 0,
                "target_end": 0,
                "status": "aligned",
            }
            for item in items
        ],
    }
    segment = Segment(index=0, source=source, target="令为正数。", meta={"annotations": metadata})
    store.save_chapter(Chapter(index=0, segments=[segment]))

    def answer(messages, *_):
        data = json.loads(messages[-1]["content"].split("INPUT JSON:\n", 1)[1])
        return json.dumps(
            {"id": data["id"], "search": "令为", "replacement": f"令{data['markers'][0]}为"}
        )

    client = FakeClient(handler=answer)
    try:
        AnnotationService(
            PipelineRuntime(Config.from_dict({"llm": {"preset": "fake"}}), client)
        ).repair_fallbacks(store)
        rows = store.load_chapter(0).segments[0].meta["annotations"]["placements"]
        assert [row["target_start"] for row in rows] == [1, 1]
        assert len(client.calls) == 2
    finally:
        store.close()


def test_pdf_directory_rows_forward_omissions_and_shift_valid_sibling(tmp_path):
    from wenyi_core.ingest.pdf_babeldoc import annotations_from_store

    store = FileStorage(str(tmp_path / "book"))
    store.save_manifest({"chapters": [{"index": 0, "status": "done"}]})
    segments = []
    for i in range(2):
        source, text = "Use .", "使用。"
        marker = object_marker(f"math:{i}", source)
        item = {
            "id": f"math:{i}",
            "marker": marker,
            "mode": "point",
            "source_start": 4,
            "source_end": 4,
            "relation": "inline_object",
            "protected": True,
        }
        placement = {
            "id": item["id"],
            "mode": "point",
            "target_start": 2,
            "target_end": 2,
            "status": "aligned",
        }
        segments.append(
            Segment(
                index=i,
                source=source,
                target=text,
                meta={
                    "babeldoc_id": "0:0",
                    "babeldoc_row": {"index": i, "count": 2},
                    "pdf_annotations": {
                        "items": [item],
                        "target_digest": target_digest(text),
                        "placements": [placement] if i else [],
                    },
                },
            )
        )
    try:
        store.save_chapter(Chapter(index=0, segments=segments))
        payload = annotations_from_store(store, {"0:0": "使用。\n使用。"})["0:0"]
        assert payload["protected_objects"]
        assert payload["omitted_protected_objects"] == ["math:0"]
        valid = [row for row in payload["placements"] if row["status"] == "aligned"]
        assert valid[0]["target_start"] == valid[0]["target_end"] == 6
        assert payload["target_digest"] == target_digest("使用。\n使用。")
    finally:
        store.close()
