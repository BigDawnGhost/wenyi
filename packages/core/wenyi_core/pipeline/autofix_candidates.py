"""Prepare Autofix candidates from a frozen chapter snapshot in stable source order."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from ..agents.review_fixer import ProvisionalPatch
from ..config import Config
from ..glossary.store import GlossaryTerm
from ..ingest.models import Chapter
from ..llm.base import LLMClient
from ..review.autofix_models import AutofixCandidates, integer_index
from ..review.evidence import BookEvidenceIndex
from ..review.models import ReviewOutcome
from ..review.run_store import ReviewRunStore
from .autofix_verification import AutofixVerification

ProgressFn = Callable[[int, int, str], None]


class AutofixCandidateService:
    def __init__(self, config: Config, client: LLMClient, style_brief: Callable[[dict], str]):
        self.config = config
        self.client = client
        self.style_brief = style_brief

    def prepare(
        self,
        chapters: list[Chapter],
        analysis: dict[str, Any],
        outcome: ReviewOutcome,
        all_terms: list[GlossaryTerm],
        debug: ReviewRunStore,
        *,
        progress: ProgressFn | None = None,
    ) -> AutofixCandidates:
        chapters_by_index = {chapter.index: chapter for chapter in chapters}
        candidates = AutofixCandidates()
        overrides = candidates.overrides

        add_record = candidates.add

        # Overlay all changes first. Explicit autofix does not apply another review_result filter;
        # retain original status values in the index for later interpretation.
        def location_key(item: dict[str, Any]) -> tuple[int, int]:
            chapter = integer_index(item.get("chapter"))
            index = integer_index(item.get("index"))
            return (chapter if chapter is not None else -1, index if index is not None else -1)

        changes = sorted(
            (dict(change) for change in outcome.changes if isinstance(change, dict)),
            key=location_key,
        )
        for change in changes:
            chapter_index = integer_index(change.get("chapter"))
            text_index = integer_index(change.get("index"))
            suggested = change.get("suggested_target")
            chapter = chapters_by_index.get(chapter_index) if chapter_index is not None else None
            if (
                chapter is None
                or text_index is None
                or not 0 <= text_index < len(chapter.text_segments)
                or not isinstance(suggested, str)
                or not suggested.strip()
            ):
                add_record(
                    chapter=chapter_index if chapter_index is not None else -1,
                    index=text_index if text_index is not None else -1,
                    segment_ref="",
                    origin="change",
                    before="",
                    after=None,
                    issue_keys=[
                        str(key) for key in change.get("issue_keys", []) if isinstance(key, str)
                    ],
                    review_result=str(change.get("review_result") or ""),
                    status="failed",
                    reason="invalid_change",
                    source_change=change,
                )
                continue
            segment = chapter.text_segments[text_index]
            location = (chapter.index, text_index)
            before = overrides.get(location, segment.target or "")
            after = suggested
            add_record(
                chapter=chapter.index,
                index=text_index,
                segment_ref=f"ch{chapter.index}:text{text_index}:seg{segment.index}",
                origin="change",
                before=before,
                after=after,
                issue_keys=[
                    str(key) for key in change.get("issue_keys", []) if isinstance(key, str)
                ],
                review_result=str(change.get("review_result") or ""),
                status="not_applied_no_net_change" if after == before else "planned",
                reason="unchanged_target" if after == before else "",
                source_change=change,
            )
            if after != before:
                overrides[location] = after

        evidence = BookEvidenceIndex(
            chapters,
            all_terms,
            analysis,
            target_overrides=overrides,
        )
        raw_issues = debug.load_json("rounds/final/unresolved_issues.json")
        if not isinstance(raw_issues, list):
            raw_issues = outcome.issues
        issues = [dict(issue) for issue in raw_issues if isinstance(issue, dict)]
        grouped: dict[tuple[int, int], list[dict[str, Any]]] = {}
        for issue in issues:
            chapter_index = issue.get("chapter")
            text_index = issue.get("index")
            if (
                isinstance(chapter_index, int)
                and not isinstance(chapter_index, bool)
                and isinstance(text_index, int)
                and not isinstance(text_index, bool)
                and chapter_index in chapters_by_index
                and 0 <= text_index < len(chapters_by_index[chapter_index].text_segments)
            ):
                issue.setdefault("issue_id", issue.get("issue_key"))
                grouped.setdefault((chapter_index, text_index), []).append(issue)
                continue
            add_record(
                chapter=(
                    chapter_index
                    if isinstance(chapter_index, int) and not isinstance(chapter_index, bool)
                    else -1
                ),
                index=(
                    text_index
                    if isinstance(text_index, int) and not isinstance(text_index, bool)
                    else -1
                ),
                segment_ref="",
                origin="final_issue_fix",
                before="",
                after=None,
                issue_keys=[str(issue.get("issue_key") or "")],
                issue_ids=[str(issue.get("issue_id") or issue.get("issue_key") or "")],
                status="failed",
                reason="invalid_issue_location",
                related_issues=[issue],
            )

        jobs = sorted(grouped.items())
        if progress and jobs:
            progress(0, len(jobs), "Final automatic revision")
        verification = AutofixVerification(
            self.config,
            self.client,
            evidence,
            debug,
            all_terms,
            analysis,
            outcome,
            self.style_brief,
        )
        fixer_round = verification.fixer_round
        fix_job = verification.fix_job
        workers = min(max(1, self.config.pipeline.review_concurrency), len(jobs))
        if workers <= 1:
            fixed = []
            for done, job in enumerate(jobs, start=1):
                fixed.append(fix_job(job))
                if progress:
                    progress(done, len(jobs), "Final automatic revision")
        else:
            ordered: list[dict[str, Any] | None] = [None] * len(jobs)
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(fix_job, job): position for position, job in enumerate(jobs)
                }
                for done, future in enumerate(as_completed(futures), start=1):
                    ordered[futures[future]] = future.result()
                    if progress:
                        progress(done, len(jobs), "Final automatic revision")
            fixed = [item for item in ordered if item is not None]

        for fixed_result in fixed:
            location = fixed_result["location"]
            location_issues = fixed_result["original_issues"]
            verified_issues = fixed_result["verified_issues"]
            dismissed = fixed_result["dismissed"]
            patch: ProvisionalPatch | None = fixed_result["patch"]
            reason = str(fixed_result.get("reason") or "")
            current = evidence.segment_ref(*location)
            before = current.target if current is not None else ""
            agent_artifact = (
                f"agents/r{fixer_round}-chunk-ch{location[0]}-base{location[1]}-n1.json"
            )
            fixer_artifact = f"autofix/fixers/ch{location[0]}-text{location[1]}.json"
            if dismissed:
                add_record(
                    chapter=location[0],
                    index=location[1],
                    segment_ref=current.ref if current is not None else "",
                    origin="final_issue_review",
                    before=before,
                    after=None,
                    issue_keys=[
                        str(issue.get("issue_key") or "")
                        for issue in dismissed
                        if str(issue.get("issue_key") or "")
                    ],
                    issue_ids=[
                        str(issue.get("issue_id") or issue.get("issue_key") or "")
                        for issue in dismissed
                        if str(issue.get("issue_id") or issue.get("issue_key") or "")
                    ],
                    status="dismissed",
                    reason="dismissed_by_review_agent_loop",
                    related_issues=dismissed,
                    artifacts=[agent_artifact],
                )
            if fixed_result.get("status") == "dismissed":
                continue
            if patch is None:
                add_record(
                    chapter=location[0],
                    index=location[1],
                    segment_ref=current.ref if current is not None else "",
                    origin="final_issue_fix",
                    before=before,
                    after=None,
                    issue_keys=[
                        str(issue.get("issue_key") or "")
                        for issue in (verified_issues or location_issues)
                        if str(issue.get("issue_key") or "")
                    ],
                    issue_ids=[
                        str(issue.get("issue_id") or issue.get("issue_key") or "")
                        for issue in (verified_issues or location_issues)
                        if str(issue.get("issue_id") or issue.get("issue_key") or "")
                    ],
                    status="failed",
                    reason=reason or "review_fixer_failed",
                    related_issues=verified_issues or location_issues,
                    artifacts=[
                        agent_artifact,
                        *([fixer_artifact] if verified_issues else []),
                    ],
                )
                continue
            add_record(
                chapter=location[0],
                index=location[1],
                segment_ref=patch.segment_ref,
                origin="final_issue_fix",
                before=patch.before,
                after=patch.after,
                issue_keys=[
                    str(issue.get("issue_key") or "")
                    for issue in verified_issues
                    if str(issue.get("issue_key") or "")
                ],
                issue_ids=list(patch.issue_ids),
                status="planned",
                reason="confirmed_by_review_agent_loop",
                evidence_refs=[
                    str(ref)
                    for issue in verified_issues
                    for ref in issue.get("evidence_refs", [])
                    if isinstance(ref, str)
                ],
                related_issues=verified_issues,
                artifacts=[agent_artifact, fixer_artifact],
            )
            overrides[location] = patch.after

        candidates.issue_group_count = len(jobs)
        return candidates
