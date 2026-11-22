"""Cross-process advisory file locks shared by the local state stores.

Two interchangeable backends satisfy the same contract
(packages/core/tests/test_locks_contract.py): the optional wenyi-locks native
library, loaded through ctypes, and a pure-Python fallback.
`WENYI_LOCKS_BACKEND` selects one with `auto` | `rust` | `python`;
`auto` prefers the native library when one is found, and `WENYI_LOCKS_LIB`
overrides the library search path. Both crates belong to one root Cargo
workspace, so a checkout build lands in the root `target/` directory and is
found without the override; `CARGO_TARGET_DIR` is searched as well, because a
developer who builds out of tree would otherwise silently keep the fallback.

`WENYI_LOCKS_MODE` decides what happens when the native library cannot be
loaded: `warn` (default) keeps the Python fallback and logs a warning naming
the paths tried and the load errors, `error` fails the lock acquisition with
those reasons instead of degrading quietly, and `silent` keeps the fallback
without the warning for operators who chose it deliberately.
"""

from __future__ import annotations

import ctypes
import errno
import logging
import os
import socket
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO

from ..errors import WenyiError

# Named lock files, one per mutual-exclusion domain. They are created on demand
# in the run directory and never deleted, so the names are part of the on-disk
# format: external tooling and long-lived state directories refer to them.
RUN_LOCK = ".run.lock"  # book-level long workflow; serializes whole runs
STATE_LOCK = ".state.lock"  # short consistent read/write transactions
EVENTS_LOCK = ".events.lock"  # append serialization for JSONL event logs
ASSEMBLE_LOCK = ".assemble.lock"  # artifact assembly versus ongoing translation
TIMING_LOCK = ".timing.lock"  # timing ledger merges
EXPORT_HISTORY_LOCK = ".export-history.lock"  # export history appends

_RETRY_SECONDS = 0.05
_FORCED_BACKEND: str | None = None
_BACKENDS = ("auto", "rust", "python")
# What to do when the native library cannot be loaded: warn and fall back
# (default), fail the acquisition, or fall back without the warning.
_LOCKS_MODES = ("warn", "error", "silent")

# Emit a holder report to stderr after this many seconds of waiting, and repeat
# it at that interval, so a CLI that blocks on a busy workflow lock tells the
# operator who holds the lock instead of stalling silently.
_WAIT_NOTICE_SECONDS = 5.0
# Owner metadata lives past byte 0. Both backends lock only byte 0 and never
# truncate the file, so the record cannot disturb the lock protocol.
_HOLDER_OFFSET = 64
_HOLDER_LIMIT = 256

_logger = logging.getLogger(__name__)

# Return codes of the wenyi-locks C ABI.
_LOCK_OK = 0
_LOCK_BUSY = 1
_LOCK_INVALID = 2
_LOCK_OS_ERROR = 3


class _NativeLockResult(ctypes.Structure):
    _fields_ = [("handle", ctypes.c_uint64), ("os_error", ctypes.c_int64)]


class StorageBusyError(WenyiError, BlockingIOError):
    """Another local process owns the requested workflow lock.

    Keeps `BlockingIOError` so existing `except BlockingIOError` guards keep
    matching while the code migrates.
    """


class LockReentryError(StorageBusyError):
    """The owning thread tried to acquire a file lock it already holds.

    Re-entering the OS-level lock from the same thread waits on an owner that,
    inside one acquisition context, can never go away, so it is reported
    instead of waited on. Nesting that is intentional is short-circuited by
    the depth tables above this layer (`RunStore._file_lock`,
    `SqliteStorage._file_lock`), so this error means a real acquisition bug.
    """


# File locks this process holds, keyed by normalised path with the owning
# thread id as the value. OS locks contend per handle rather than per process,
# so same-thread re-entry would wait on an owner that never exits; it is
# detected here and reported as `LockReentryError` instead.
_HELD_LOCKS: dict[str, int] = {}


def _lock_key(lock_path: str) -> str:
    return os.path.normcase(os.path.abspath(lock_path))


def _held_by_this_thread(lock_path: str) -> bool:
    return _HELD_LOCKS.get(_lock_key(lock_path)) == threading.get_ident()


def _forget_held_lock(lock_path: str) -> None:
    """Drop this process's ownership record for `lock_path`, if one exists."""
    _HELD_LOCKS.pop(_lock_key(lock_path), None)


