"""Construct the routed workflow client from validated configuration."""

from __future__ import annotations

from collections.abc import Mapping

from ..config import Config
from .router import RoutedLLMClient


def build_client(
    config: Config, *, credentials: Mapping[str, str | None] | None = None
) -> RoutedLLMClient:
    return RoutedLLMClient(config.llm, credentials=credentials)
