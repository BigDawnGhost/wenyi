"""兼容入口：Review 运行记录已迁至 ``trans_novel.review.run_store``。"""

from __future__ import annotations

from ..review.run_store import ReviewOutcome, ReviewRunStore, review_candidate_id

__all__ = ["ReviewOutcome", "ReviewRunStore", "review_candidate_id"]
