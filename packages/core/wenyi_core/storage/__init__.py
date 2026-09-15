"""Backend-neutral persistence ports; implementations are loaded lazily."""

from .protocol import STATUS_DONE, STATUS_PENDING, Storage

__all__ = ["Storage", "FileStorage", "STATUS_DONE", "STATUS_PENDING"]


def __getattr__(name):
    if name == "FileStorage":
        from .file import FileStorage

        return FileStorage
    raise AttributeError(name)
