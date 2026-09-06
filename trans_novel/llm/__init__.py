"""LLM 调用层的稳定公共接口。"""

from trans_novel.config import ModelRef
from trans_novel.llm.base import LLMClient, Messages
from trans_novel.llm.errors import (
    AllModelsFailedError,
    LLMError,
    ProviderError,
    UnknownAgentError,
)
from trans_novel.llm.factory import build_client
from trans_novel.llm.generation import GenerationOptions
from trans_novel.llm.json_parser import parse_json_loose
from trans_novel.llm.providers.fake import FakeClient
from trans_novel.llm.router import AgentRouter
from trans_novel.llm.telemetry import CallAttemptTelemetry, CallTelemetrySink
from trans_novel.llm.usage import has_response_usage, normalize_response_usage
from trans_novel.llm.usage_persistence import UsagePersistenceError

__all__ = [
    "AgentRouter",
    "AllModelsFailedError",
    "CallAttemptTelemetry",
    "CallTelemetrySink",
    "FakeClient",
    "GenerationOptions",
    "LLMClient",
    "LLMError",
    "Messages",
    "ModelRef",
    "ProviderError",
    "UnknownAgentError",
    "UsagePersistenceError",
    "build_client",
    "has_response_usage",
    "normalize_response_usage",
    "parse_json_loose",
]
