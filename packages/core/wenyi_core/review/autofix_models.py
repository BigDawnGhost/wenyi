"""Pure Autofix publication data and deterministic candidate records."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, TypedDict


class _RecordFields(TypedDict):
    record_id: str
    chapter: int
    index: int
    segment_ref: str
    origin: str
    before: str
    before_hash: str
    after: str | None
    after_hash: str | None
    issue_keys: list[str]
    issue_ids: list[str]
    review_result: str
    status: str


class AutofixRecord(_RecordFields, total=False):
    reason: str
    evidence_refs: list[str]
    source_change: dict[str, Any]
    related_issues: list[dict[str, Any]]
    artifacts: list[str]


class PublishLocation(TypedDict):
    chapter: int
    index: int
    segment_ref: str
    before: str
    before_hash: str
    target: str
    target_hash: str
    record_ids: list[str]
    status: str
    alignment_status: str


class AutofixPlan(TypedDict):
    version: int
    inference: dict[str, Any]
    review_id: str
    status: str
    reviewed_content_digest: str | None
    records: list[AutofixRecord]
    locations: list[PublishLocation]


def text_hash(text: str) -> str:
    """Return the UTF-8 SHA-256 used by the optimistic autofix publication protocol."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def integer_index(value: Any) -> int | None:
    """Accept integer positions excluding booleans and narrow the type explicitly."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@dataclass
class AutofixCandidates:
    """Accumulate records in stable source order without changing formal chapters."""

    records: list[AutofixRecord] = field(default_factory=list)
    overrides: dict[tuple[int, int], str] = field(default_factory=dict)
    issue_group_count: int = 0

    def add(
        self,
        *,
        chapter: int,
        index: int,
        segment_ref: str,
        origin: str,
        before: str,
        after: str | None,
        issue_keys: list[str] | None = None,
        issue_ids: list[str] | None = None,
        review_result: str = "",
        status: str = "planned",
        reason: str = "",
        evidence_refs: list[str] | None = None,
        source_change: dict[str, Any] | None = None,
        related_issues: list[dict[str, Any]] | None = None,
        artifacts: list[str] | None = None,
    ) -> AutofixRecord:
        record: AutofixRecord = {
            "record_id": f"autofix-{len(self.records) + 1:05d}",
            "chapter": chapter,
            "index": index,
            "segment_ref": segment_ref,
            "origin": origin,
            "before": before,
            "before_hash": text_hash(before),
            "after": after,
            "after_hash": text_hash(after) if isinstance(after, str) else None,
            "issue_keys": sorted({item for item in issue_keys or [] if item}),
            "issue_ids": sorted({item for item in issue_ids or [] if item}),
            "review_result": review_result,
            "status": status,
        }
        if reason:
            record["reason"] = reason
        if evidence_refs:
            record["evidence_refs"] = sorted(set(evidence_refs))
        if source_change is not None:
            record["source_change"] = dict(source_change)
        if related_issues:
            record["related_issues"] = [dict(issue) for issue in related_issues]
        if artifacts:
            record["artifacts"] = sorted(set(artifacts))
        self.records.append(record)
        return record
