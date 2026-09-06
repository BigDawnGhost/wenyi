"""Stable public interface for LLM calls."""

from .base import LLMClient, Messages
from .factory import build_client
from .json_parser import parse_json_loose
from .providers.fake import FakeClient

__all__ = [
    "FakeClient",
    "LLMClient",
    "Messages",
    "build_client",
    "parse_json_loose",
]
