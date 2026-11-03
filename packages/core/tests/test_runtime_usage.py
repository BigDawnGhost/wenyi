"""One coordinator owns the client checkpoint without constructing a pipeline or agents."""

import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import contextmanager
from unittest.mock import Mock

import pytest
from wenyi_core.llm.usage import UsageSample, UsageTracker, empty_usage
from wenyi_core.pipeline.runtime_usage import UsageCoordinator
from wenyi_core.storage.file import FileStorage
from wenyi_core.storage.sqlite import SqliteStorage


class UsageSource:
    def __init__(self, tracker: UsageTracker):
        self.tracker = tracker

    def usage_summary(self) -> dict:
        return self.tracker.summary()


def record(tracker):
    tracker.record(
        "cheap", UsageSample(prompt_tokens=7, completion_tokens=3, total_tokens=10), "review.scan"
    )


@pytest.fixture(params=[FileStorage, SqliteStorage], ids=["file", "sqlite"])
def store(request, tmp_path):
    storage = request.param(str(tmp_path))
    yield storage
    if isinstance(storage, SqliteStorage):
        storage.close_all()
    else:
        storage.close()


@pytest.mark.parametrize("failure", ["prepare", "recover", "event"])
def test_checkpoint_advances_only_after_durable_prepare(store, monkeypatch, failure):
    tracker = UsageTracker()
    coordinator = UsageCoordinator(UsageSource(tracker))
    record(tracker)
    with monkeypatch.context() as patch:
        if failure == "recover":
            recover = store.recover_usage
            calls = 0

            def fail_publication():
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("interrupted")
                recover()

            patch.setattr(store, "recover_usage", fail_publication)
        else:
            method = "prepare_usage_commit" if failure == "prepare" else "log_event"
            patch.setattr(store, method, Mock(side_effect=OSError("interrupted")))
        with pytest.raises(OSError, match="interrupted"):
            coordinator.flush(store, scope="test")
    if failure == "recover":
        assert store.read_artifact("usage-pending.json") is not None
    record(tracker)
    assert coordinator.flush(store, scope="test")["totals"]["calls"] == 2
    assert coordinator.flush(store, scope="retry")["totals"]["calls"] == 2


def test_checkpoint_does_not_advance_when_sqlite_prepare_transaction_rolls_back(
    tmp_path, monkeypatch
):
    store = SqliteStorage(str(tmp_path))
    tracker = UsageTracker()
    coordinator = UsageCoordinator(UsageSource(tracker))
    record(tracker)
    state_lock = store.state_lock

    @contextmanager
    def failed_commit():
        outer = store._conn_local.depth == 0
        with state_lock():
            yield
            if outer:
                raise OSError("commit failed")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(store, "state_lock", failed_commit)
            with pytest.raises(OSError, match="commit failed"):
                coordinator.flush(store, scope="test")
        assert store.read_artifact("usage-pending.json") is None
        assert store.load_usage() is None
        assert coordinator.flush(store, scope="retry")["totals"]["calls"] == 1
        assert coordinator.flush(store, scope="retry")["totals"]["calls"] == 1
    finally:
        store.close_all()


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


def test_concurrent_flushes_bill_every_increment_once(store):
    """Two racing flushes lose no increment from a cumulative ledger.

    Both coordinators read the ledger before either commits. Without a lock
    around the read-merge-write both publish the same baseline, so one
    increment silently disappears from a ledger that is meant to accumulate.
    """
    trackers = [UsageTracker(), UsageTracker()]
    coordinators = [UsageCoordinator(UsageSource(tracker)) for tracker in trackers]
    for tracker in trackers:
        record(tracker)

    original_load = store.load_usage
    reads = 0
    read_guard = threading.Lock()
    both_read = threading.Barrier(2, timeout=1)

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


@pytest.mark.parametrize("shared_coordinator", [False, True])
def test_flushes_interleaving_after_prepare_do_not_replay_a_delta(
    store, monkeypatch, shared_coordinator
):
    trackers = [UsageTracker(), UsageTracker()]
    coordinators = [UsageCoordinator(UsageSource(tracker)) for tracker in trackers]
    for tracker in trackers:
        record(tracker)
    if shared_coordinator:
        coordinators[1] = coordinators[0]
    prepared, publish, second_started = threading.Event(), threading.Event(), threading.Event()
    local = threading.local()
    state_lock = store.state_lock

    @contextmanager
    def pause_after_prepare():
        depth = getattr(local, "depth", 0)
        local.depth = depth + 1
        try:
            with state_lock():
                yield
        finally:
            local.depth = depth
        if depth == 0 and getattr(local, "first", False) and not prepared.is_set():
            prepared.set()
            assert publish.wait(5)

    def flush(index):
        local.first = index == 0
        if index == 1:
            second_started.set()
        return coordinators[index].flush(store, scope=f"thread-{index}")

    monkeypatch.setattr(store, "state_lock", pause_after_prepare)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(flush, 0)
        assert prepared.wait(5)
        second = pool.submit(flush, 1)
        try:
            assert second_started.wait(5)
            if shared_coordinator:
                with pytest.raises(TimeoutError):
                    second.result(timeout=0.1)
            else:
                # The second coordinator recovers the first committed journal
                # before preparing its own, even before the first advances its checkpoint.
                assert second.result(timeout=5)["totals"]["calls"] == 2
        finally:
            publish.set()
        first.result(timeout=5)
        second.result(timeout=5)
    expected = 1 if shared_coordinator else 2
    assert store.load_usage()["totals"]["calls"] == expected
    assert coordinators[0].flush(store, scope="retry")["totals"]["calls"] == expected
