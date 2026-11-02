"""Pin identical semantics for the Python and Rust file-lock backends."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time
from types import SimpleNamespace

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


@pytest.mark.skipif(
    os.name == "nt", reason="POSIX signal delivery; FFI cleanup is tested everywhere"
)
def test_blocking_acquire_can_be_interrupted(tmp_path, backend):
    path = tmp_path / ".run.lock"
    program = textwrap.dedent(
        """
        import os, signal, sys, threading, time
        from wenyi_core.storage.locks import exclusive_file_lock

        def interrupt():
            time.sleep(0.1)
            os.kill(os.getpid(), signal.SIGINT)

        threading.Thread(target=interrupt, daemon=True).start()
        try:
            with exclusive_file_lock(sys.argv[1], timeout_ms=10000):
                raise AssertionError("Entered a lock held by the parent")
        except KeyboardInterrupt:
            print("interrupted", flush=True)
        """
    )
    with exclusive_file_lock(str(path)):
        result = subprocess.run(
            [sys.executable, "-c", program, str(path)],
            env={**os.environ, "WENYI_LOCKS_BACKEND": backend},
            capture_output=True,
            text=True,
            timeout=3,
        )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "interrupted"


def test_native_interrupt_after_acquisition_releases_handle(tmp_path, monkeypatch):
    library = locks._native_library()
    if library is None:
        pytest.skip("wenyi-locks native library is not built")
    monkeypatch.setattr(locks, "_FORCED_BACKEND", "rust")
    path = str(tmp_path / ".run.lock")
    acquire = library.wenyi_lock_acquire_v2
    acquired = []

    def interrupt_after_acquire(*args):
        code = acquire(*args)
        assert code == locks._LOCK_OK
        acquired.append(args[-1]._obj.handle)
        raise KeyboardInterrupt

    try:
        with monkeypatch.context() as patch:
            patch.setattr(library, "wenyi_lock_acquire_v2", interrupt_after_acquire)
            with pytest.raises(KeyboardInterrupt):
                with exclusive_file_lock(path):
                    pytest.fail("Interrupted acquisition entered the context")
        with exclusive_file_lock(path, blocking=False):
            pass
    finally:
        # Also clean up the handle when running this regression against the broken implementation.
        for handle in acquired:
            library.wenyi_lock_release(handle)


def test_blocking_timeout_releases_failed_attempt(tmp_path, backend):
    path = str(tmp_path / ".run.lock")
    with exclusive_file_lock(path):
        started = time.monotonic()
        with pytest.raises(StorageBusyError):
            with exclusive_file_lock(path, timeout_ms=100):
                pytest.fail("Entered a lock held by another handle")
        assert 0.08 <= time.monotonic() - started < 2
    with exclusive_file_lock(path, blocking=False):
        pass


def test_native_error_interrupted_on_return_does_not_release_another_lock(tmp_path, monkeypatch):
    library = locks._native_library()
    if library is None:
        pytest.skip("wenyi-locks native library is not built")
    monkeypatch.setattr(locks, "_FORCED_BACKEND", "rust")
    path = str(tmp_path / ".run.lock")
    holder = FileLockHandle(path)
    holder.acquire()

    def interrupted_error(*args):
        # An OS error can have the same numeric value as another live handle.
        result = args[-1]._obj
        result.handle = 0
        result.os_error = holder._handle
        raise KeyboardInterrupt

    try:
        with monkeypatch.context() as patch:
            patch.setattr(library, "wenyi_lock_acquire_v2", interrupted_error)
            with pytest.raises(KeyboardInterrupt):
                with exclusive_file_lock(str(tmp_path / "missing" / ".run.lock")):
                    pytest.fail("Interrupted acquisition entered the context")
        with pytest.raises(StorageBusyError):
            with exclusive_file_lock(path, blocking=False):
                pytest.fail("Released another owner's lock")
    finally:
        holder.release()


def test_incompatible_native_library_is_not_called(monkeypatch):
    legacy = SimpleNamespace(wenyi_lock_acquire=lambda *args: pytest.fail("Called the old ABI"))
    monkeypatch.setattr(locks, "_native_probed", False)
    monkeypatch.setattr(locks, "_native", None)
    monkeypatch.setattr(locks, "_library_candidates", lambda: iter(["old-library"]))
    monkeypatch.setattr(locks.ctypes, "CDLL", lambda _: legacy)
    assert locks._native_library() is None
