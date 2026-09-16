"""Review badges must describe the text captured at the start of a review."""

import pytest
from wenyi_api.dal import chapter_review_state


@pytest.mark.parametrize("status", ["completed", "interrupted", "running"])
def test_edit_during_review_invalidates_findings_even_if_review_finishes_later(status):
    review = {
        "status": status,
        "started_at": "2026-09-16T10:00:00+00:00",
        "finished_at": "2026-09-16T10:30:00+00:00",
        "interrupted_at": "2026-09-16T10:30:00+00:00",
    }
    meta = {"review_invalidated_at": "2026-09-16T10:15:00+00:00"}
    assert chapter_review_state(review, meta, "completed") == ("pending", False)


def test_review_started_after_retranslation_establishes_current_findings():
    review = {
        "status": "completed",
        "started_at": "2026-09-16T10:20:00+00:00",
        "finished_at": "2026-09-16T10:30:00+00:00",
    }
    meta = {"review_invalidated_at": "2026-09-16T10:15:00+00:00"}
    assert chapter_review_state(review, meta) == ("completed", True)


def test_manual_review_of_new_text_supersedes_older_in_progress_ai_review():
    review = {
        "status": "completed",
        "started_at": "2026-09-16T10:00:00+00:00",
        "finished_at": "2026-09-16T10:30:00+00:00",
    }
    meta = {
        "review_invalidated_at": "2026-09-16T10:15:00+00:00",
        "manual_reviewed_at": "2026-09-16T10:20:00+00:00",
    }
    assert chapter_review_state(review, meta) == ("completed", False)


def test_legacy_review_without_start_time_retains_timestamp_fallback():
    review = {"status": "completed", "finished_at": "2026-09-16T10:30:00+00:00"}
    meta = {"review_invalidated_at": "2026-09-16T10:15:00+00:00"}
    assert chapter_review_state(review, meta) == ("completed", True)
