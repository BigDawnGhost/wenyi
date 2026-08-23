"""Review 领域纯模型：证据索引与运行目录，不含编排状态机。

``pipeline.review_workflow`` 负责调度；``agents.review_loop`` 只依赖本包的
数据结构与存储，不得反向依赖编排层。
"""

from .evidence import BookEvidenceIndex, SegmentRef
from .run_store import ReviewOutcome, ReviewRunStore, review_candidate_id

__all__ = [
    "BookEvidenceIndex",
    "SegmentRef",
    "ReviewOutcome",
    "ReviewRunStore",
    "review_candidate_id",
]
