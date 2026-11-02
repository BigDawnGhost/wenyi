"""Pin identical semantics for the Python and Rust file-lock backends."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time

import pytest
from wenyi_core.storage import locks
from wenyi_core.storage.locks import FileLockHandle, StorageBusyError, exclusive_file_lock


@pytest.fixture(params=["python", "rust"])
def backend(request, monkeypatch):
    """Run every case against both backends; the native one may be unavailable."""
    if request.param == "rust" and locks._native_library() is None:
        pytest.skip("wenyi-locks native library is not built")
    monkeypatch.setattr(locks, "_FORCED_BACKEND", request.param)
    return request.param


def test_blocking_acquire_waits_for_release(tmp_path, backend):
    path = tmp_path / ".run.lock"
    holder = FileLockHandle(str(path))
    holder.acquire()
    released = threading.Event()

    def release_later() -> None:
        time.sleep(0.3)
        holder.release()
        released.set()

    thread = threading.Thread(target=release_later)
    thread.start()
    try:
        started = time.monotonic()
        with exclusive_file_lock(str(path)):
            waited = time.monotonic() - started
    finally:
        thread.join(timeout=5)

    assert released.is_set()
    assert waited >= 0.25, f"expected to wait for the holder, waited {waited:.3f}s"


def test_non_blocking_contention_reports_busy(tmp_path, backend):
    path = tmp_path / ".run.lock"
    holder = FileLockHandle(str(path))
    holder.acquire()
    try:
        started = time.monotonic()
        with pytest.raises(StorageBusyError):
            with exclusive_file_lock(str(path), blocking=False):
                pass
        assert time.monotonic() - started < 1.0
    finally:
        holder.release()


def test_cross_process_exclusion(tmp_path, backend):
    path = tmp_path / ".run.lock"
    program = textwrap.dedent(
        """
        import sys, time
        from wenyi_core.storage.locks import FileLockHandle

        handle = FileLockHandle(sys.argv[1])
        handle.acquire()
        print("locked", flush=True)
        time.sleep(1.0)
        handle.release()
        """
    )
    # The child must use the same backend as the parent, or the test would
    # compare two different implementations instead of one contract.
    environment = dict(os.environ)
    environment["WENYI_LOCKS_BACKEND"] = backend
    child = subprocess.Popen(
        [sys.executable, "-c", program, str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    try:
        assert child.stdout is not None
        ready = child.stdout.readline().strip()
        assert ready == "locked", child.stderr.read() if child.stderr else ready
        with pytest.raises(StorageBusyError):
            with exclusive_file_lock(str(path), blocking=False):
                pass
    finally:
        child.wait(timeout=30)

    # The child released the lock on exit, so the blocking acquisition succeeds.
    with exclusive_file_lock(str(path)):
        pass


def test_exception_inside_context_releases(tmp_path, backend):
    path = tmp_path / ".run.lock"
    with pytest.raises(RuntimeError):
        with exclusive_file_lock(str(path)):
            raise RuntimeError("boom")

    with exclusive_file_lock(str(path), blocking=False):
        pass
