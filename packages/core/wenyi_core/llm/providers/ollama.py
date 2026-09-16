"""Connection defaults for ollama."""

from .openai_compatible import OpenAICompatibleClient

DEFAULT_BASE_URL = "http://localhost:11434/v1"


class OllamaClient(OpenAICompatibleClient):
    default_base_url = DEFAULT_BASE_URL
