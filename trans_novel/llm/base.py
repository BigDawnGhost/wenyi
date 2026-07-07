from __future__ import annotations

import json
import re
import threading
from abc import ABC, abstractmethod
from typing import Any, Callable, Optional

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..config import Config, LLMConfig, TierConfig

Messages = list[dict[str, str]]

_TIER_FALLBACK = {"fast": ("cheap", "strong"), "cheap": ("strong",), "strong": ()}


def resolve_tier(tiers: dict[str, TierConfig], tier: str) -> TierConfig:
    if tier in tiers:
        return tiers[tier]
    for fb in _TIER_FALLBACK.get(tier, ("strong",)):
        if fb in tiers:
            return tiers[fb]
    return tiers["strong"]


def parse_json_loose(text: str) -> Any:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        inner = fenced.group(1).strip()
        try:
            return json.loads(inner)
        except Exception:
            text = inner
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        start = text.find(open_ch)
        end = text.rfind(close_ch)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except Exception:
                continue
    raise ValueError(f"无法解析为 JSON：{text[:200]!r}")


class LLMClient(ABC):
    @abstractmethod
    def complete(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        json_mode: bool = False,
        max_tokens: Optional[int] = None,
    ) -> str:
        raise NotImplementedError

    def complete_json(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        max_tokens: Optional[int] = None,
    ) -> Any:
        text = self.complete(messages, tier=tier, json_mode=True, max_tokens=max_tokens)
        return parse_json_loose(text)


class OpenAIClient(LLMClient):
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        if not cfg.tiers:
            raise ValueError("配置缺少 tiers")
        self._client = None
        self._client_lock = threading.Lock()

    def _ensure_client(self):
        with self._client_lock:
            return self._ensure_client_locked()

    def _ensure_client_locked(self):
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as e:
                raise RuntimeError("请安装 openai SDK: pip install openai") from e
            api_key = self.cfg.api_key
            if not api_key:
                raise RuntimeError(f"未设置 API key")
            self._client = OpenAI(
                api_key=api_key,
                base_url=self.cfg.base_url,
                timeout=self.cfg.timeout,
            )
        return self._client

    def complete(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        json_mode: bool = False,
        max_tokens: Optional[int] = None,
    ) -> str:
        tcfg = resolve_tier(self.cfg.tiers, tier)
        client = self._ensure_client()

        kwargs: dict[str, Any] = {
            "model": tcfg.model,
            "messages": messages,
            "stream": False,
        }

        if tcfg.thinking:
            kwargs["reasoning_effort"] = tcfg.reasoning_effort

        if tcfg.extra_body:
            kwargs.setdefault("extra_body", {}).update(tcfg.extra_body)

        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        if max_tokens:
            kwargs["max_tokens"] = max_tokens

        @retry(
            stop=stop_after_attempt(self.cfg.max_retries + 1),
            wait=wait_exponential(multiplier=1, max=30),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        )
        def _call() -> str:
            resp = client.chat.completions.create(**kwargs)
            return resp.choices[0].message.content or ""

        return _call()


class FakeClient(LLMClient):
    def __init__(self, handler: Optional[Callable[[Messages, str, bool], str]] = None):
        self.handler = handler
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        json_mode: bool = False,
        max_tokens: Optional[int] = None,
    ) -> str:
        self.calls.append({"messages": messages, "tier": tier,
                           "json_mode": json_mode, "max_tokens": max_tokens})
        if self.handler is not None:
            return self.handler(messages, tier, json_mode)
        return "[]" if json_mode else ""


def build_client(config: Config) -> LLMClient:
    provider = config.llm.provider.lower()
    if provider == "fake":
        return FakeClient()
    return OpenAIClient(config.llm)
