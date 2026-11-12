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

The retry policy is bounded on purpose: a permanent failure must surface to the
caller instead of hanging the run, and the raised message names the target so a
person can act on it.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from uuid import uuid4

from .io_path import plain_path, to_io_path

#: Attempts for one replacement before the failure is reported.
REPLACE_ATTEMPTS = 5
#: Delay between replacement attempts, in seconds, scaled by the attempt number.
REPLACE_DELAY = 0.2

#: Temporary files this module creates, and the legacy form older versions left
#: behind. Anything else in a run directory belongs to the user and stays.
_TEMPORARY_NAME = re.compile(r"^.+\.[0-9a-f]{32}\.tmp$|^.+\.tmp$")


def resolve_temporary(name: str) -> str:
    """Return the private temporary name for one target, beside the target.

    The suffix still ends in `.tmp` so an interrupted write is recognisable, and
    the random middle makes two writers of the same target independent.
    ```resolve_temporary("manifest.json")``` starts with `manifest.json.` and ends
    with `.tmp`.
    """
    return f"{name}.{uuid4().hex}.tmp"


def is_temporary_name(name: str) -> bool:
    """Whether a file name is a temporary file this module or its predecessors wrote."""
    return bool(_TEMPORARY_NAME.match(name))


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


def write_text_atomic(path: str | Path, text: str) -> None:
    """Durably write `text` to `path` through a private temporary file.

    The parent directory is created when missing, because a caller writing a
    nested artifact should not have to know the layout of the run directory.
    """
    target = str(path)
    directory = os.path.dirname(target)
    os.makedirs(to_io_path(directory), exist_ok=True)
    temporary = resolve_temporary(target)
    try:
        with open(to_io_path(temporary), "w", encoding="utf-8") as handle:
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


def sweep_temporary(directory: str | Path) -> list[str]:
    """Delete leftover temporary files below one directory, returning what was removed.

    Only names `is_temporary_name` accepts are deleted, and only files: a run
    directory also holds exported documents and cached sources, so a sweep that
    matched every `*.tmp` would eventually delete something this program never
    wrote, and a directory is never a leftover write.

    The walk is recursive because a writer keeps its temporary file beside its
    target, and chapters, reviews and analysis each have their own directory. It
    is safe to run while the process holds the run lock, which is what stops a
    concurrent writer from being swept mid-conversion.
    """
    removed: list[str] = []
    root = str(directory)
    for current, _directories, names in os.walk(to_io_path(root)):
        for name in names:
            if not is_temporary_name(name):
                continue
            path = os.path.join(current, name)
            try:
                # os.walk yields paths below the prefixed root it was given, so
                # this removal already operates in the extended-length namespace.
                os.remove(to_io_path(path))
                removed.append(plain_path(path))
            except FileNotFoundError:
                continue
            except OSError:
                # A file another process holds open is not this sweep's to remove,
                # and failing the whole initialization for it would be worse than
                # leaving one temporary file behind.
                continue
    return removed
