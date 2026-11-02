"""One coordinator owns the client checkpoint without constructing a pipeline or agents."""

import threading
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


def test_concurrent_flushes_bill_every_increment_once(tmp_path):
    """Two racing flushes lose no increment from a cumulative ledger.

    Both coordinators read the ledger before either commits. Without a lock
    around the read-merge-write both publish the same baseline, so one
    increment silently disappears from a ledger that is meant to accumulate.
    """
    store = FileStorage(str(tmp_path))
    trackers = [UsageTracker(), UsageTracker()]
    coordinators = [UsageCoordinator(UsageSource(tracker)) for tracker in trackers]
    for tracker in trackers:
        record(tracker)

    original_load = store.load_usage
    reads = 0
    read_guard = threading.Lock()
    both_read = threading.Barrier(2, timeout=5)

    def delayed_load():
        nonlocal reads
        value = original_load()
        with read_guard:
            reads += 1
            first_round = reads <= 2
        if first_round:
            # Unlocked code reaches here from both threads at once; locked code
            # reaches it one at a time, so the barrier times out harmlessly.
            try:
                both_read.wait()
            except threading.BrokenBarrierError:
                pass
        return value

    store.load_usage = delayed_load
    errors: list[BaseException] = []

    def flush(index: int) -> None:
        try:
            coordinators[index].flush(store, scope=f"thread-{index}")
        except BaseException as error:  # noqa: BLE001 - asserted below
            errors.append(error)

    threads = [threading.Thread(target=flush, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors, errors
    assert store.load_usage()["totals"]["calls"] == 2
