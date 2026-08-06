"""通过 OrcaRouter 的 OpenAI 兼容接口调用模型。"""

from __future__ import annotations

from ...config import LLMConfig
from .openai_compatible import OpenAICompatibleClient

DEFAULT_BASE_URL = "https://api.orcarouter.ai/v1"
DEFAULT_API_KEY_ENV = "ORCAROUTER_API_KEY"


class OrcaRouterClient(OpenAICompatibleClient):
    """OrcaRouter 网关客户端。

    OrcaRouter 是 OpenAI 兼容的多模型路由网关（Base URL
    ``https://api.orcarouter.ai/v1``），请求体走**扁平的顶层**
    ``reasoning_effort``（同 OpenAI 方言），而非 OpenRouter 的嵌套
    ``reasoning`` 块。因此复用通用 OpenAI 兼容客户端，并把推理方言
    固定为 ``openai``。
    """

    def __init__(self, cfg: LLMConfig):
        super().__init__(
            cfg,
            provider_name="OrcaRouter",
            default_base_url=DEFAULT_BASE_URL,
            default_api_key_env=DEFAULT_API_KEY_ENV,
            requires_api_key=True,
        )
        # OrcaRouter 的推理方言是扁平 reasoning_effort（同 OpenAI），
        # 覆盖配置里可能设置的其它 reasoning_style。
        self.reasoning_style = "openai"
