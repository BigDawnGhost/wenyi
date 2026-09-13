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
