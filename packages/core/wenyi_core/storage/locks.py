"""Cross-process advisory file locks shared by the local state stores.

Two interchangeable backends satisfy the same contract
(packages/core/tests/test_locks_contract.py): the optional wenyi-locks native
library, loaded through ctypes, and a pure-Python fallback.
`WENYI_LOCKS_BACKEND` selects one with `auto` | `rust` | `python`;
`auto` prefers the native library when one is found, and `WENYI_LOCKS_LIB`
overrides the library search path. The checked-in crate builds into its own
`target/` directory, so `CARGO_TARGET_DIR` is searched too: without it a
developer who builds the crate out of tree would silently keep the fallback.
"""

from __future__ import annotations

import ctypes
import errno
import logging
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
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
    root = Path(__file__).resolve().parents[4]
    for profile in profiles:
        yield str(root / "native" / "wenyi-locks" / "target" / profile / name)


_native: ctypes.CDLL | None = None
_native_probed = False


def _native_library() -> ctypes.CDLL | None:
    """Load the native backend once; `None` means the fallback is in use."""
    global _native, _native_probed
    if _native_probed:
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
    elif _native is None:
        _logger.warning(
            "wenyi-locks native library not found; serving locks with the Python backend. "
            "Tried: %s",
            ", ".join(tried),
        )
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


class FileLockHandle:
    """Own one acquired cross-process lock until :meth:`release`."""

    def __init__(self, lock_path: str | bytes | os.PathLike[str] | os.PathLike[bytes]):
        # Both backends hand the path to the OS as text, so a bytes path is
        # decoded here rather than failing later inside one backend only.
        self._path = os.fsdecode(lock_path)
        self._handle: int | None = None
        self._file: BinaryIO | None = None
        self._served_by: str | None = None

    @property
    def backend(self) -> str | None:
        """The backend that served this handle, or `None` before :meth:`acquire`.

        It records who took the lock, not whether native state is still held, so
        it keeps its value after :meth:`release`.
        """
        return self._served_by

    def acquire(self, *, blocking: bool = True, timeout_ms: int = 0) -> None:
        """Take the lock, waiting for another owner unless `blocking` is false."""
        if _use_native():
            self._acquire_native(blocking=blocking, timeout_ms=timeout_ms)
            self._served_by = "rust"
        else:
            self._acquire_python(blocking=blocking, timeout_ms=timeout_ms)
            self._served_by = "python"

    def _acquire_native(self, *, blocking: bool, timeout_ms: int) -> None:
        library = _native_library()
        assert library is not None  # _use_native() already proved availability
        raw = self._path.encode("utf-8")
        deadline = None if timeout_ms <= 0 else time.monotonic() + timeout_ms / 1000.0
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

    Same-path re-entry is the caller's responsibility and is not detected here:
    a second acquisition of a path this process already holds blocks like any
    other contender. Every nesting the code base relies on is short-circuited by
    a depth table above this layer (`RunStore.state_lock`, `SqliteStorage._file_lock`),
    and treating a second handle as contention keeps same-process contention
    observable and testable.
    """
    handle = FileLockHandle(lock_path)
    try:
        handle.acquire(blocking=blocking, timeout_ms=timeout_ms)
        yield
    finally:
        handle.release()
