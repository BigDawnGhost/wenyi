"""将内部 Agent 映射到 translator / analyst / editor / fast 四个模型角色。"""

from __future__ import annotations

import time
import uuid
from typing import Any

from trans_novel.config import PRODUCTION_AGENT_IDS, Config, ModelRef
from trans_novel.llm.base import LLMClient, Messages
from trans_novel.llm.errors import AllModelsFailedError, UnknownAgentError
from trans_novel.llm.generation import GenerationOptions
from trans_novel.llm.json_parser import parse_json_loose
from trans_novel.llm.providers.transport import validate_generation_options
from trans_novel.llm.registry import ProviderRegistry
from trans_novel.llm.retrying import classify_fallback
from trans_novel.llm.telemetry import CallTelemetrySink
from trans_novel.model_profiles import parse_model_selection, parse_provider_model

_TRANSLATOR_AGENTS = frozenset({"translator"})
_ANALYST_AGENTS = frozenset({"analyst"})
_EDITOR_AGENTS = frozenset({"editor"})
_FAST_AGENTS = frozenset({"preparer"})


class AgentRouter(LLMClient):
    def __init__(
        self,
        config: Config,
        *,
        registry: ProviderRegistry | None = None,
        transports: dict | None = None,
        generation_options: GenerationOptions | None = None,
        telemetry_sink: CallTelemetrySink | None = None,
    ) -> None:
        super().__init__()
        self.config = config
        self._role_candidates = {
            role: tuple(getattr(config.llm.models, role))
            for role in ("translator", "analyst", "editor", "fast")
        }
        if registry is not None:
            self._registry = registry
            if generation_options is not None:
                registry.set_generation_options(generation_options)
            if telemetry_sink is not None:
                registry.set_telemetry_sink(telemetry_sink)
            self.generation_options = registry.generation_options
            self.usage = registry.usage
        else:
            self.generation_options = generation_options
            self._registry = ProviderRegistry(
                config.llm,
                self.usage,
                transports=transports,
                generation_options=generation_options,
                telemetry_sink=telemetry_sink,
            )

    def _models(self, agent: str | None) -> tuple[ModelRef, ...]:
        if not agent or agent not in PRODUCTION_AGENT_IDS:
            raise UnknownAgentError(
                f"未知或缺失 Agent：{agent!r}（生产 LLM 调用必须提供内置 Agent 标识）"
            )
        if agent in _TRANSLATOR_AGENTS:
            role, default_thinking = "translator", "off"
        elif agent in _ANALYST_AGENTS:
            role, default_thinking = "analyst", "low"
        elif agent in _EDITOR_AGENTS:
            role, default_thinking = "editor", "low"
        elif agent in _FAST_AGENTS:
            role, default_thinking = "fast", "low"
        else:  # pragma: no cover - PRODUCTION_AGENT_IDS 与映射必须同步
            raise UnknownAgentError(f"Agent 未绑定模型角色：{agent!r}")
        models: list[ModelRef] = []
        for value in self._role_candidates[role]:
            provider, model = parse_provider_model(value)
            selection = parse_model_selection(model)
            thinking = selection.thinking or default_thinking
            reasoning_enabled = thinking != "off"
            models.append(
                ModelRef(
                    provider=provider,
                    model=selection.model,
                    reasoning_enabled=reasoning_enabled,
                    reasoning_effort=thinking if reasoning_enabled else "high",
                )
            )
        return tuple(models)

    def complete(
        self,
        messages: Messages,
        *,
        json_mode: bool = False,
        max_tokens: int | None = None,
        stage: str | None = None,
        agent: str,
        operation: str,
    ) -> str:
        models = self._models(agent)
        logical_call_id = uuid.uuid4().hex if self._registry.telemetry_sink is not None else None
        attempt_counter = [0]
        records: list[tuple[ModelRef, str]] = []
        start = time.monotonic()
        try:
            for model_ref in models:
                validate_generation_options(
                    self._registry.capabilities_for(model_ref.provider, model_ref.model),
                    model_ref,
                    self.generation_options,
                )
            for index, model_ref in enumerate(models):
                try:
                    return self._registry.transport(model_ref.provider).complete(
                        messages,
                        model_ref,
                        json_mode=json_mode,
                        max_tokens=max_tokens,
                        stage=stage,
                        agent=agent,
                        operation=operation,
                        logical_call_id=logical_call_id,
                        attempt_counter=attempt_counter,
                    )
                except Exception as error:
                    reason = classify_fallback(error)
                    if reason is None:
                        raise
                    records.append((model_ref, reason))
                    if index + 1 < len(models):
                        self.usage.record_fallback(agent, operation)
                        continue
                    raise AllModelsFailedError(tuple(records)) from error
            raise AssertionError("model candidate chain must not be empty")
        finally:
            self.usage.record_logical_call(agent, operation, (time.monotonic() - start) * 1000)

    def complete_json(
        self,
        messages: Messages,
        *,
        max_tokens: int | None = None,
        stage: str | None = None,
        agent: str,
        operation: str,
    ) -> Any:
        text = self.complete(
            messages,
            json_mode=True,
            max_tokens=max_tokens,
            stage=stage,
            agent=agent,
            operation=operation,
        )
        return parse_json_loose(text)
