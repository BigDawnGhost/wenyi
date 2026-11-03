"""Atomic file publication shared by export and bridge output paths.

Export artifacts must never appear half-written: crash or disk-full leaves the previous
target intact and no debris behind. Temporary files use the ``<name>.<uuid>.tmp`` shape
so sweepers can recognize them. Windows virus scanners and open readers can briefly pin
the target, so replacement retries ``PermissionError`` with a bound before failing with
a readable error.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from .errors import WenyiError

_PERMISSION_RETRIES = 5
_RETRY_WAIT_SECONDS = 0.2


class PublishError(WenyiError, RuntimeError):
    """The replacement of a published file failed after bounded retries."""

    code = "publish_failed"


def _temp_name(target: Path) -> Path:
    return target.with_name(f"{target.name}.{uuid4().hex}.tmp")


def _fsync_best_effort(temp: Path) -> None:
    """Flush file data where the filesystem supports it; exotic mounts may refuse."""
    try:
        with open(temp, "rb+") as handle:
            os.fsync(handle.fileno())
    except OSError:
        pass


def _replace_with_retry(temp: Path, target: Path) -> None:
    for attempt in range(_PERMISSION_RETRIES):
        try:
            os.replace(temp, target)
            return
        except PermissionError:
            if attempt + 1 >= _PERMISSION_RETRIES:
                raise PublishError(
                    f"Could not replace {target}: the file stays in use by another process"
                ) from None
            time.sleep(_RETRY_WAIT_SECONDS)


@contextmanager
def publish_target(target: str | Path) -> Iterator[Path]:
    """Yield a temporary path in the target's directory and publish it on clean exit.

    Any exception inside the block removes the temporary file and leaves the previous
    target untouched.
    """
    target_path = Path(target)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp = _temp_name(target_path)
    try:
        yield temp
        _fsync_best_effort(temp)
        _replace_with_retry(temp, target_path)
    finally:
        if temp.exists():
            temp.unlink()


def publish_bytes(target: str | Path, data: bytes) -> Path:
    """Write ``data`` to ``target`` atomically and return the target path."""
    with publish_target(target) as temp:
        temp.write_bytes(data)
    return Path(target)
