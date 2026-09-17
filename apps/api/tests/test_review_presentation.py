"""Read-only review rows keep recommendations separate from actual publication."""

from types import SimpleNamespace

from wenyi_api.review_presentation import review_items
from wenyi_core.ingest.models import Chapter, Segment


def storage():
    chapter = Chapter(
        index=3,
        title="A chapter",
        segments=[
            Segment(index=7, source="", kind="image"),
            Segment(index=18, source="Source one", target="Current translation"),
            Segment(index=29, source="Source two", target="Other translation"),
        ],
    )
    return SimpleNamespace(
        load_chapter=lambda index: chapter if index == 3 else None,
        read_artifact=lambda key: (
            [
                {
                    "issue_key": "a",
                    "issue_id": "round-a",
                    "chapter": 3,
                    "index": 0,
                    "evidence_refs": ["ch3:text1:seg29", "ch3:text1:seg18", "invalid"],
                }
            ]
            if key.endswith("unresolved_issues.json")
            else None
        ),
    )


def test_rows_join_evidence_and_only_matching_publications():
    result = {
        "issues": [
            {"issue_key": "a", "chapter": 3, "index": 0, "detail": "Missing phrase"},
            {"issue_key": "b", "chapter": 3, "index": 0, "detail": "Different problem"},
        ],
        "changes": [{"chapter": 3, "index": 0, "issue_keys": ["a"], "suggested_target": "Draft"}],
    }
    records = [
        {
            "record_id": "fix-a",
            "chapter": 3,
            "index": 0,
            "issue_ids": ["round-a"],
            "status": "applied",
            "before": "Before",
            "after": "Published",
        }
    ]
    rows = review_items(storage(), "review-a", result, {"records": records})
    assert [row["status"] for row in rows] == ["fixed", "pending"]
    assert rows[0]["location"]["segment_index"] == 18
    assert rows[0]["location"]["current_target"] == "Current translation"
    assert rows[0]["changes"][0]["suggested_target"] == "Draft"
    assert rows[0]["publications"][0]["after"] == "Published"
    assert [item["segment_index"] for item in rows[0]["evidence"]] == [29]
    assert rows[1]["publications"] == []


def test_suggestions_are_pending_and_orphan_records_remain_visible():
    result = {
        "issues": [],
        "changes": [
            {
                "chapter": 3,
                "index": 1,
                "suggested_target": "Unpublished",
                "review_result": "not_rereported",
            }
        ],
    }
    rows = review_items(
        storage(),
        "review-a",
        result,
        {
            "records": [
                {
                    "record_id": "failed",
                    "chapter": 3,
                    "index": 99,
                    "status": "failed",
                    "reason": "invalid_location",
                }
            ]
        },
    )
    assert [row["kind"] for row in rows] == ["change", "publication"]
    assert [row["status"] for row in rows] == ["pending", "failed"]
    assert rows[0]["location"]["segment_index"] == 29
    assert rows[1]["location"] is None


def test_failed_and_unchanged_publications_never_count_as_fixed():
    for status, expected in [
        ("planned", "pending"),
        ("not_applied", "failed"),
        ("not_applied_no_net_change", "unchanged"),
    ]:
        result = {"issues": [{"issue_key": "a", "chapter": 3, "index": 0}]}
        rows = review_items(
            storage(),
            "review-a",
            result,
            {
                "records": [
                    {
                        "record_id": "one",
                        "issue_keys": ["a"],
                        "status": status,
                        "chapter": 3,
                        "index": 0,
                    }
                ]
            },
        )
        assert rows[0]["status"] == expected


def test_unknown_locations_are_not_guessed():
    result = {"issues": [{"issue_key": "a", "chapter": True, "index": 0}], "changes": []}
    rows = review_items(storage(), "review-a", result, {"records": []})
    assert rows[0]["location"] is None
    assert rows[0]["status"] == "pending"


def test_saved_empty_translation_is_not_presented_as_pending():
    saved = storage()
    chapter = saved.load_chapter(3)
    chapter.segments[1].target = ""
    saved.load_chapter = lambda index: chapter
    rows = review_items(
        saved, "review-a", {"issues": [{"chapter": 3, "index": 0}]}, {"records": []}
    )
    assert rows[0]["location"]["current_target"] == ""


def test_publication_details_use_final_text_and_reason_from_persisted_location():
    result = {"issues": [{"issue_key": "a", "chapter": 3, "index": 0}]}
    record = {
        "record_id": "fix-a",
        "issue_keys": ["a"],
        "chapter": 3,
        "index": 0,
        "status": "applied",
        "before": "Baseline",
        "after": "Intermediate draft",
    }
    location = {
        "chapter": 3,
        "index": 0,
        "record_ids": ["fix-a"],
        "status": "applied",
        "before": "Baseline",
        "target": "Final published translation",
    }
    autofix = {"records": [record], "index": {"locations": [location]}}
    row = review_items(storage(), "review-a", result, autofix)[0]
    publication = row["publications"][0]
    assert publication["after"] == "Intermediate draft"
    assert publication["publication"]["target"] == "Final published translation"
    assert "publication" not in record  # Presentation must not mutate the saved ledger.
    record["status"] = "not_applied"
    location.update(status="failed", reason="formal_target_changed")
    row = review_items(storage(), "review-a", result, autofix)[0]
    assert row["status"] == "failed"
    assert row["publications"][0]["publication"]["reason"] == "formal_target_changed"

    location["record_ids"] = ["a-different-fix"]
    row = review_items(storage(), "review-a", result, autofix)[0]
    assert "publication" not in row["publications"][0]
