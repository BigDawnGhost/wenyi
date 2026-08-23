"""兼容入口：证据索引已迁至 ``trans_novel.review.evidence``。"""

from __future__ import annotations

from ..review.evidence import BookEvidenceIndex, SegmentRef

__all__ = ["BookEvidenceIndex", "SegmentRef"]
