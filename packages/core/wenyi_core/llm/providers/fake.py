"""测试和离线流程使用的可编程 provider。"""

from __future__ import annotations

from typing import Any, Callable, Optional

from ..base import LLMClient, Messages


class FakeClient(LLMClient):
    """可编程的离线 client。

    handler(messages, tier, json_mode) -> str。默认对 json_mode 返回 "[]"，
    否则返回空串。测试通过注入 handler 模拟翻译/抽取等行为。
    """

    def __init__(
        self,
        handler: Optional[Callable[[Messages, str, bool], str]] = None,
    ) -> None:
        """保存可选响应处理器，并初始化调用记录列表。"""
        super().__init__()
        self.handler = handler
        self.calls: list[dict[str, Any]] = []  # 记录调用，便于断言

    def complete(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        json_mode: bool = False,
        max_tokens: Optional[int] = None,
        stage: Optional[str] = None,
    ) -> str:
        """记录调用并返回处理器结果；未配置处理器时返回最小默认响应。"""
        self.calls.append(
            {
                "messages": messages,
                "tier": tier,
                "json_mode": json_mode,
                "max_tokens": max_tokens,
                "stage": stage,
            }
        )
        if self.handler is not None:
            return self.handler(messages, tier, json_mode)
        return "[]" if json_mode else ""
