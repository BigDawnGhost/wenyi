"""OpenAI 兼容 provider 共用的传输、重试与档位解析。"""

from __future__ import annotations

import os
import threading
from abc import abstractmethod
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Callable, Generic, TypeVar

from pydantic import BaseModel

from ...config import LLMConfig, TierConfig
from ..base import LLMClient, Messages
from ..retrying import RetryReporter, provider_retry
from ..tiers import resolve_tier
from ..usage import (
    UsageSample,
    make_usage_sample,
    read_usage_int,
    read_usage_value,
)

OptionsT = TypeVar("OptionsT", bound=BaseModel)
_JSON_MODE_INSTRUCTION = "Output must be valid json."


@dataclass(frozen=True)
class ResolvedTier(Generic[OptionsT]):
    """provider 已补全并校验的运行时档位。"""

    model: str
    options: OptionsT


def resolve_provider_tiers(
    overrides: dict[str, TierConfig],
    *,
    options_type: type[OptionsT],
    defaults: dict[str, ResolvedTier[OptionsT]] | None = None,
) -> dict[str, ResolvedTier[OptionsT]]:
    """合并通用档位覆盖，并交给 provider 专属 options 模型校验。"""
    tiers = dict(defaults or {})
    for name, override in overrides.items():
        current = tiers.get(name)
        model = override.model or (current.model if current else None)
        if not model:
            raise ValueError(f"llm.tiers.{name}.model 不能为空")
        option_values = current.options.model_dump() if current else {}
        option_values.update(override.options)
        tiers[name] = ResolvedTier(
            model=model,
            options=options_type.model_validate(option_values),
        )
    if "strong" not in tiers:
        raise ValueError("配置缺少 llm.tiers.strong.model")
    return tiers


