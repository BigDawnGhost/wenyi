"""Construct the routed workflow client from validated configuration."""

from __future__ import annotations

from ..config import Config
from .router import RoutedLLMClient


def build_client(config: Config) -> RoutedLLMClient:
    return RoutedLLMClient(config.llm)
