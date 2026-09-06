"""Pure review models, evidence indices and run storage, without the orchestration state
machine.
pipeline.review_workflow schedules work. agents.review_loop may depend on this package's
data/storage interfaces but must not depend on the pipeline.
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
