"""一次应用调用独占的执行上下文及默认无状态适配器。

旧执行器把可变语言配置、LLM 回调、累计用量游标以及进程级随机源挂在长寿命
对象上，连续或并发调用可能相互污染。``ExecutionContext`` 将这些横切能力的
所有权缩到一次 invocation；它不负责业务配置，也不包含具体模型或存储对象。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from .models import ApplicationEvent, ProgressUpdate, UsageRecord
from .ports import Clock, EventSink, ProgressSink, RandomSource, UsageSink


class NullProgressSink:
    """忽略进度的默认适配器。"""

    def publish(self, update: ProgressUpdate) -> None:
        """接受并丢弃更新。"""
        del update


class NullEventSink:
    """忽略应用事件的默认适配器。"""

    def publish(self, event: ApplicationEvent) -> None:
        """接受并丢弃事件。"""
        del event


class NullUsageSink:
    """忽略用量增量的默认适配器。"""

    def record(self, usage: UsageRecord) -> None:
        """接受并丢弃记录。"""
        del usage


class SystemClock:
    """以标准库系统时钟实现生产默认时钟。"""

    def utc_now_ms(self) -> int:
        """返回非负 Unix epoch UTC 毫秒数。"""
        return max(0, time.time_ns() // 1_000_000)

    def monotonic(self) -> float:
        """返回进程内单调计时值。"""
        return time.monotonic()


class SystemRandomSource:
    """封装独立的系统熵随机发生器，避免依赖模块级随机状态。"""

    def __init__(self) -> None:
        self._random = random.SystemRandom()

    def random(self) -> float:
        """返回半开区间 ``[0.0, 1.0)`` 内的随机值。"""
        return self._random.random()


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """一次 invocation 的身份、观察端口和非确定性来源。

    dataclass 冻结只防止重新绑定字段；sink 实现本身通常会累积状态。调用方必须
    为每次 invocation 构造新上下文，不得把带状态 sink 或随机源跨调用共享。
    ``run_id`` 是日志关联身份，不等同于可持久化 workflow identity。
    """

    run_id: str
    progress: ProgressSink = field(default_factory=NullProgressSink)
    events: EventSink = field(default_factory=NullEventSink)
    usage: UsageSink = field(default_factory=NullUsageSink)
    clock: Clock = field(default_factory=SystemClock)
    random: RandomSource = field(default_factory=SystemRandomSource)


__all__ = [
    "ExecutionContext",
    "NullEventSink",
    "NullProgressSink",
    "NullUsageSink",
    "SystemClock",
    "SystemRandomSource",
]