def _backend_choice() -> str:
    choice = _FORCED_BACKEND or os.environ.get("WENYI_LOCKS_BACKEND") or "auto"
    if choice not in _BACKENDS:
        raise ValueError(f"Unknown lock backend {choice!r}; expected one of {', '.join(_BACKENDS)}")
    return choice


def _library_name() -> str:
    if os.name == "nt":
        return "wenyi_locks.dll"
    if sys.platform == "darwin":
        return "libwenyi_locks.dylib"
    return "libwenyi_locks.so"


def _library_candidates() -> Iterator[str]:
    override = os.environ.get("WENYI_LOCKS_LIB")
    if override:
        yield override
    name = _library_name()
    # PyInstaller --add-binary destination, for both --onedir and --onefile.
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen:
        yield os.path.join(frozen, name)
    profiles = ("release", "debug")
    # Cargo may put the artifact outside the crate. Ask the same variable it
    # honours first, so a local out-of-tree build is found without the override.
    cargo_target_dir = os.environ.get("CARGO_TARGET_DIR")
    if cargo_target_dir:
        for profile in profiles:
            yield str(Path(cargo_target_dir) / profile / name)
    # In-place checkout: packages/core/wenyi_core/storage/locks.py -> repo root.
    # Both crates share one root workspace, so the artifacts land in the root
    # target directory rather than beside the crate.
    root = Path(__file__).resolve().parents[4]
    for profile in profiles:
        yield str(root / "target" / profile / name)


_native: ctypes.CDLL | None = None
_native_probed = False
# One "path (reason)" entry per candidate that failed to load, kept so the
# fallback warning and the error-mode failure can name the real cause.
_native_attempts: list[str] = []


def _locks_mode() -> str:
    mode = os.environ.get("WENYI_LOCKS_MODE", "warn")
    if mode not in _LOCKS_MODES:
        raise ValueError(f"Unknown lock mode {mode!r}; expected one of {', '.join(_LOCKS_MODES)}")
    return mode


def _native_unavailable_message() -> str:
    tried = "; ".join(_native_attempts) or "no candidate paths"
    return (
        "The wenyi-locks native library could not be loaded and "
        "WENYI_LOCKS_MODE=error refuses the Python fallback. "
        f"Tried: {tried}. Build the crate (`cargo build -p wenyi-locks`) or point "
        "WENYI_LOCKS_LIB at the built library, or set WENYI_LOCKS_MODE=warn to "
        "accept the fallback."
    )


def _report_native_unavailable() -> None:
    """Make a missing native library loud: warn by default, fail on demand."""
    mode = _locks_mode()
    if mode == "error":
        raise RuntimeError(_native_unavailable_message())
    if mode == "silent":
        return
    _logger.warning(
        "wenyi-locks native library not found; serving locks with the Python "
        "backend. Set WENYI_LOCKS_MODE=error to fail instead of falling back, or "
        "WENYI_LOCKS_MODE=silent to quiet this warning. Tried: %s",
        "; ".join(_native_attempts),
    )


def _native_library() -> ctypes.CDLL | None:
    """Load the native backend once; `None` means the fallback is in use."""
    global _native, _native_probed
    if _native_probed:
        if _native is None and _locks_mode() == "error":
            raise RuntimeError(_native_unavailable_message())
        return _native
    _native_probed = True
    override = os.environ.get("WENYI_LOCKS_LIB")
    override_error: Exception | None = None
    tried = []
    for candidate in _library_candidates():
        tried.append(candidate)
        try:
            library = ctypes.CDLL(candidate)
            acquire = library.wenyi_lock_acquire_v2
            release = library.wenyi_lock_release
        except (OSError, AttributeError) as error:
            # Older builds used the output handle for OS errors too; never call
            # them with the new layout or mistake an error number for an owned lock.
            _native_attempts.append(f"{candidate} ({error})")
            if override is not None and candidate == override:
                override_error = error
            continue
        acquire.argtypes = [
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.c_ubyte,
            ctypes.c_uint32,
            ctypes.POINTER(_NativeLockResult),
        ]
        acquire.restype = ctypes.c_int
        release.argtypes = [ctypes.c_uint64]
        release.restype = ctypes.c_int
        _native = library
        break
    # Probing happens once per process, so an unnoticed miss pins the fallback
    # for the lifetime of a long-running sidecar.
    if override_error is not None:
        # An explicitly named library that does not load is always the operator's
        # problem, even when a later candidate rescues the process.
        _logger.error(
            "WENYI_LOCKS_LIB=%s did not load (%s); the override is ignored. Tried: %s",
            override,
            override_error,
            ", ".join(tried),
        )
    if _native is None:
        _report_native_unavailable()
    return _native


