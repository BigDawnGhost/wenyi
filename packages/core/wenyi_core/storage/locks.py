"""Cross-process advisory file locks shared by the local state stores.

Two interchangeable backends satisfy the same contract
(packages/core/tests/test_locks_contract.py): the optional wenyi-locks native
library, loaded through ctypes, and a pure-Python fallback.
`WENYI_LOCKS_BACKEND` selects one with `auto` | `rust` | `python`;
`auto` prefers the native library when one is found, and `WENYI_LOCKS_LIB`
overrides the library search path.
"""

from __future__ import annotations

import ctypes
import errno
import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ..errors import WenyiError

_RETRY_SECONDS = 0.05
_FORCED_BACKEND: str | None = None

# Return codes of the wenyi-locks C ABI.
_LOCK_OK = 0
_LOCK_BUSY = 1
_LOCK_INVALID = 2
_LOCK_OS_ERROR = 3


class StorageBusyError(WenyiError, BlockingIOError):
    """Another local process owns the requested workflow lock.

    Keeps `BlockingIOError` so existing `except BlockingIOError` guards keep
    matching while the code migrates.
    """


def _backend_choice() -> str:
    return _FORCED_BACKEND or os.environ.get("WENYI_LOCKS_BACKEND", "auto")


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
    # In-place checkout: packages/core/wenyi_core/storage/locks.py -> repo root.
    root = Path(__file__).resolve().parents[4]
    for profile in ("release", "debug"):
        yield str(root / "native" / "wenyi-locks" / "target" / profile / name)


_native: ctypes.CDLL | None = None
_native_probed = False


def _native_library() -> ctypes.CDLL | None:
    """Load the native backend once; `None` means the fallback is in use."""
    global _native, _native_probed
    if _native_probed:
        return _native
    _native_probed = True
    for candidate in _library_candidates():
        try:
            library = ctypes.CDLL(candidate)
        except OSError:
            continue
        library.wenyi_lock_acquire.argtypes = [
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.c_ubyte,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint64),
        ]
        library.wenyi_lock_acquire.restype = ctypes.c_int
        library.wenyi_lock_release.argtypes = [ctypes.c_uint64]
        library.wenyi_lock_release.restype = ctypes.c_int
        _native = library
        break
    return _native


def _use_native() -> bool:
    if _backend_choice() == "python":
        return False
    return _native_library() is not None


def _busy_error() -> StorageBusyError:
    return StorageBusyError("Local workflow is already running")


def _still_waiting(error: OSError, blocking: bool, deadline: float | None) -> bool:
    """Decide whether a failed attempt is contention worth retrying."""
    if not blocking:
        return False
    if deadline is not None and time.monotonic() >= deadline:
        return False
    if os.name == "nt":
        return error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK)
    return isinstance(error, BlockingIOError)


class FileLockHandle:
    """Own one acquired cross-process lock until :meth:`release`."""

    def __init__(self, lock_path: str):
        self._path = os.fspath(lock_path)
        self._handle: int | None = None
        self._file: object | None = None

    @property
    def backend(self) -> str:
        """The backend that actually served this handle."""
        return "rust" if self._handle is not None else "python"

    def acquire(self, *, blocking: bool = True, timeout_ms: int = 0) -> None:
        """Take the lock, waiting for another owner unless `blocking` is false."""
        if _use_native():
            self._acquire_native(blocking=blocking, timeout_ms=timeout_ms)
        else:
            self._acquire_python(blocking=blocking, timeout_ms=timeout_ms)

    def _acquire_native(self, *, blocking: bool, timeout_ms: int) -> None:
        library = _native_library()
        assert library is not None  # _use_native() already proved availability
        raw = self._path.encode("utf-8")
        handle = ctypes.c_uint64(0)
        code = library.wenyi_lock_acquire(
            raw, len(raw), 1 if blocking else 0, timeout_ms, ctypes.byref(handle)
        )
        if code == _LOCK_OK:
            self._handle = handle.value
            return
        if code == _LOCK_BUSY:
            raise _busy_error()
        if code == _LOCK_INVALID:
            raise OSError(f"Cannot open lock file: {self._path}")
        # _LOCK_OS_ERROR carries the raw OS error code in the out parameter.
        raise OSError(handle.value, f"Cannot lock {self._path}")

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
                        if not _still_waiting(error, blocking, deadline):
                            raise _busy_error() from error
                        time.sleep(_RETRY_SECONDS)
            else:  # pragma: no cover - POSIX
                import fcntl

                while True:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError as error:
                        if not _still_waiting(error, blocking, deadline):
                            raise _busy_error() from error
                        time.sleep(_RETRY_SECONDS)
        except BaseException:
            # Closing must not mask the reason the acquisition failed: Windows
            # reports PermissionError from close() while another owner holds it.
            try:
                handle.close()
            except OSError:
                pass
            raise
        self._file = handle

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
    lock_path: str, *, blocking: bool = True, timeout_ms: int = 0
) -> Iterator[None]:
    """Hold an exclusive OS-level lock on `lock_path` (created if missing) until exit.

    A non-blocking acquisition raises :class:`StorageBusyError` instead of waiting.
    """
    handle = FileLockHandle(lock_path)
    handle.acquire(blocking=blocking, timeout_ms=timeout_ms)
    try:
        yield
    finally:
        handle.release()
