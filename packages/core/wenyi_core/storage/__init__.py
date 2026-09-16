"""Backend-neutral persistence ports; implementations are loaded lazily."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .protocol import STATUS_DONE, STATUS_PENDING, Storage

if TYPE_CHECKING:
    from .file import FileStorage as FileStorage

__all__ = ["Storage", "FileStorage", "STATUS_DONE", "STATUS_PENDING"]


def __getattr__(name: str):
    if name == "FileStorage":
        from .file import FileStorage

        return FileStorage
    raise AttributeError(name)
