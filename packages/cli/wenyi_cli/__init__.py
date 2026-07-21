"""文译 CLI 包。

实际的命令实现复用内核自带的 ``wenyi_core.cli``（文件模式，零基础设施）。
本包仅提供 console_script 入口与未来"远程模式"（经 HTTP 调 API）的扩展位。
"""

from .main import app, main

__all__ = ["app", "main"]
