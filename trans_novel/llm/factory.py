"""根据配置创建内置 LLM provider。"""

from __future__ import annotations

from ..config import Config
from ..locales import message
from .base import LLMClient


def build_client(config: Config) -> LLMClient:
    """根据 llm.provider 延迟导入并构造对应客户端。"""
    provider = config.llm.provider.strip().lower().replace("_", "-")
    if provider == "deepseek":
        from .providers.deepseek import DeepSeekClient

        return DeepSeekClient(config.llm)
    if provider == "openai":
        from .providers.openai import OpenAIClient

        return OpenAIClient(config.llm)
    if provider == "openrouter":
        from .providers.openrouter import OpenRouterClient

        return OpenRouterClient(config.llm)
    if provider == "openai-compatible":
        from .providers.openai_compatible import OpenAICompatibleClient

        return OpenAICompatibleClient(config.llm)
    if provider == "ollama":
        from .providers.ollama import OllamaClient

        return OllamaClient(config.llm)
    if provider == "vllm":
        from .providers.vllm import VLLMClient

        return VLLMClient(config.llm)
    if provider == "fake":
        from .providers.fake import FakeClient

        return FakeClient()
    raise ValueError(
        message(
            "error.provider_unknown",
            provider=provider,
            supported=(
                "deepseek / openai / openrouter / openai-compatible / "
                "ollama / vllm / fake"
            ),
        )
    )
