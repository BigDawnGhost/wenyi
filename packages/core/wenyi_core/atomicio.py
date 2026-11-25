"""Atomic file publication shared by export and bridge output paths.

Export artifacts are written to uniquely named sibling files before replacement.
Temporary names do not depend on the target name, which may already be close to
the filesystem's component limit. On Windows every filesystem operation here uses
the extended-length (``\\\\?\\``) spelling, so targets beyond the historical
``MAX_PATH`` limit still work: the yielded temporary path uses that filesystem
spelling, while returned targets keep the caller's logical spelling. Windows virus
scanners and open readers can briefly pin the target, so replacement retries
``PermissionError`` with a bound before failing with a readable error.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from .errors import WenyiError
from .storage.io_path import to_io_path

_PERMISSION_RETRIES = 5
_RETRY_WAIT_SECONDS = 0.2


class PublishError(WenyiError, RuntimeError):
    """The replacement of a published file failed after bounded retries."""

    code = "publish_failed"


def _temp_name(target: Path) -> Path:
    return target.with_name(f".wenyi-{uuid4().hex}.tmp")


def _fsync_best_effort(temp: Path) -> None:
    """Flush file data where the filesystem supports it; exotic mounts may refuse."""
    try:
        with open(to_io_path(temp), "rb+") as handle:
            os.fsync(handle.fileno())
    except OSError:
        pass


def _replace_with_retry(temp: Path, target: Path, logical_target: Path) -> None:
    for attempt in range(_PERMISSION_RETRIES):
        try:
            os.replace(to_io_path(temp), to_io_path(target))
            return
        except PermissionError:
            if attempt + 1 >= _PERMISSION_RETRIES:
                raise PublishError(
                    f"Could not replace {logical_target}: the file stays in use by another process"
                ) from None
            time.sleep(_RETRY_WAIT_SECONDS)


@contextmanager
def publish_target(target: str | Path) -> Iterator[Path]:
    """Yield a temporary path in the target's directory and publish it on clean exit.

    Any exception inside the block removes the temporary file and leaves the previous
    target untouched. Exiting without ever creating the temporary file is a deliberate
    abort: nothing is published and no error is raised. The yielded path is spelled
    for the filesystem (extended-length on Windows), so callers can open it even
    under a deep or long target directory.
    """
    target_path = Path(target)
    target_io = Path(to_io_path(target_path))
    target_io.parent.mkdir(parents=True, exist_ok=True)
    temp = _temp_name(target_io)
    try:
        yield temp
        if temp.exists():
            _fsync_best_effort(temp)
            _replace_with_retry(temp, target_io, target_path)
    finally:
        if temp.exists():
            temp.unlink()


def publish_bytes(target: str | Path, data: bytes) -> Path:
    """Write ``data`` to ``target`` atomically and return the logical target path."""
    with publish_target(target) as temp:
        temp.write_bytes(data)
    return Path(target)
