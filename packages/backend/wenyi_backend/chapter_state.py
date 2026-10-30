"""Presentation of immutable review results after manual chapter changes."""

from datetime import datetime, timezone
from typing import Any


def chapter_review_state(
    review: dict[str, Any] | None, meta: dict[str, Any], fallback_status: str = "pending"
) -> tuple[str, bool]:
    def timestamp(value: Any) -> float:
        if not isinstance(value, str):
            return 0.0
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
        except ValueError:
            return 0.0

    review = review or {}
    reviewed = timestamp(
        review.get("finished_at") or review.get("interrupted_at") or review.get("started_at")
    )
    edited = timestamp(meta.get("review_invalidated_at"))
    manual = timestamp(meta.get("manual_reviewed_at"))
    if edited and edited >= max(reviewed, manual):
        return "pending", False
    if manual and manual >= max(reviewed, edited):
        return "completed", False
    if review:
        return review.get("status") or "pending", True
    return fallback_status, False
