"""Call local models through Ollama's OpenAI-compatible endpoint."""

from ...config import LLMConfig
from .openai_compatible import OpenAICompatibleClient

DEFAULT_BASE_URL = "http://localhost:11434/v1"


class OllamaClient(OpenAICompatibleClient):
    def __init__(self, cfg: LLMConfig):
        """Initialize Ollama's default local endpoint without requiring credentials by default."""
        super().__init__(
            cfg,
            provider_name="Ollama",
            default_base_url=DEFAULT_BASE_URL,
            requires_api_key=False,
        )
