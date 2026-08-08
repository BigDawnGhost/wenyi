"""LLM provider 的稳定抽象接口。"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from .json_parser import parse_json_loose
from .usage import UsageTracker

Messages = list[dict[str, str]]
EventSink = Callable[..., None]
_LOGGER = logging.getLogger(__name__)


class LLMClient(ABC):
    """所有 provider 实现此接口。"""

    def __init__(self) -> None:
        """为 provider 初始化独立的用量统计器和可选事件出口。"""
        self.usage = UsageTracker()
        self._event_sink: EventSink | None = None
        self._event_sink_lock = threading.Lock()

    def set_event_sink(self, sink: EventSink | None) -> None:
        """绑定运行事件出口；Orchestrator 用它把重试实时写入书籍日志。"""
        with self._event_sink_lock:
            self._event_sink = sink

    def _emit_event(self, event: str, **data: Any) -> None:
        """线程安全地发送 provider 事件，日志失败不得掩盖原始模型异常。"""
        with self._event_sink_lock:
            sink = self._event_sink
            if sink is None:
                return
            try:
                sink(event, **data)
            except Exception:  # noqa: BLE001 - 可观察性故障不能改变模型调用语义
                _LOGGER.exception("Failed to write LLM event: %s", event)

    def usage_summary(self) -> dict[str, Any]:
        """返回累计 token 用量快照（totals + by_tier + cache_hit_rate）。"""
        return self.usage.summary()

    def validate_credentials(self) -> None:
        """校验 provider 调用所需凭据；本地或测试 provider 默认免检。"""

    @abstractmethod
    def complete(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        json_mode: bool = False,
        max_tokens: int | None = None,
        stage: str | None = None,
    ) -> str:
        """返回模型回复的纯文本；stage 仅用于用量归因。"""
        raise NotImplementedError

    def complete_json(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        max_tokens: int | None = None,
        stage: str | None = None,
    ) -> Any:
        """要求 JSON 输出并解析。"""
        text = self.complete(
            messages, tier=tier, json_mode=True, max_tokens=max_tokens, stage=stage
        )
        return parse_json_loose(text)
