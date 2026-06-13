"""LLM 调用层：抽象接口 + DeepSeek provider + 离线 FakeClient。"""

from .base import LLMClient, FakeClient, build_client, parse_json_loose

__all__ = ["LLMClient", "FakeClient", "build_client", "parse_json_loose"]
