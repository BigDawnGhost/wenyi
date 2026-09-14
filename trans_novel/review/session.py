"""Pure shadow-session state, stable snapshots and unresolved-issue bookkeeping."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ReviewRoundResult:
    """Deterministic result of one whole-book shadow review and conflict arbitration."""

    issues: list[dict[str, Any]]
    pre_arbitration_issues: list[dict[str, Any]]
    arbitration_superseded: list[dict[str, Any]]
    conflict_groups: list[dict[str, Any]]
    residual_conflicts: list[dict[str, Any]]
    fallback_agent_count: int


def review_overlay_digest(
    chapters,
    overrides: Mapping[tuple[int, int], str],
) -> str:
    """Fingerprint effective shadow text to detect no progress and A/B oscillation."""
    payload = [
        (
            chapter.index,
            text_index,
            overrides.get((chapter.index, text_index), segment.target or ""),
        )
        for chapter in chapters
        for text_index, segment in enumerate(chapter.text_segments)
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def content_digest(chapters) -> str:
    """Hash the formal body text actually read by this review."""
    payload = [
        (
            chapter.index,
            text_index,
            segment.index,
            segment.anchor or "",
            segment.kind,
            segment.source,
            segment.target or "",
        )
        for chapter in chapters
        for text_index, segment in enumerate(chapter.text_segments)
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ReviewPolicy:
    """Existing confirmation and revision limits, independent of configuration storage."""

    fix_loop: bool
    max_fix_rounds: int
    clean_confirmations: int

    @property
    def required_clean(self) -> int:
        return self.clean_confirmations if self.fix_loop else 1

    @property
    def max_review_rounds(self) -> int:
        return (self.max_fix_rounds + 1) * self.required_clean if self.fix_loop else 1


@dataclass(frozen=True)
class ReviewDecision:
    """Next step plus the exact round summary and optional clean-progress update."""

    action: str
    summary: dict[str, Any]
    clean_progress: int | None = None


@dataclass(frozen=True)
class ReviewFixPlan:
    """Validated shadow candidates awaiting the existing artifact/acceptance boundary."""

    patches: list[dict[str, Any]]
    failures: list[dict[str, Any]]
    applicable: list[dict[str, Any]]
    overrides: dict[tuple[int, int], str]
    candidate_digest: str
    issue_keys_by_id: dict[str, str]


@dataclass
class ReviewSessionState:
    """Own mutable shadow state for one Review, independently of files and model clients."""

    target_overrides: dict[tuple[int, int], str] = field(default_factory=dict)
    seen_overlays: set[str] = field(default_factory=set)
    patch_records: list[dict[str, Any]] = field(default_factory=list)
    active_patches: dict[tuple[int, int], dict[str, Any]] = field(default_factory=dict)
    fix_failures: list[dict[str, Any]] = field(default_factory=list)
    blocked_issues: dict[str, dict[str, Any]] = field(default_factory=dict)
    round_summaries: list[dict[str, Any]] = field(default_factory=list)
    latest: ReviewRoundResult | None = None
    clean_streak: int = 0
    fix_rounds: int = 0
    termination: str = "not_started"

    def register_blocked(
        self,
        issues: list[dict[str, Any]],
        failures: list[dict[str, Any]],
    ) -> None:
        """Retain fixer failures by stable issue key so later reviewer omissions cannot
        create false clean results.
        """
        by_id = {
            str(issue["issue_id"]): issue
            for issue in issues
            if isinstance(issue.get("issue_id"), str)
        }
        for failure in failures:
            failure_ids = failure.get("issue_ids")
            if not isinstance(failure_ids, list):
                failure_id = failure.get("issue_id")
                failure_ids = [failure_id] if isinstance(failure_id, str) else []
            for issue_id in failure_ids:
                issue = by_id.get(str(issue_id))
                if issue is None:
                    continue
                issue_key = issue.get("issue_key")
                if not isinstance(issue_key, str) or not issue_key:
                    continue
                self.blocked_issues[issue_key] = {
                    **dict(issue),
                    "fix_failure": {
                        "status": failure.get("status"),
                        "reason": failure.get("reason"),
                        "review_round": failure.get("review_round"),
                    },
                }

    def effective_issues(self, current: ReviewRoundResult) -> list[dict[str, Any]]:
        """Merge current issues and historical unfixed issues into public unresolved issues
        in book order.
        """
        combined = {
            str(issue["issue_key"]): dict(issue)
            for issue in current.issues
            if isinstance(issue.get("issue_key"), str)
        }
        for issue_key, blocked in self.blocked_issues.items():
            current_issue = combined.get(issue_key)
            if current_issue is None:
                combined[issue_key] = dict(blocked)
                continue
            fix_failure = blocked.get("fix_failure")
            if isinstance(fix_failure, dict):
                current_issue["fix_failure"] = dict(fix_failure)
        return sorted(
            combined.values(),
            key=lambda issue: (
                issue.get("chapter", -1),
                issue.get("index", -1),
                issue.get("review_round", -1),
                issue.get("issue_id", ""),
            ),
        )

    def after_scan(
        self,
        current: ReviewRoundResult,
        review_round: int,
        overlay_digest: str,
        policy: ReviewPolicy,
    ) -> ReviewDecision:
        """Apply blind-recheck evidence and choose stop, confirmation or shadow revision."""
        current_issue_keys = {
            str(issue["issue_key"])
            for issue in current.issues
            if isinstance(issue.get("issue_key"), str)
        }
        for patch_record in self.active_patches.values():
            if patch_record.get("round", review_round) >= review_round:
                continue
            covered_issue_keys = {
                str(issue_key)
                for issue_key in patch_record.get("issue_keys", [])
                if isinstance(issue_key, str)
            }
            rereported = sorted(covered_issue_keys & current_issue_keys)
            not_rereported = sorted(covered_issue_keys - current_issue_keys)
            for issue_key in not_rereported:
                self.blocked_issues.pop(issue_key, None)
            patch_record["rereported_issue_keys"] = rereported
            patch_record["not_rereported_issue_keys"] = not_rereported
            if rereported:
                patch_record["status"] = "needs_revision"
                patch_record["failed_review_round"] = review_round
            else:
                if patch_record.get("status") != "not_rereported":
                    patch_record["not_rereported_in_round"] = review_round
                patch_record["status"] = "not_rereported"

        round_summary: dict[str, Any] = {
            "review_round": review_round,
            "overlay_digest": overlay_digest,
            "override_count": len(self.target_overrides),
            "issue_count": len(current.issues),
            "conflict_count": len(current.conflict_groups),
            "unresolved_conflict_count": len(current.residual_conflicts),
            "fallback_agent_count": current.fallback_agent_count,
            "clean_streak_before": self.clean_streak,
            "blocked_issue_count": len(self.blocked_issues),
        }

        if not current.issues:
            if self.blocked_issues:
                self.clean_streak = 0
                self.termination = "unresolved_fixes"
                round_summary.update(
                    clean_streak_after=0, patch_count=0, termination=self.termination
                )
                return ReviewDecision("stop", round_summary, 0)
            self.clean_streak += 1
            round_summary["clean_streak_after"] = self.clean_streak
            round_summary["patch_count"] = 0
            if self.clean_streak >= policy.required_clean:
                self.termination = "clean_confirmed"
                round_summary["termination"] = self.termination
            return ReviewDecision(
                "stop" if self.termination == "clean_confirmed" else "continue",
                round_summary,
                self.clean_streak,
            )
        clean_progress = 0 if self.clean_streak else None
        self.clean_streak = 0
        round_summary["clean_streak_after"] = 0
        if not policy.fix_loop:
            self.termination = "issues_reported"
        elif self.fix_rounds >= policy.max_fix_rounds:
            self.termination = "max_rounds"
        else:
            return ReviewDecision("fix", round_summary, clean_progress)
        round_summary["patch_count"] = 0
        round_summary["termination"] = self.termination
        return ReviewDecision("stop", round_summary, clean_progress)

    def prepare_fix(
        self,
        loaded,
        latest: ReviewRoundResult,
        patches: list[dict[str, Any]],
        failures: list[dict[str, Any]],
        current_targets: Mapping[tuple[int, int], str],
        review_round: int,
        round_summary: dict[str, Any],
    ) -> ReviewFixPlan | None:
        """Validate candidate bases and record blocked failures before artifacts are saved."""
        self.fix_failures.extend(
            [
                {
                    **failure,
                    "review_round": review_round,
                }
                for failure in failures
            ]
        )
        self.register_blocked(
            latest.issues,
            [
                {
                    **failure,
                    "review_round": review_round,
                }
                for failure in failures
            ],
        )
        round_summary["patch_count"] = len(patches)

        if not patches:
            self.termination = "no_progress"
            round_summary["fix_failure_count"] = len(failures)
            round_summary["blocked_issue_count"] = len(self.blocked_issues)
            round_summary["termination"] = self.termination
            return None
        issue_keys_by_id = {
            str(issue["issue_id"]): str(issue["issue_key"])
            for issue in latest.issues
            if isinstance(issue.get("issue_id"), str) and isinstance(issue.get("issue_key"), str)
        }
        candidate_overrides = dict(self.target_overrides)
        applicable: list[dict[str, Any]] = []
        hash_failures: list[dict[str, Any]] = []
        for patch in patches:
            location = (patch["chapter"], patch["index"])
            current = current_targets.get(location)
            if (
                current is None
                or hashlib.sha256(current.encode("utf-8")).hexdigest() != patch["before_hash"]
            ):
                failure = {
                    "patch_id": patch["patch_id"],
                    "issue_ids": list(patch["issue_ids"]),
                    "chapter": patch["chapter"],
                    "index": patch["index"],
                    "status": "failed",
                    "reason": "before_hash_changed",
                    "review_round": review_round,
                }
                self.fix_failures.append(failure)
                failures.append(failure)
                hash_failures.append(failure)
                continue
            candidate_overrides[location] = patch["after"]
            applicable.append(patch)

        self.register_blocked(
            latest.issues,
            hash_failures,
        )
        round_summary["fix_failure_count"] = len(failures)
        round_summary["blocked_issue_count"] = len(self.blocked_issues)
        candidate_digest = review_overlay_digest(
            loaded,
            candidate_overrides,
        )

        return ReviewFixPlan(
            patches, failures, applicable, candidate_overrides, candidate_digest, issue_keys_by_id
        )

    def after_fix(
        self, plan: ReviewFixPlan, overlay_digest: str, round_summary: dict[str, Any]
    ) -> bool:
        """Reject no-progress/cyclic overlays or accept the next shadow state after artifacts save."""
        applicable = plan.applicable
        candidate_digest = plan.candidate_digest
        candidate_overrides = plan.overrides
        issue_keys_by_id = plan.issue_keys_by_id
        if not applicable or candidate_digest == overlay_digest:
            self.termination = "no_progress"
            round_summary["termination"] = self.termination
            return False
        if candidate_digest in self.seen_overlays:
            self.termination = "cycle_detected"
            for patch in applicable:
                record = {
                    **dict(patch),
                    "issue_keys": sorted(
                        {
                            issue_keys_by_id[issue_id]
                            for issue_id in patch["issue_ids"]
                            if issue_id in issue_keys_by_id
                        }
                    ),
                    "status": "rejected_cycle",
                }
                self.patch_records.append(record)
            round_summary["termination"] = self.termination
            return False

        self.fix_rounds += 1
        for patch in applicable:
            location = (patch["chapter"], patch["index"])
            previous = self.active_patches.get(location)
            record = dict(patch)
            record["issue_keys"] = sorted(
                {
                    issue_keys_by_id[issue_id]
                    for issue_id in patch["issue_ids"]
                    if issue_id in issue_keys_by_id
                }
            )
            if previous is not None:
                previous["status"] = "superseded"
                previous["superseded_by"] = patch["patch_id"]
            self.patch_records.append(record)
            self.active_patches[location] = record
        self.target_overrides = candidate_overrides
        self.seen_overlays.add(candidate_digest)
        round_summary["fix_round"] = self.fix_rounds
        return True