def _use_native() -> bool:
    if _backend_choice() == "python":
        return False
    return _native_library() is not None


def _busy_error() -> StorageBusyError:
    return StorageBusyError("Local workflow is already running")


# Errnos that mean "another owner holds this lock" rather than "this call
# cannot work at all". Only these may be reported as contention: an EACCES from
# a read-only directory or an ENOSPC from a full disk is a real failure, and
# calling it "busy" sends the user looking for a workflow that is not running.
_CONTENTION_ERRNOS = frozenset({errno.EACCES, errno.EAGAIN, errno.EDEADLK, errno.EWOULDBLOCK})


def _is_contention(error: OSError) -> bool:
    """Whether the OS refused because someone else owns the lock."""
    return error.errno in _CONTENTION_ERRNOS


def _still_waiting(error: OSError, blocking: bool, deadline: float | None) -> bool:
    """Decide whether a failed attempt is contention worth retrying."""
    if not _is_contention(error):
        return False
    if not blocking:
        return False
    if deadline is not None and time.monotonic() >= deadline:
        return False
    return True


def _write_holder_record(lock_path: str) -> None:
    """Record this process in the lock file for wait diagnostics; best effort."""
    record = (
        f"pid={os.getpid()} host={socket.gethostname()} "
        f"since={datetime.now(timezone.utc).isoformat(timespec='seconds')}"
    ).encode("utf-8", "replace")[:_HOLDER_LIMIT]
    try:
        with open(lock_path, "r+b") as handle:
            handle.seek(_HOLDER_OFFSET)
            handle.write(record)
    except OSError:
        # Diagnostics only: a read-only directory or a full disk must not turn
        # a successful acquisition into a failure.
        pass


def _describe_holder(lock_path: str) -> str:
    """Best-effort description of the recorded owner of `lock_path`."""
    try:
        with open(lock_path, "rb") as handle:
            handle.seek(_HOLDER_OFFSET)
            raw = handle.read(_HOLDER_LIMIT)
    except OSError:
        return "an unknown owner"
    text = raw.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
    return text or "an unknown owner"


def _report_wait(lock_path: str, waited: float) -> float:
    """Tell the operator who holds the lock; returns the next notice threshold."""
    print(
        f"Waiting for lock {lock_path} held by {_describe_holder(lock_path)} "
        f"(waited {waited:.1f}s)...",
        file=sys.stderr,
        flush=True,
    )
    return waited + _WAIT_NOTICE_SECONDS


