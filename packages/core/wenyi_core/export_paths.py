"""Publish explicit CLI editions without replacing a concurrent writer's output."""

from __future__ import annotations

import errno
import os
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory

from .atomicio import PublishError
from .storage.io_path import to_io_path


def bilingual_out_path(out_path: str) -> str:
    """Derive the bilingual path from an explicit out_path by appending -bi to the stem."""
    base, ext = os.path.splitext(out_path)
    return f"{base}-bi{ext}"


def _publish_new(staged: Path, destination: str) -> None:
    # Both primitives fail atomically if the destination exists. Never fall back
    # to check-then-replace, even on a filesystem that does not support hard links.
    if os.name == "nt":
        os.rename(staged, to_io_path(destination))
    else:
        try:
            os.link(staged, destination)
        except OSError as error:
            if error.errno not in {errno.EPERM, errno.ENOTSUP, errno.ENOSYS}:
                raise
            raise PublishError(
                f"Cannot safely publish {destination}: the filesystem does not allow hard links. "
                "Choose another output directory or explicitly allow replacement with --overwrite."
            ) from error


class NumberedOutput:
    """One explicit output request; use the same numbered stem for paired editions."""

    def __init__(self, path: str, on_rename: Callable[[str, str], None]):
        self.path = path
        self._on_rename = on_rename
        self._index = 0
        self._published_mono: str | None = None

    def publish(
        self, render: Callable[[str], str], *, bilingual: bool, include_bilingual: bool
    ) -> str:
        parent = Path(to_io_path(self.path)).parent
        parent.mkdir(parents=True, exist_ok=True)
        stem, ext = os.path.splitext(self.path)
        with TemporaryDirectory(dir=parent, prefix=".wenyi-export-") as directory:
            staged = Path(render(str(Path(directory) / f"edition{ext}")))
            warnings = Path(f"{staged}.warnings.json")
            index = self._index
            while True:
                base = self.path if index == 0 else f"{stem} ({index}){ext}"
                other = bilingual_out_path(base)
                target, companion = (other, base) if bilingual else (base, other)
                candidates = [target]
                if include_bilingual and companion != self._published_mono:
                    candidates.append(companion)
                if any(
                    os.path.lexists(to_io_path(name + suffix))
                    for name in candidates
                    for suffix in ("", ".warnings.json")
                ):
                    index += 1
                    continue
                try:
                    _publish_new(staged, target)
                except FileExistsError:
                    index += 1
                    continue
                break
            if not bilingual:
                self._published_mono = target
            if warnings.exists():
                try:
                    _publish_new(warnings, f"{target}.warnings.json")
                except OSError as error:
                    raise PublishError(
                        f"Published {target}, but could not publish its warnings: {error}"
                    ) from error
            if index != self._index:
                requested = bilingual_out_path(self.path) if bilingual else self.path
                self._on_rename(requested, target)
            self._index = index
            return target
