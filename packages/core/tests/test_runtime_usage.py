"""One coordinator owns the client checkpoint without constructing a pipeline or agents."""

from unittest.mock import Mock

import pytest
from wenyi_core.llm.usage import UsageSample, UsageTracker, empty_usage
from wenyi_core.pipeline.runtime_usage import UsageCoordinator
from wenyi_core.storage.file import FileStorage


class UsageSource:
    def __init__(self, tracker: UsageTracker):
        self.tracker = tracker

    def usage_summary(self) -> dict:
        return self.tracker.summary()


def record(tracker):
    tracker.record(
        "cheap", UsageSample(prompt_tokens=7, completion_tokens=3, total_tokens=10), "review.scan"
    )


@pytest.mark.parametrize("failure", ["prepare", "event"])
def test_checkpoint_advances_only_after_durable_prepare(tmp_path, monkeypatch, failure):
    tracker = UsageTracker()
    coordinator = UsageCoordinator(UsageSource(tracker))
    store = FileStorage(str(tmp_path))
    record(tracker)
    method = "prepare_usage_commit" if failure == "prepare" else "log_event"
    with monkeypatch.context() as patch:
        patch.setattr(store, method, Mock(side_effect=OSError("interrupted")))
        with pytest.raises(OSError, match="interrupted"):
            coordinator.flush(store, scope="test")
    record(tracker)
    assert coordinator.flush(store, scope="test")["totals"]["calls"] == 2
    assert coordinator.flush(store, scope="retry")["totals"]["calls"] == 2


def test_initial_checkpoint_excludes_previous_client_calls_and_creates_empty_review(tmp_path):
    tracker = UsageTracker()
    record(tracker)
    coordinator = UsageCoordinator(UsageSource(tracker))
    review = Mock(review_id="review-empty", load_usage=Mock(return_value=None))
    store = FileStorage(str(tmp_path))
    assert coordinator.flush(store, scope="review", review=review) == empty_usage()
    review.save_usage.assert_called_once_with(empty_usage())
    assert store.load_usage() is None
    record(tracker)
    assert coordinator.flush(store, scope="next")["totals"]["calls"] == 1