def base_request_kwargs(
    model: str,
    messages: Messages,
    *,
    json_mode: bool,
) -> dict[str, Any]:
    """构造 Chat Completions 基础参数，并为 JSON 模式补充明确指令。"""
    request_messages = messages
    if json_mode:
        request_messages = [dict(message) for message in messages]
        for message in request_messages:
            if message.get("role") == "system":
                message["content"] = f"{message.get('content', '')}\n\n{_JSON_MODE_INSTRUCTION}"
                break
        else:
            request_messages.insert(
                0,
                {"role": "system", "content": _JSON_MODE_INSTRUCTION},
            )
        # 有些中转/网关只校验 user 角色内容（例如转发到 Responses API 的
        # text.format 校验只看 input 里的用户内容），只在 system 里提到
        # "json" 未必够，所以也在最后一条 user 消息里补一份，双重保证。
        for message in reversed(request_messages):
            if message.get("role") == "user":
                content = str(message.get("content", ""))
                if "json" not in content.lower():
                    message["content"] = f"{content}\n\n{_JSON_MODE_INSTRUCTION}"
                break
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": request_messages,
        "stream": False,
    }
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    return kwargs


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并 provider 请求体；用户值优先。"""
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def normalize_openai_usage(usage: Any) -> UsageSample | None:
    """把 OpenAI 风格的嵌套缓存明细转换成统一用量。"""
    if usage is None:
        return None
    details = read_usage_value(usage, "prompt_tokens_details")
    cached_value = read_usage_value(details, "cached_tokens")
    if cached_value is None:
        cache_hit_tokens = 0
        cache_miss_tokens = 0
    else:
        cache_hit_tokens = read_usage_int(details, "cached_tokens")
        cache_miss_tokens = max(
            0,
            read_usage_int(usage, "prompt_tokens") - cache_hit_tokens,
        )
    return make_usage_sample(
        usage,
        cache_hit_tokens=cache_hit_tokens,
        cache_miss_tokens=cache_miss_tokens,
    )


def _read_attr(value: Any, name: str, default: Any = None) -> Any:
    """同时读取 SDK 对象和兼容端点可能返回的字典。"""
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _openai_delta_text(chunk: Any) -> str:
    """提取一个 Chat Completions 流式 chunk 的可见文本增量。"""
    choices = _read_attr(chunk, "choices") or []
    if not choices:
        return ""
    delta = _read_attr(choices[0], "delta")
    content = _read_attr(delta, "content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""

    pieces: list[str] = []
    for part in content:
        if isinstance(part, str):
            pieces.append(part)
            continue
        text = _read_attr(part, "text")
        if isinstance(text, str):
            pieces.append(text)
    return "".join(pieces)


class OpenAICompatibleBaseClient(LLMClient, Generic[OptionsT]):
    """所有 OpenAI Chat Completions 兼容 provider 的共用客户端。"""

    def __init__(
        self,
        cfg: LLMConfig,
        *,
        provider_name: str,
        default_base_url: str | None,
        default_api_key_env: str | None,
        tiers: dict[str, ResolvedTier[OptionsT]],
        requires_api_key: bool,
    ) -> None:
        """解析连接信息并保存已校验档位，SDK 客户端稍后按需创建。"""
        super().__init__()
        self.cfg = cfg
        self.provider_name = provider_name
        self.base_url = cfg.base_url or default_base_url
        self.api_key_env = cfg.api_key_env or default_api_key_env
        self.tiers = tiers
        self.requires_api_key = requires_api_key
        if not self.base_url:
            raise ValueError(f"{provider_name} provider 需要配置 llm.base_url")
        self._client: Any = None
        self._client_lock = threading.Lock()

    def _ensure_client(self) -> Any:
        """线程安全地惰性创建 OpenAI SDK 客户端并校验 API Key。"""
        with self._client_lock:
            if self._client is None:
                try:
                    from openai import OpenAI
                except ImportError as error:  # pragma: no cover
                    raise RuntimeError(
                        "需要 openai SDK：pip install openai"
                        "（或把 llm.provider 设为 fake 做离线测试）"
                    ) from error
                self.validate_credentials()
                api_key = os.environ.get(self.api_key_env) if self.api_key_env else None
                self._client = OpenAI(
                    api_key=api_key or "no-key",
                    base_url=self.base_url,
                    timeout=self.cfg.timeout,
                    # 重试由 Wenyi 统一分类、退避和记录，禁止 SDK 再叠一层。
                    max_retries=0,
                )
        return self._client

    def validate_credentials(self) -> None:
        """在发起任何模型流程前报告缺失的 API Key 环境变量。"""
        if not self.api_key_env:
            if self.requires_api_key:
                raise RuntimeError(f"{self.provider_name} provider 需要配置 llm.api_key_env")
            return
        api_key = os.environ.get(self.api_key_env, "").strip()
        if (self.requires_api_key or self.api_key_env) and not api_key:
            raise RuntimeError(f"未设置环境变量 {self.api_key_env}（{self.provider_name} API key）")

    def _normalize_usage(self, usage: Any) -> UsageSample | None:
        """标准 OpenAI 兼容响应默认使用嵌套缓存明细。"""
        return normalize_openai_usage(usage)

    def _consume_stream(
        self,
        stream: Any,
        record_usage: Callable[[UsageSample | None], None],
    ) -> tuple[str, UsageSample | None]:
        """消费一次响应流；失败时不返回任何已拼接的半成品。"""
        pieces: list[str] = []
        latest_sample: UsageSample | None = None
        saw_choice = False
        try:
            for chunk in stream:
                sample = self._normalize_usage(_read_attr(chunk, "usage"))
                if sample is not None:
                    # 流式 usage 通常是累计快照，只保留最后一个，不能逐块相加。
                    latest_sample = sample
                choices = _read_attr(chunk, "choices") or []
                if choices:
                    saw_choice = True
                pieces.append(_openai_delta_text(chunk))
            if not saw_choice:
                raise RuntimeError("OpenAI-compatible API 未返回任何候选结果")
        except Exception:
            record_usage(latest_sample)
            raise
        else:
            record_usage(latest_sample)
            return "".join(pieces), latest_sample
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                with suppress(Exception):
                    close()

    @abstractmethod
    def _build_request_kwargs(
        self,
        tier_config: ResolvedTier[OptionsT],
        messages: Messages,
        *,
        json_mode: bool,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        """把通用调用转换成 provider 的请求方言。"""
        raise NotImplementedError

    def complete(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        json_mode: bool = False,
        max_tokens: int | None = None,
        stage: str | None = None,
    ) -> str:
        """按指定档位调用兼容接口，自动重试并记录标准化用量。"""
        tier_config = resolve_tier(self.tiers, tier)
        kwargs = self._build_request_kwargs(
            tier_config,
            messages,
            json_mode=json_mode,
            max_tokens=max_tokens,
        )
        kwargs["stream"] = self.cfg.stream
        if self.cfg.stream:
            stream_options = kwargs.setdefault("stream_options", {})
            if isinstance(stream_options, dict):
                stream_options.setdefault("include_usage", True)
        client = self._ensure_client()
        started_at = self.performance.now()
        final_sample: UsageSample | None = None
        accumulated_prompt_tokens = 0
        accumulated_total_tokens = 0

        def account_usage(sample: UsageSample | None) -> None:
            """把一次 HTTP 尝试的最终 usage 快照记入累计账本。"""
            nonlocal accumulated_prompt_tokens, accumulated_total_tokens
            self.usage.record(tier, sample, stage)
            if sample is not None:
                accumulated_prompt_tokens += sample.prompt_tokens
                accumulated_total_tokens += sample.total_tokens

        reporter = RetryReporter(
            provider=self.provider_name,
            tier=tier,
            stage=stage,
            max_attempts=max(1, self.cfg.max_retries + 1),
            emit=self._emit_event,
        )

        @provider_retry(self.cfg.max_retries, reporter)
        def _call() -> str:
            """执行一次实际请求；异常交由 tenacity 重试装饰器处理。"""
            nonlocal accumulated_prompt_tokens, accumulated_total_tokens, final_sample
            response = client.chat.completions.create(**kwargs)
            if self.cfg.stream:
                text, final_sample = self._consume_stream(response, account_usage)
                return text
            sample = self._normalize_usage(getattr(response, "usage", None))
            account_usage(sample)
            final_sample = sample
            return response.choices[0].message.content or ""

        def record_performance(completion_tokens: int) -> None:
            """统一记录成功或已产生计费用量的失败逻辑调用。"""
            self.performance.record(
                provider=self.provider_name,
                model=tier_config.model,
                tier=tier,
                stage=stage,
                completion_tokens=completion_tokens,
                prompt_tokens=accumulated_prompt_tokens,
                total_tokens=accumulated_total_tokens,
                started_at=started_at,
            )

        try:
            result = _call()
        except Exception:
            if accumulated_total_tokens > 0:
                record_performance(0)
            raise
        record_performance(final_sample.completion_tokens if final_sample else 0)
        return result
