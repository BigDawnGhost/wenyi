"""Pin identical semantics for the Python and Rust file-lock backends."""

from __future__ import annotations

import errno
import logging
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


def test_native_backend_is_actually_loaded():
    """The guard the parameterised cases cannot provide.

    The `rust` parameter skips when the library is missing, so a machine without a
    build reports "the native contract is covered" while never running it. When the
    environment names a library, loading it is a hard requirement instead.
    """
    declared = os.environ.get("WENYI_LOCKS_LIB")
    if not declared:
        pytest.skip("WENYI_LOCKS_LIB is not set; no native library was named to verify")
    library = locks._native_library()
    assert library is not None, f"WENYI_LOCKS_LIB={declared} did not load"
    assert locks._use_native() is True


def test_backend_reports_the_server_and_survives_release(tmp_path, backend):
    path = str(tmp_path / ".run.lock")
    handle = FileLockHandle(path)
    assert handle.backend is None  # nothing has served it yet
    handle.acquire()
    try:
        assert handle.backend == backend
    finally:
        handle.release()
    assert handle.backend == backend


def test_backend_reports_the_fallback_when_rust_is_forced_without_a_library(tmp_path, monkeypatch):
    if locks._native_library() is not None:
        pytest.skip("this case needs a machine without the native library")
    monkeypatch.setattr(locks, "_FORCED_BACKEND", "rust")
    handle = FileLockHandle(str(tmp_path / ".run.lock"))
    handle.acquire()
    try:
        assert handle.backend == "python", "a handle must name who served it, not who was asked"
    finally:
        handle.release()


@pytest.mark.parametrize("value", ["zsh", "RUST", "Python", "auto ", " rust", "native"])
def test_unknown_backend_value_is_rejected(value, tmp_path, monkeypatch):
    monkeypatch.delenv("WENYI_LOCKS_LIB", raising=False)
    monkeypatch.setenv("WENYI_LOCKS_BACKEND", value)
    with pytest.raises(ValueError, match="lock backend"):
        with locks.exclusive_file_lock(str(tmp_path / ".run.lock")):
            pytest.fail("an unknown backend must not be treated as auto")


@pytest.mark.parametrize("value", ["auto", "rust", "python"])
def test_supported_backend_values_are_accepted(value, monkeypatch):
    monkeypatch.setenv("WENYI_LOCKS_BACKEND", value)
    assert locks._backend_choice() == value


def _raising_cdll(error: Exception):
    def load(_: str):
        raise error

    return load


def test_missing_library_warns_once(monkeypatch, caplog):
    monkeypatch.setattr(locks, "_native_probed", False)
    monkeypatch.setattr(locks, "_native", None)
    monkeypatch.delenv("WENYI_LOCKS_LIB", raising=False)
    monkeypatch.setattr(locks, "_library_candidates", lambda: iter(["absent-one", "absent-two"]))
    monkeypatch.setattr(locks.ctypes, "CDLL", _raising_cdll(OSError("cannot load")))
    with caplog.at_level(logging.WARNING, logger=locks.__name__):
        assert locks._native_library() is None
        assert locks._native_library() is None
    records = [record for record in caplog.records if record.name == locks.__name__]
    assert [record.levelname for record in records] == ["WARNING"]
    assert "absent-two" in records[0].getMessage()


def test_explicit_library_that_does_not_load_is_an_error(monkeypatch, caplog):
    monkeypatch.setattr(locks, "_native_probed", False)
    monkeypatch.setattr(locks, "_native", None)
    monkeypatch.setenv("WENYI_LOCKS_LIB", "broken-library")
    monkeypatch.setattr(locks, "_library_candidates", lambda: iter(["broken-library"]))
    monkeypatch.setattr(locks.ctypes, "CDLL", _raising_cdll(OSError("cannot load")))
    with caplog.at_level(logging.ERROR, logger=locks.__name__):
        assert locks._native_library() is None
    records = [record for record in caplog.records if record.name == locks.__name__]
    assert [record.levelname for record in records] == ["ERROR"]
    assert "WENYI_LOCKS_LIB=broken-library" in records[0].getMessage()


@pytest.mark.parametrize("as_bytes", [False, True], ids=["str", "bytes"])
def test_bytes_lock_path_matches_the_text_path(tmp_path, backend, as_bytes):
    path = str(tmp_path / ".run.lock")
    lock_path = os.fsencode(path) if as_bytes else path
    with exclusive_file_lock(lock_path, blocking=False):
        with pytest.raises(StorageBusyError):
            with exclusive_file_lock(path, blocking=False):
                pytest.fail("the text path should contend with the bytes path")


def test_non_contention_os_error_is_not_reported_as_busy(tmp_path, backend):
    # A missing parent directory refuses the open outright. Calling that "busy"
    # hides a real failure behind a message about a workflow that never started.
    missing = tmp_path / "missing" / ".run.lock"
    with pytest.raises(OSError) as raised:
        with exclusive_file_lock(str(missing), blocking=False):
            pytest.fail("acquired a lock inside a directory that does not exist")
    assert not isinstance(raised.value, StorageBusyError)


@pytest.mark.parametrize(
    "blocking, expected",
    [
        (False, False),
        (True, True),
    ],
    ids=["non-blocking", "blocking"],
)
def test_contention_errnos_are_classified_the_same_for_both_modes(blocking, expected):
    for number in (errno.EACCES, errno.EAGAIN, errno.EDEADLK, errno.EWOULDBLOCK):
        assert locks._is_contention(OSError(number, "contended")) is True
        assert locks._still_waiting(OSError(number, "contended"), blocking, None) is expected


@pytest.mark.parametrize("number", [errno.ENOSPC, errno.ENOENT, errno.EPERM, errno.EROFS])
def test_other_errnos_are_failures_rather_than_contention(number):
    error = OSError(number, "not contention")
    assert locks._is_contention(error) is False
    assert locks._still_waiting(error, True, None) is False


def test_same_path_reentry_contends_instead_of_being_detected(tmp_path, backend):
    """Pin the pass-through ruling: the primitive never inspects process-local state."""
    path = str(tmp_path / ".run.lock")
    entered = threading.Event()
    outcome = []

    def contender() -> None:
        try:
            with exclusive_file_lock(path, timeout_ms=200):
                entered.set()
        except StorageBusyError:
            outcome.append("busy")

    with exclusive_file_lock(path):
        thread = threading.Thread(target=contender)
        thread.start()
        thread.join(timeout=5)
    assert not entered.is_set(), "a second handle must contend, not reuse the outer lock"
    assert outcome == ["busy"], "re-entry must look like any other contender"
    with exclusive_file_lock(path, blocking=False):
        pass


def test_cargo_target_dir_contributes_library_candidates(tmp_path, monkeypatch):
    monkeypatch.setenv("CARGO_TARGET_DIR", str(tmp_path))
    # An override would short-circuit the search order being checked here.
    monkeypatch.delenv("WENYI_LOCKS_LIB", raising=False)
    candidates = list(locks._library_candidates())
    assert candidates[0] == str(tmp_path / "release" / locks._library_name())
    assert str(tmp_path / "debug" / locks._library_name()) in candidates


def test_checkout_candidates_remain_when_cargo_target_dir_is_unset(monkeypatch):
    monkeypatch.delenv("CARGO_TARGET_DIR", raising=False)
    monkeypatch.delenv("WENYI_LOCKS_LIB", raising=False)
    candidates = list(locks._library_candidates())
    assert any("wenyi-locks" in candidate for candidate in candidates)
