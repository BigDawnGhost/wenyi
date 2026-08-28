"""通过 OrcaRouter 的 OpenAI 兼容接口调用模型。"""

from __future__ import annotations

from ...config import LLMConfig
from .openai_compatible import OpenAICompatibleClient

DEFAULT_BASE_URL = "https://api.orcarouter.ai/v1"
DEFAULT_API_KEY_ENV = "ORCAROUTER_API_KEY"


class OrcaRouterClient(OpenAICompatibleClient):
    def __init__(self, cfg: LLMConfig):
        """使用 OrcaRouter 默认端点和密钥环境变量初始化兼容客户端。"""
        super().__init__(
            cfg,
            provider_name="OrcaRouter",
            default_base_url=DEFAULT_BASE_URL,
            default_api_key_env=DEFAULT_API_KEY_ENV,
            requires_api_key=True,
        )
