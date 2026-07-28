"""Storage 抽象（tech-stack §7）。内核只依赖 Protocol，由调用方注入实现。"""

from .file import FileStorage
from .protocol import STATUS_DONE, STATUS_PENDING, Storage

__all__ = ["Storage", "FileStorage", "STATUS_DONE", "STATUS_PENDING"]
