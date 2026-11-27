"""Connection defaults for Atlas Cloud."""

from .openai_compatible import OpenAICompatibleClient

DEFAULT_BASE_URL = "https://api.atlascloud.ai/v1"
DEFAULT_API_KEY_ENV = "ATLASCLOUD_API_KEY"


class AtlasCloudClient(OpenAICompatibleClient):
    default_base_url = DEFAULT_BASE_URL
    default_api_key_env = DEFAULT_API_KEY_ENV
    requires_api_key = True
