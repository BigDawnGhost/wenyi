"""Verify native lock ownership without trusting a process environment switch.

The native owner sends an ephemeral capability on the child's anonymous stdin.
It is checked against the current workspace's discovery record, the actual
parent PID and the live owner socket. The capability is never stored or logged.
Standalone engines have no capability and must acquire the native lock normally.
"""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from wenyi_core.storage.locks import FileLockHandle, StorageBusyError

from .. import paths

MAGIC = b"WENYI_MIGRATE\0\0\x01"


@dataclass(frozen=True)
class NativeOwner:
    workspace: Path
    port: int
    pid: int
    instance: list[int]
    token: bytes = field(repr=False)

    @classmethod
    def read(cls, stream: TextIO, workspace: Path) -> NativeOwner:
        try:
            line = stream.readline(4097)
            if len(line) > 4096 or not line.endswith("\n"):
                raise ValueError
            value = json.loads(line)
            owner = cls(
                workspace=Path(value["workspace"]),
                port=value["port"],
                pid=value["pid"],
                instance=value["instance"],
                token=bytes.fromhex(value["token"]),
            )
            if not owner.valid(workspace):
                raise ValueError
            return owner
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise RuntimeError("The native Desktop startup ownership proof is invalid") from error

    def valid(self, workspace: Path) -> bool:
        try:
            if (
                self.workspace.resolve() != workspace.resolve()
                or self.pid != os.getppid()
                or len(self.token) != 32
                or len(self.instance) != 16
                or any(type(byte) is not int or not 0 <= byte <= 255 for byte in self.instance)
            ):
                return False
            endpoint = json.loads(
                (workspace / ".desktop-instance.json").read_text(encoding="utf-8")
            )
            if (
                endpoint["protocol"] != 1
                or endpoint["pid"] != self.pid
                or endpoint["port"] != self.port
                or endpoint["instance"] != self.instance
            ):
                return False
            with socket.create_connection(("127.0.0.1", self.port), timeout=2) as stream:
                stream.sendall(MAGIC + self.token)
                return stream.recv(1) == b"\x01"
        except (OSError, ValueError, KeyError, TypeError, OverflowError):
            return False


def _shared_lock(file) -> None:
    """Use the same OS lock as Rust File::try_lock_shared, including on Windows."""
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class Overlapped(ctypes.Structure):
            _fields_ = [
                ("Internal", ctypes.c_size_t),
                ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.LockFileEx.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(Overlapped),
        ]
        kernel.LockFileEx.restype = wintypes.BOOL
        # FAIL_IMMEDIATELY without EXCLUSIVE_LOCK means a nonblocking read lease.
        if not kernel.LockFileEx(
            msvcrt.get_osfhandle(file.fileno()), 1, 0, 1, 0, ctypes.byref(Overlapped())
        ):
            raise ctypes.WinError(ctypes.get_last_error())
    else:
        import fcntl

        fcntl.flock(file, fcntl.LOCK_SH | fcntl.LOCK_NB)


def _linked(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    # Windows junctions/reparse points are not always reported as symlinks.
    return path.is_symlink() or bool(getattr(metadata, "st_file_attributes", 0) & 0x400)


@contextmanager
def profile_lock(workspace: Path, owner: NativeOwner | None) -> Iterator[None]:
    """Every engine participates in upgrade admission, even when run standalone."""
    root = paths.user_storages()
    root.mkdir(parents=True, exist_ok=True)
    gate = FileLockHandle(root / ".desktop-profile-admission.lock")
    file = None
    writer = None
    try:
        try:
            gate.acquire(blocking=False)
            if owner is None:
                file = (root / ".desktop-profile.lock").open("a+b")
                _shared_lock(file)
            else:
                # Register while admission is serialized and reverify the parent
                # here, not only when its pipe message was initially received.
                if not owner.valid(workspace):
                    raise RuntimeError("The native Desktop owner could not be verified")
                directory = root / ".desktop-writers"
                directory.mkdir(exist_ok=True)
                name = f"{bytes(owner.instance).hex()}-{os.getpid()}.lock"
                lease = directory / name
                if _linked(directory) or _linked(lease):
                    raise RuntimeError("The Desktop writer lease directory is unsafe")
                writer = FileLockHandle(lease)
                writer.acquire(blocking=False)
        except (OSError, StorageBusyError) as error:
            raise RuntimeError(
                "This Desktop profile is preparing an update; retry after it ends"
            ) from error
        finally:
            gate.release()
        yield
    finally:
        if writer is not None:
            # The lock, not file existence, is authoritative. A later native
            # admission prunes this proven-unlocked entry under the same gate.
            writer.release()
        if file is not None:
            file.close()
