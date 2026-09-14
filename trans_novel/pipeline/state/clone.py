"""Closed run-store directory cloning."""

from __future__ import annotations

import os
import shutil


def clone_closed_runstore(source: str, destination: str) -> None:
    """Copy a closed store while holding its stable advisory lock inode."""
    if os.path.exists(destination):
        raise ValueError(f"runstore clone destination already exists: {destination}")
    source_root = os.path.abspath(source)
    destination_root = os.path.abspath(destination)
    if not os.path.isdir(source_root):
        raise ValueError(f"runstore source is not a directory: {source}")
    lock_path = os.path.join(source_root, ".run.lock")
    with open(lock_path, "a+b") as lock_file:
        try:
            if os.name == "nt":  # pragma: no cover - Windows-specific
                import msvcrt

                lock_file.seek(0, os.SEEK_END)
                if lock_file.tell() == 0:
                    lock_file.write(b"\0")
                    lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as error:
            raise ValueError("runstore source is actively locked") from error
        try:
            forbidden = {"journal.json", "note_migration.json"}
            for root, dirs, files in os.walk(source_root):
                relative = os.path.relpath(root, source_root)
                for name in (*dirs, *files):
                    if name == ".run.lock":
                        continue
                    if name in forbidden or name.endswith(".tmp") or ".pending" in name:
                        raise ValueError(f"source runstore has transient marker: {name}")
                    if name.startswith(".") and name not in {".gitkeep"}:
                        raise ValueError(f"source runstore has transient marker: {name}")
                if relative == ".":
                    continue
            shutil.copytree(
                source_root,
                destination_root,
                ignore=shutil.ignore_patterns(
                    ".run.lock", "journal.json", "note_migration.json", "*.tmp", "*.pending"
                ),
            )
        finally:
            if os.name == "nt":  # pragma: no cover - Windows-specific
                import msvcrt

                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


__all__ = ["clone_closed_runstore"]
