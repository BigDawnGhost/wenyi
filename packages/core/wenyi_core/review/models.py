"""Pure review outcomes, stable identities and evidence locations."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

CONSISTENCY_KINDS = {"term", "pronoun", "fixed"}


def review_candidate_id(
    chapter: int,
    chunk_base: int,
    ordinal: int,
    review_round: int | None = None,
) -> str:
    """Generate deterministic candidate IDs shared by initial snapshots and the agent protocol."""
    prefix = f"r{review_round}-" if review_round is not None else ""
    return f"{prefix}ch{chapter}-base{chunk_base}-candidate{ordinal}"


@dataclass(frozen=True)
class ReviewOutcome:
    """A completed review's result and directory."""

    run_dir: str
    result: dict[str, Any]
    usage: dict[str, Any]

    @property
    def issues(self) -> list[dict[str, Any]]:
        """Return issues remaining after blind rechecks."""
        return list(self.result.get("issues") or [])

    @property
    def changes(self) -> list[dict[str, Any]]:
        """Return collapsed final shadow-change recommendations."""
        return list(self.result.get("changes") or [])


@dataclass(frozen=True)
class ReviewLoopOutcome:
    """The verified result of one review leaf block."""

    issues: list[dict[str, Any]]
    dismissed: list[dict[str, Any]]
    fallback_reason: str = ""


def clean_text(value: Any) -> str:
    """Accept strings only and strip surrounding whitespace."""
    return value.strip() if isinstance(value, str) else ""


def normalize_value(value: str) -> str:
    """Normalize compatibility characters, width and case when comparing proposed values."""
    return unicodedata.normalize("NFKC", value).casefold().strip()


def _identity_text(value: Any) -> str:
    """Normalize issue identity whitespace and compatibility forms to reduce variation across
    rounds.
    """
    return re.sub(r"\s+", " ", normalize_value(clean_text(value)))


def review_issue_key(issue: dict[str, Any]) -> str:
    """Generate a stable issue key across review rounds, independent of temporary issue_id
    values.
    """
    consistency = issue.get("consistency")
    consistency_key = (
        _identity_text(consistency.get("key")) if isinstance(consistency, dict) else ""
    )
    subject = consistency_key or _identity_text(issue.get("detail"))
    payload = json.dumps(
        [
            issue.get("chapter"),
            issue.get("index"),
            _identity_text(issue.get("type")),
            subject,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"review-issue-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:20]}"


@dataclass(frozen=True)
class SegmentRef:
    """A reviewable paragraph with its stable book position."""

    global_ordinal: int
    chapter: int
    text_index: int
    segment_index: int
    source: str
    target: str
    baseline_target: str
    target_origin: str
    chapter_title: str

    @property
    def ref(self) -> str:
        """Return a stable ID for diagnostics and agent evidence references."""
        return f"ch{self.chapter}:text{self.text_index}:seg{self.segment_index}"

    def compact(self) -> dict[str, Any]:
        """Serialize as an evidence payload."""
        limit = 4000
        payload = {
            "ref": self.ref,
            "chapter": self.chapter,
            "text_index": self.text_index,
            "segment_index": self.segment_index,
            "chapter_title": self.chapter_title,
            "source": self.source[:limit],
            "target": self.target[:limit],
            "target_origin": self.target_origin,
            "source_truncated": len(self.source) > limit,
            "target_truncated": len(self.target) > limit,
        }
        if self.target_origin == "shadow_override":
            payload.update(
                {
                    "baseline_target": self.baseline_target[:limit],
                    "baseline_target_truncated": len(self.baseline_target) > limit,
                }
            )
        return payload
