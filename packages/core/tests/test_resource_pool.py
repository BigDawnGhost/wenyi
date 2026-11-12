"""CPU pool admission, stable ordering, bounded submission and spawn isolation."""

from __future__ import annotations

import multiprocessing
import os
import signal
import time
from concurrent.futures import Future
from types import SimpleNamespace

import pytest
from wenyi_core import resource_pool
from wenyi_core.resource_pool import ResourcePool


def _identify_worker(delay: float) -> tuple[int, float, str, bool]:
    time.sleep(delay)
    return (
        os.getpid(),
        delay,
        multiprocessing.get_start_method(),
        signal.getsignal(signal.SIGINT) == signal.SIG_IGN,
    )


def _fail_worker(value: int) -> int:
    raise ValueError(f"Invalid resource {value}")


def test_worker_budget_respects_size_cpu_affinity_and_daemon(monkeypatch):
    monkeypatch.setattr(resource_pool.os, "cpu_count", lambda: 12)
    monkeypatch.setattr(
        resource_pool.os, "sched_getaffinity", lambda _: set(range(12)), raising=False
    )
    monkeypatch.setattr(
        resource_pool.multiprocessing, "current_process", lambda: SimpleNamespace(daemon=False)
    )
    assert ResourcePool(753, 40 * 1024 * 1024).workers == 6
    assert ResourcePool(12, 2 * 1024 * 1024).workers == 6
    assert ResourcePool(11, 2 * 1024 * 1024).workers == 1
    assert ResourcePool(12, 2 * 1024 * 1024 - 1).workers == 1
    assert ResourcePool(1, 40 * 1024 * 1024).workers == 1
    assert ResourcePool(753, 1024).workers == 1
    monkeypatch.setattr(resource_pool.os, "sched_getaffinity", lambda _: {0, 1})
    assert ResourcePool(753, 40 * 1024 * 1024).workers == 2
    monkeypatch.setattr(
        resource_pool.multiprocessing, "current_process", lambda: SimpleNamespace(daemon=True)
    )
    assert ResourcePool(753, 40 * 1024 * 1024).workers == 1


def test_interactive_entry_uses_serial_path(monkeypatch):
    monkeypatch.setattr(resource_pool.sys.modules["__main__"], "__file__", "<stdin>", raising=False)
    assert ResourcePool(753, 40 * 1024 * 1024).workers == 1


def test_small_workloads_never_construct_an_executor(monkeypatch):
    def unexpected_executor(**kwargs):
        pytest.fail("Small books must not spawn processes")

    monkeypatch.setattr(resource_pool, "ProcessPoolExecutor", unexpected_executor)
    with ResourcePool(2, 1024) as pool:
        assert list(pool.map(lambda value: value * 2, [3, 1, 2])) == [6, 2, 4]


def test_submission_is_bounded_and_shutdown_releases_admission(monkeypatch):
    submitted = []
    shutdown = []

    class Executor:
        def __init__(self, **kwargs):
            assert kwargs["max_workers"] == 6
            assert kwargs["mp_context"].get_start_method() == "spawn"

        def submit(self, operation, value):
            future = Future()
            future.set_result(operation(value))
            submitted.append(value)
            return future

        def shutdown(self, **kwargs):
            shutdown.append(kwargs)

    monkeypatch.setattr(resource_pool, "_worker_count", lambda *_: 6)
    monkeypatch.setattr(resource_pool, "ProcessPoolExecutor", Executor)
    with ResourcePool(100, 10_000_000) as pool:
        assert not resource_pool._POOL_ADMISSION.acquire(blocking=False)
        results = pool.map(abs, range(-100, 0))
        assert next(results) == 100
        assert len(submitted) == 12
        assert next(results) == 99
        assert len(submitted) == 13
        results.close()
    assert shutdown == [{"wait": True, "cancel_futures": True}]
    assert resource_pool._POOL_ADMISSION.acquire(blocking=False)
    resource_pool._POOL_ADMISSION.release()


def test_executor_start_failure_releases_admission(monkeypatch):
    def fail(**kwargs):
        raise OSError("No worker capacity")

    monkeypatch.setattr(resource_pool, "_worker_count", lambda *_: 6)
    monkeypatch.setattr(resource_pool, "ProcessPoolExecutor", fail)
    with pytest.raises(OSError, match="No worker capacity"):
        with ResourcePool(100, 10_000_000):
            pass
    assert resource_pool._POOL_ADMISSION.acquire(blocking=False)
    resource_pool._POOL_ADMISSION.release()


def test_spawned_work_is_ordered_isolated_and_errors_propagate(monkeypatch):
    monkeypatch.setattr(resource_pool, "_worker_count", lambda *_: 2)
    with ResourcePool(12, 3_000_000) as pool:
        results = list(pool.map(_identify_worker, [0.05, 0, 0.01, 0]))
        assert [result[1] for result in results] == [0.05, 0, 0.01, 0]
        assert all(
            result[0] != os.getpid() and result[2] == "spawn" and result[3] for result in results
        )
        with pytest.raises(ValueError, match="Invalid resource 7"):
            list(pool.map(_fail_worker, [7, 8]))
    assert not {result[0] for result in results} & {
        process.pid for process in multiprocessing.active_children()
    }
    assert resource_pool._POOL_ADMISSION.acquire(blocking=False)
    resource_pool._POOL_ADMISSION.release()
