"""Connection defaults for orcarouter."""

from .openai_compatible import OpenAICompatibleClient

DEFAULT_BASE_URL = "https://api.orcarouter.ai/v1"
DEFAULT_API_KEY_ENV = "ORCAROUTER_API_KEY"


class OrcaRouterClient(OpenAICompatibleClient):
    default_base_url = DEFAULT_BASE_URL
    default_api_key_env = DEFAULT_API_KEY_ENV
    requires_api_key = True
