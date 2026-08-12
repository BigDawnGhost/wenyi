"""调用级可观察性与非确定性端口。

端口保持刻意狭窄：实现可以写终端、日志、指标或测试探针，但应用服务无需知道
这些适配器来自 CLI、Web、数据库还是内存。每个方法都只接收稳定应用 DTO。
"""

from __future__ import annotations

from typing import Protocol

from .models import ApplicationEvent, ProgressUpdate, UsageRecord


class ProgressSink(Protocol):
    """接收当前 invocation 的进度快照。"""

    def publish(self, update: ProgressUpdate) -> None:
        """发布一条进度更新；实现不得借此改变应用执行语义。"""
        ...


class EventSink(Protocol):
    """接收当前 invocation 的应用事件。"""

    def publish(self, event: ApplicationEvent) -> None:
        """发布一条事件；重试和持久化策略由适配器决定。"""
        ...


class UsageSink(Protocol):
    """接收当前 invocation 的独立资源用量增量。"""

    def record(self, usage: UsageRecord) -> None:
        """记录一次增量；调用方不得提交跨 invocation 的累计快照。"""
        ...


class Clock(Protocol):
    """提供可替换的 UTC 墙钟和单调计时源。"""

    def utc_now_ms(self) -> int:
        """返回 Unix epoch 后的 UTC 毫秒数。"""
        ...

    def monotonic(self) -> float:
        """返回只用于计算间隔、不可持久化的单调秒数。"""
        ...


class RandomSource(Protocol):
    """提供可注入、可复现的随机采样。"""

    def random(self) -> float:
        """返回半开区间 ``[0.0, 1.0)`` 内的值。"""
        ...


__all__ = [
    "Clock",
    "EventSink",
    "ProgressSink",
    "RandomSource",
    "UsageSink",
]
