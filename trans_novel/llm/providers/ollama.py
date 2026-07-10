"""通过 Ollama 的 OpenAI 兼容接口调用本地模型。"""

from ...config import LLMConfig
from .openai_compatible import OpenAICompatibleClient

DEFAULT_BASE_URL = "http://localhost:11434/v1"


class OllamaClient(OpenAICompatibleClient):
    def __init__(self, cfg: LLMConfig):
        super().__init__(
            cfg,
            provider_name="Ollama",
            default_base_url=DEFAULT_BASE_URL,
            requires_api_key=False,
        )
