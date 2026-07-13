"""``trans-novel`` 命令入口。

本地模式：直接复用内核的 ``wenyi_core.cli``（写本地 ``state/`` 目录，不连数据库）。
行为与升级前完全一致（tech-stack 约束 4：CLI 兼容）。
"""

from wenyi_core.cli import app, main

__all__ = ["app", "main"]
