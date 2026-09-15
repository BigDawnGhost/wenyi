"""Persist Autofix inference identity and the complete index before publication."""

from __future__ import annotations

from typing import Any

from ..ingest.models import Chapter
from ..llm.routing import inference_snapshot
from ..review.autofix_models import AutofixCandidates, AutofixPlan, PublishLocation, text_hash
from ..review.models import ReviewOutcome
from ..review.run_store import ReviewRunStore


def prepare_identity(debug: ReviewRunStore, llm_config) -> dict[str, Any]:
    inference = inference_snapshot(llm_config, ("autofix.verify", "autofix.fix"))
    plan = debug.load_json("autofix/plan.json")
    if plan is not None and plan.get("inference") != inference:
        raise ValueError(
            "Autofix planning models changed. Restore the prior routes to finish this review."
        )
    debug.write_json("autofix/plan.json", {"inference": inference})

    return inference


def save_plan(
    chapters: list[Chapter],
    candidates: AutofixCandidates,
    inference: dict[str, Any],
    debug: ReviewRunStore,
    outcome: ReviewOutcome,
) -> AutofixPlan:
    chapters_by_index = {chapter.index: chapter for chapter in chapters}
    overrides = candidates.overrides
    records = candidates.records
    locations: list[PublishLocation] = []
    for (chapter_index, text_index), target in sorted(overrides.items()):
        segment = chapters_by_index[chapter_index].text_segments[text_index]
        baseline = segment.target or ""
        locations.append(
            {
                "chapter": chapter_index,
                "index": text_index,
                "segment_ref": f"ch{chapter_index}:text{text_index}:seg{segment.index}",
                "before": baseline,
                "before_hash": text_hash(baseline),
                "target": target,
                "target_hash": text_hash(target),
                "record_ids": [
                    record["record_id"]
                    for record in records
                    if record["chapter"] == chapter_index
                    and record["index"] == text_index
                    and record["status"] == "planned"
                ],
                "status": "pending",
                "alignment_status": "pending",
            }
        )

    index: AutofixPlan = {
        "version": 1,
        "inference": inference,
        "review_id": debug.review_id,
        "status": "applying",
        "reviewed_content_digest": outcome.result.get("reviewed_content_digest"),
        "records": records,
        "locations": locations,
    }
    debug.write_json("autofix/index.json", index)
    debug.log_event(
        "review_autofix_planned",
        record_count=len(records),
        location_count=len(locations),
        issue_group_count=candidates.issue_group_count,
    )
    return index
