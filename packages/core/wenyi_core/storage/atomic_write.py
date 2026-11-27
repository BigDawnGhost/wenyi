"""Publish a file so a reader sees either the previous content or the new one.

Three properties are needed together, and each one alone leaves a real failure:

* A per-writer temporary name. A fixed `<target>.tmp` is shared by every process
  writing the same target, and on Windows two holders of one name make the
  replacement itself fail with `WinError 32` rather than merely interleave.
* `flush` and `fsync` before the replacement. `os.replace` publishes the name,
  not the bytes, so without a flush a power loss leaves the new name over old or
  empty content.
* Bounded retries around the replacement. Even with a private temporary name,
  a target that an antivirus scanner or the search indexer has momentarily
  opened is inaccessible to the replacement, and the failure is transient.

Temporary basenames have a fixed length so a target near the filesystem's
component limit can still be written. The writer opens the temporary file
exclusively and only cleans up files it created: a published artifact can have
any temporary-looking name.

The retry policy is bounded on purpose: a permanent failure must surface to the
caller instead of hanging the run, and the raised message names the target so a
person can act on it.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from uuid import uuid4

from .io_path import to_io_path

#: Attempts for one replacement before the failure is reported.
REPLACE_ATTEMPTS = 5
#: Delay between replacement attempts, in seconds, scaled by the attempt number.
REPLACE_DELAY = 0.2


def replace_with_retry(source: str | Path, target: str | Path) -> None:
    """Replace `target` with `source`, retrying a transient Windows failure.

    Only a permission failure is retried. A missing source or a full disk is
    permanent, and retrying it would delay the report without changing it.
    """
    target_text = to_io_path(target)
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(to_io_path(source), target_text)
            return
        except PermissionError as error:
            if attempt + 1 >= REPLACE_ATTEMPTS:
                raise PermissionError(
                    f"Cannot replace {target_text} after {REPLACE_ATTEMPTS} attempts; "
                    "another process may still hold it open"
                ) from error
            time.sleep(REPLACE_DELAY * (attempt + 1))


def write_text_atomic(
    path: str | Path, text: str, *, mode: int = 0o666, temporary_prefix: str = ".wenyi-"
) -> None:
    """Durably write `text` to `path` through a private temporary file.

    The parent directory is created when missing, because a caller writing a
    nested artifact should not have to know the layout of the run directory.
    `mode` is the creation mode of the new file, subject to the process umask.
    A caller with an existing private namespace can supply a fixed `temporary_prefix`
    for the staging basename. It must not include a path or depend on the target name.
    """
    target = str(path)
    directory, basename = os.path.split(target)
    directory = directory or os.curdir
    os.makedirs(to_io_path(directory), exist_ok=True)

    def opener(path: str, flags: int) -> int:
        return os.open(to_io_path(path), flags, mode)

    while True:
        name = f"{temporary_prefix}{uuid4().hex}.tmp"
        if name.casefold() == basename.casefold():
            continue  # Even an unpublished target may have a temporary-looking name.
        temporary = os.path.join(directory, name)
        try:
            handle = open(to_io_path(temporary), "x", encoding="utf-8", opener=opener)
        except FileExistsError:
            continue  # We do not own this entry; leave it alone.
        break

    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(temporary, target)
    except BaseException:
        # The target is untouched, so the temporary file is the only thing left to
        # discard. A failure to remove it must not replace the original error.
        try:
            os.remove(to_io_path(temporary))
        except OSError:
            pass
        raise


def sync_directory(path: str) -> None:
    """Persist directory-entry changes on POSIX, where directories can be fsynced."""
    if os.name == "nt":
        return
    descriptor = os.open(to_io_path(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
