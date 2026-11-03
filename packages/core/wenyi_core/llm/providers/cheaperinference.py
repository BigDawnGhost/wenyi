"""Connection defaults for Cheaper Inference."""

from .openai_compatible import OpenAICompatibleClient

DEFAULT_BASE_URL = "https://api.cheaperinference.com/v1"
DEFAULT_API_KEY_ENV = "CHEAPER_INFERENCE_API_KEY"


class CheaperInferenceClient(OpenAICompatibleClient):
    default_base_url = DEFAULT_BASE_URL
    default_api_key_env = DEFAULT_API_KEY_ENV
    requires_api_key = True
