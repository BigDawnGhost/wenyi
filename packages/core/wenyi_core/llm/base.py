"""LLM provider 的稳定抽象接口。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from .json_parser import parse_json_loose
from .usage import UsageTracker

Messages = list[dict[str, str]]


class LLMClient(ABC):
    """所有 provider 实现此接口。"""

    def __init__(self) -> None:
        """为 provider 初始化独立的线程安全用量统计器。"""
        self.usage = UsageTracker()

    def usage_summary(self) -> dict[str, Any]:
        """返回累计 token 用量快照（totals + by_tier + cache_hit_rate）。"""
        return self.usage.summary()

    @abstractmethod
    def complete(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        json_mode: bool = False,
        max_tokens: Optional[int] = None,
        stage: Optional[str] = None,
    ) -> str:
        """返回模型回复的纯文本；stage 仅用于用量归因。"""
        raise NotImplementedError

    def complete_json(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        max_tokens: Optional[int] = None,
        stage: Optional[str] = None,
    ) -> Any:
        """要求 JSON 输出并解析。"""
        text = self.complete(
            messages, tier=tier, json_mode=True, max_tokens=max_tokens, stage=stage
        )
        return parse_json_loose(text)
