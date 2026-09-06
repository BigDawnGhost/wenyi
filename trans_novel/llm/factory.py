"""Construct a built-in LLM provider from configuration."""

from __future__ import annotations

from ..config import Config
from .base import LLMClient


def build_client(config: Config) -> LLMClient:
    """Import and construct the client selected by llm.provider lazily."""
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
    if provider in ("orcarouter", "orca-router"):
        from .providers.orcarouter import OrcaRouterClient

        return OrcaRouterClient(config.llm)
    if provider == "openai-compatible":
        from .providers.openai_compatible import OpenAICompatibleClient

        return OpenAICompatibleClient(config.llm)
    if provider == "ollama":
        from .providers.ollama import OllamaClient

        return OllamaClient(config.llm)
    if provider == "vllm":
        from .providers.vllm import VLLMClient

        return VLLMClient(config.llm)
    if provider in ("gemini", "google"):
        from .providers.gemini import GeminiClient

        return GeminiClient(config.llm)
    if provider == "fake":
        from .providers.fake import FakeClient

        return FakeClient()
    raise ValueError(
        f"Unknown provider: {provider}"
        " (supported: deepseek / openai / openrouter / orcarouter / "
        "openai-compatible / ollama / vllm / gemini / fake)"
    )
