"""应用边界使用的冻结数据传输对象。

这些对象描述一次调用的意图和可观察事实，不保存配置模型、运行时客户端、
数据库连接或文件句柄。命令在进入应用层后不得被原地改写。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProgressUpdate:
    """一次单调进度观察；``total=None`` 表示总量尚不可知。"""

    stage: str
    completed: int
    total: int | None = None
    message: str = ""


@dataclass(frozen=True, slots=True)
class ApplicationEvent:
    """应用调用产生的轻量事件；负载由不可变二元组表达。

    事件持久化时适配器应按自身协议编码值。本 DTO 故意不承诺领域 outbox 的
    JSON 架构，也不复用具体 LLM provider 的可变关键字参数字典。
    """

    name: str
    attributes: tuple[tuple[str, object], ...] = ()


@dataclass(frozen=True, slots=True)
class UsageRecord:
    """一次调用的非负资源增量，而不是共享客户端的累计快照。"""

    stage: str
    provider: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


__all__ = [
    "ApplicationEvent",
    "ProgressUpdate",
    "UsageRecord",
]
