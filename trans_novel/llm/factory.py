"""根据 API 格式创建通用 LLM 客户端或离线测试客户端。"""

from __future__ import annotations

from ..config import Config
from .base import LLMClient


def build_client(config: Config) -> LLMClient:
    """真实格式统一返回 UniversalClient；fake 只用于离线测试。"""
    if config.llm.api_format == "fake":
        from .providers.fake import FakeClient

        return FakeClient()

    from .providers.universal import UniversalClient

    return UniversalClient(config.llm)
