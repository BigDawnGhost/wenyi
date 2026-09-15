"""Connection defaults for vllm."""

from .openai_compatible import OpenAICompatibleClient

DEFAULT_BASE_URL = "http://localhost:8000/v1"


class VLLMClient(OpenAICompatibleClient):
    default_base_url = DEFAULT_BASE_URL
