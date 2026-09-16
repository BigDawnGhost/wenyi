"""Publish a persisted Autofix index with optimistic target checks and resumable alignment."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..review.autofix_models import text_hash
from ..review.models import ReviewOutcome
from ..review.run_store import ReviewRunStore
from ..storage.protocol import Storage

if TYPE_CHECKING:
    from .annotations import AnnotationService
    from .docx_styles import DocxStyleService

ProgressFn = Callable[[int, int, str], None]


class AutofixPublisher:
    def __init__(self, annotations: AnnotationService, docx_styles: DocxStyleService):
        self._annotations = annotations
        self._docx_styles = docx_styles

    def apply(
        self,
        store: Storage,
        debug: ReviewRunStore,
        index: dict[str, Any],
        result: dict[str, Any],
        *,
        progress: ProgressFn | None,
    ) -> None:
        """Publish formal targets idempotently from the index, then refresh character-offset
        metadata.
        """
        raw_locations = index.get("locations")
        locations = [row for row in raw_locations or [] if isinstance(row, dict)]
        by_chapter: dict[int, list[dict[str, Any]]] = {}
        for row in locations:
            chapter = row.get("chapter")
            if isinstance(chapter, int) and not isinstance(chapter, bool):
                by_chapter.setdefault(chapter, []).append(row)

        total = len(locations)
        done = 0
        if progress and total:
            progress(0, total, "Publishing Review Autofix")
        for chapter_index in sorted(by_chapter):
            chapter = store.load_chapter(chapter_index)
            chapter_locations = sorted(
                by_chapter[chapter_index], key=lambda row: row.get("index", -1)
            )
            applied_positions: list[int] = []
            chapter_changed = False
            for row in chapter_locations:
                text_index = row.get("index")
                target = row.get("target")
                before = row.get("before")
                if (
                    isinstance(text_index, bool)
                    or not isinstance(text_index, int)
                    or not 0 <= text_index < len(chapter.text_segments)
                    or not isinstance(target, str)
                    or not isinstance(before, str)
                ):
                    row["status"] = "failed"
                    row["reason"] = "invalid_index_location"
                    done += 1
                    continue
                current = chapter.text_segments[text_index].target or ""
                if current == target:
                    row["status"] = "no_net_change" if current == before else "applied"
                    if current != before and row.get("alignment_status") != "completed":
                        applied_positions.append(text_index)
                elif current == before and text_hash(current) == row.get("before_hash"):
                    chapter.text_segments[text_index].target = target
                    row["status"] = "applied"
                    applied_positions.append(text_index)
                    chapter_changed = True
                else:
                    row["status"] = "failed"
                    row["reason"] = "formal_target_changed"
                    row["actual_hash"] = text_hash(current)
                done += 1
                if progress:
                    progress(done, total, "Publishing Review Autofix")
            if chapter_changed:
                store.save_chapter(chapter)

            # Post-translation alignment depends on target offsets; refresh it against final published text.
            for text_index in sorted(set(applied_positions)):
                row = next(item for item in chapter_locations if item.get("index") == text_index)
                if row.get("status") == "failed" or row.get("alignment_status") == "completed":
                    continue
                self._annotations.align_annotations_after_batch(
                    chapter_index,
                    chapter,
                    text_index,
                    1,
                    store,
                )
                self._docx_styles.align_styles_after_batch(
                    chapter_index,
                    chapter,
                    text_index,
                    1,
                    store,
                )
                row["alignment_status"] = "completed"
            debug.write_json("autofix/index.json", index)

        location_status = {
            (row.get("chapter"), row.get("index")): row.get("status") for row in locations
        }
        for record in index.get("records", []):
            if not isinstance(record, dict) or record.get("status") != "planned":
                continue
            status = location_status.get((record.get("chapter"), record.get("index")))
            if status == "applied":
                record["status"] = "applied"
            elif status == "no_net_change":
                record["status"] = "not_applied_no_net_change"
            elif status == "failed":
                record["status"] = "not_applied"

        has_failures = any(
            isinstance(record, dict) and record.get("status") in {"failed", "not_applied"}
            for record in index.get("records", [])
        ) or any(row.get("status") == "failed" for row in locations)
        index["status"] = "partial" if has_failures else "completed"
        debug.write_json("autofix/index.json", index)

    def finish(
        self,
        store: Storage,
        debug: ReviewRunStore,
        index: dict[str, Any],
        result: dict[str, Any],
    ) -> ReviewOutcome:
        """Merge the autofix publication summary into review result.json idempotently."""
        records = [record for record in index.get("records", []) if isinstance(record, dict)]
        locations = [row for row in index.get("locations", []) if isinstance(row, dict)]
        applied_locations = [row for row in locations if row.get("status") == "applied"]
        failed_issue_records = [
            record
            for record in records
            if record.get("origin") == "final_issue_fix"
            and record.get("status") in {"failed", "not_applied"}
        ]
        autofix = {
            "enabled": True,
            "status": index.get("status", "partial"),
            "index": "autofix/index.json",
            "applied_segment_count": len(applied_locations),
            "applied_change_count": sum(
                record.get("origin") == "change" and record.get("status") == "applied"
                for record in records
            ),
            "applied_issue_fix_count": sum(
                record.get("origin") == "final_issue_fix" and record.get("status") == "applied"
                for record in records
            ),
            "failed_issue_count": sum(
                max(1, len(record.get("issue_ids") or [])) for record in failed_issue_records
            ),
            "failed_record_count": sum(
                record.get("status") in {"failed", "not_applied"} for record in records
            ),
        }
        updated = dict(result)
        existing_autofix = updated.get("autofix")
        if (
            isinstance(existing_autofix, dict)
            and existing_autofix.get("index") == "autofix/index.json"
            and existing_autofix.get("status") == autofix["status"]
        ):
            return ReviewOutcome(
                run_dir=debug.run_dir,
                result=updated,
                usage=debug.load_usage() or {},
            )
        updated["summary"] = {
            **dict(result.get("summary") or {}),
            "autofix_applied_segment_count": autofix["applied_segment_count"],
            "autofix_failed_issue_count": autofix["failed_issue_count"],
        }
        updated["autofix"] = autofix
        debug.write_json("result.json", updated)
        debug.log_event("review_autofix_finished", **autofix)
        store.log_event(
            "review_autofix_finished",
            review_id=debug.review_id,
            status=autofix["status"],
            applied_segment_count=autofix["applied_segment_count"],
            failed_issue_count=autofix["failed_issue_count"],
        )
        return ReviewOutcome(
            run_dir=debug.run_dir,
            result=updated,
            usage=debug.load_usage() or {},
        )
