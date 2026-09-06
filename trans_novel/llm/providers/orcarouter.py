"""Call models through OrcaRouter's OpenAI-compatible endpoint."""

from __future__ import annotations

from ...config import LLMConfig
from .openai_compatible import OpenAICompatibleClient

DEFAULT_BASE_URL = "https://api.orcarouter.ai/v1"
DEFAULT_API_KEY_ENV = "ORCAROUTER_API_KEY"


class OrcaRouterClient(OpenAICompatibleClient):
    def __init__(self, cfg: LLMConfig):
        """Initialize a compatible client with OrcaRouter's default endpoint and key
        environment variable.
        """
        super().__init__(
            cfg,
            provider_name="OrcaRouter",
            default_base_url=DEFAULT_BASE_URL,
            default_api_key_env=DEFAULT_API_KEY_ENV,
            requires_api_key=True,
        )