class FileLockHandle:
    """Own one acquired cross-process lock until :meth:`release`."""

    def __init__(self, lock_path: str | bytes | os.PathLike[str] | os.PathLike[bytes]):
        # Both backends hand the path to the OS as text, so a bytes path is
        # decoded here rather than failing later inside one backend only.
        self._path = os.fsdecode(lock_path)
        self._handle: int | None = None
        self._file: BinaryIO | None = None
        self._served_by: str | None = None
        self._acquired = False

    @property
    def backend(self) -> str | None:
        """The backend that served this handle, or `None` before :meth:`acquire`.

        It records who took the lock, not whether native state is still held, so
        it keeps its value after :meth:`release`.
        """
        return self._served_by

    def acquire(self, *, blocking: bool = True, timeout_ms: int = 0) -> None:
        """Take the lock, waiting for another owner unless `blocking` is false.

        Re-entry from the owning thread raises :class:`LockReentryError`
        instead of waiting on an owner that cannot go away.
        """
        if _held_by_this_thread(self._path):
            raise LockReentryError(
                f"Lock {self._path} is already held by this thread; re-acquiring "
                "it would deadlock. Intentional nesting belongs in a depth table "
                "above this layer."
            )
        if _use_native():
            self._acquire_native(blocking=blocking, timeout_ms=timeout_ms)
            self._served_by = "rust"
        else:
            self._acquire_python(blocking=blocking, timeout_ms=timeout_ms)
            self._served_by = "python"
        _HELD_LOCKS[_lock_key(self._path)] = threading.get_ident()
        self._acquired = True
        _write_holder_record(self._path)

    def _acquire_native(self, *, blocking: bool, timeout_ms: int) -> None:
        library = _native_library()
        assert library is not None  # _use_native() already proved availability
        raw = self._path.encode("utf-8")
        deadline = None if timeout_ms <= 0 else time.monotonic() + timeout_ms / 1000.0
        wait_started = time.monotonic()
        next_notice = 0.0
        result = _NativeLockResult()
        try:
            while True:
                # Retry in Python so SIGINT can interrupt a wait for a long workflow.
                code = library.wenyi_lock_acquire_v2(raw, len(raw), 0, 0, ctypes.byref(result))
                if code == _LOCK_OK:
                    self._handle = result.handle
                    return
                if code == _LOCK_INVALID:
                    raise OSError(f"Cannot open lock file: {self._path}")
                if code != _LOCK_BUSY:
                    raise OSError(result.os_error, f"Cannot lock {self._path}")
                if not blocking or (deadline is not None and time.monotonic() >= deadline):
                    raise _busy_error()
                waited = time.monotonic() - wait_started
                if waited >= next_notice:
                    next_notice = _report_wait(self._path, waited)
                time.sleep(_RETRY_SECONDS)
        except BaseException:
            # ctypes may raise KeyboardInterrupt before assigning its return code.
            # The out parameter still identifies the resource acquired by that call.
            if result.handle:
                library.wenyi_lock_release(result.handle)
            self._handle = None
            raise

    def _acquire_python(self, *, blocking: bool, timeout_ms: int) -> None:
        deadline = None if timeout_ms <= 0 else time.monotonic() + timeout_ms / 1000.0
        wait_started = time.monotonic()
        next_notice = 0.0
        handle = open(self._path, "a+b")
        try:
            if os.name == "nt":  # pragma: no cover - Windows-specific
                import msvcrt

                # msvcrt locks a zero-length file, so no padding byte is written:
                # writing one fails when another process already owns the lock.
                while True:
                    handle.seek(0)
                    try:
                        # LK_NBLCK plus the shared retry loop is the point: LK_LOCK
                        # gives up after roughly ten seconds instead of waiting.
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as error:
                        # A disk that is full or a directory we may not write is
                        # not another owner: reporting it as busy would send the
                        # operator hunting for a workflow that never started.
                        if not _is_contention(error):
                            raise
                        if not _still_waiting(error, blocking, deadline):
                            raise _busy_error() from error
                        waited = time.monotonic() - wait_started
                        if waited >= next_notice:
                            next_notice = _report_wait(self._path, waited)
                        time.sleep(_RETRY_SECONDS)
            else:  # pragma: no cover - POSIX
                import fcntl

                while True:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError as error:
                        if not _is_contention(error):
                            raise
                        if not _still_waiting(error, blocking, deadline):
                            raise _busy_error() from error
                        waited = time.monotonic() - wait_started
                        if waited >= next_notice:
                            next_notice = _report_wait(self._path, waited)
                        time.sleep(_RETRY_SECONDS)
            self._file = handle
        except BaseException:
            self._file = None
            # Closing must not mask the reason the acquisition failed: Windows
            # reports PermissionError from close() while another owner holds it.
            try:
                handle.close()
            except OSError:
                pass
            raise

    def release(self) -> None:
        """Release the lock; safe to call on a handle that never acquired."""
        try:
            if self._handle is not None:
                library = _native_library()
                if library is not None:
                    library.wenyi_lock_release(self._handle)
                self._handle = None
                return
            handle = self._file
            self._file = None
            if handle is None:
                return
            try:
                if os.name == "nt":  # pragma: no cover - Windows-specific
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:  # pragma: no cover - POSIX
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
        finally:
            if self._acquired:
                self._acquired = False
                _forget_held_lock(self._path)


@contextmanager
def exclusive_file_lock(
    lock_path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    *,
    blocking: bool = True,
    timeout_ms: int = 0,
) -> Iterator[None]:
    """Hold an exclusive OS-level lock on `lock_path` (created if missing) until exit.

    A non-blocking acquisition raises :class:`StorageBusyError` instead of waiting.
    A non-contention `OSError` (a missing parent directory, a read-only one, a
    full disk) is raised unchanged, because it is not another owner.

    Re-entry from the owning thread is detected and raises
    :class:`LockReentryError` instead of silently deadlocking on the OS lock.
    Every nesting the code base relies on is short-circuited by a depth table
    above this layer (`RunStore.state_lock`, `SqliteStorage._file_lock`), so
    this error means a real acquisition bug. A blocked wait reports the lock
    holder to stderr, so a CLI that stalls on a busy workflow tells the
    operator who holds it.
    """
    handle = FileLockHandle(lock_path)
    try:
        handle.acquire(blocking=blocking, timeout_ms=timeout_ms)
        yield
    finally:
        handle.release()
