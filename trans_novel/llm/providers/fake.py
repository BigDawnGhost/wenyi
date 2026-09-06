"""测试与离线流程使用的可编程 provider。

- FakeClient：直接注入 handler 的标准测试路径，handler 签名
  (messages, agent, operation, json_mode)。调用记录同时包含 agent 与
  operation 两个键，供离线测试确定性断言。缺省输出（纯文本 ""、JSON "[]"）
  是刻意的 fake 语义，绕过生产空响应校验，空输出计成功，不计 failed_attempts。
- FakeProviderTransport：provider 类型 fake 在 ProviderRegistry 中的共享传输，
  无凭据无网络，输出与 FakeClient 缺省一致；模型 ID 只用于目录一致性，不发送。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from trans_novel.config import LLMConfig, ModelRef
from trans_novel.llm.base import LLMClient, Messages
from trans_novel.llm.generation import GenerationOptions
from trans_novel.llm.providers.transport import validate_generation_options, warn_telemetry_failure
from trans_novel.llm.telemetry import CallAttemptTelemetry, CallTelemetrySink
from trans_novel.llm.usage import UsageTracker
from trans_novel.model_profiles import capabilities_for

Handler = Callable[[Messages, str, str, bool], str]


class FakeClient(LLMClient):
    """可编程的离线 client。"""

    def __init__(self, handler: Handler | None = None) -> None:
        super().__init__()
        self.handler = handler
        self.calls: list[dict[str, Any]] = []
        self._calls_lock = threading.Lock()

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
        record = {
            "messages": messages,
            "json_mode": json_mode,
            "max_tokens": max_tokens,
            "stage": stage,
            "agent": agent,
            "operation": operation,
        }
        with self._calls_lock:
            self.calls.append(record)
        self.usage.begin_attempt()
        start = time.monotonic()
        try:
            try:
                if self.handler is not None:
                    result = self.handler(messages, agent, operation, json_mode)
                else:
                    result = "[]" if json_mode else ""
            except Exception:
                self.usage.record_attempt_result(agent=agent, operation=operation, failed=True)
                raise
            self.usage.record_attempt_result(agent=agent, operation=operation)
            return result
        finally:
            self.usage.record_logical_call(agent, operation, (time.monotonic() - start) * 1000)


class FakeProviderTransport:
    """provider 类型 fake 的共享传输：无凭据、无网络、无 handler。

    缺省输出与直接 FakeClient 一致（"" / "[]"），是生产空响应规则之外的
    显式测试例外：成功返回，不记 failed_attempts，不产生 token usage。
    """

    def __init__(
        self,
        cfg: LLMConfig,
        usage: UsageTracker,
        *,
        provider: str,
        generation_options: GenerationOptions | None = None,
        telemetry_sink: CallTelemetrySink | None = None,
    ) -> None:
        self.provider = provider
        self.cfg = cfg
        self.usage = usage
        self.generation_options = generation_options
        self.telemetry_sink = telemetry_sink

    def complete(
        self,
        messages: Messages,
        model_ref: ModelRef,
        *,
        json_mode: bool = False,
        max_tokens: int | None = None,
        stage: str | None = None,
        agent: str,
        operation: str,
        logical_call_id: str | None = None,
        attempt_counter: list[int] | None = None,
    ) -> str:
        started_at = (
            datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )
        started = time.monotonic()
        validate_generation_options(
            capabilities_for(self.provider, model_ref.model),
            model_ref,
            self.generation_options,
        )
        if attempt_counter is not None:
            attempt_counter[0] += 1
            attempt_index = attempt_counter[0]
        else:
            attempt_index = 1
        self.usage.begin_attempt()
        content = "[]" if json_mode else ""
        if self.telemetry_sink is not None:
            try:
                request = {"model": model_ref.model, "messages": messages, "stream": False}
                request_hash = hashlib.sha256(
                    json.dumps(
                        request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    ).encode()
                ).hexdigest()
                self.telemetry_sink.record(
                    CallAttemptTelemetry(
                        schema_version=1,
                        logical_call_id=logical_call_id or uuid.uuid4().hex,
                        attempt_index=attempt_index,
                        started_at=started_at,
                        elapsed_ms=round((time.monotonic() - started) * 1000),
                        stage=stage,
                        agent=agent,
                        operation=operation,
                        provider=self.provider,
                        requested_model=model_ref.model,
                        resolved_model=None,
                        reasoning_enabled=model_ref.reasoning_enabled,
                        reasoning_effort=model_ref.reasoning_effort,
                        temperature=(
                            float(self.generation_options.temperature)
                            if self.generation_options is not None
                            and self.generation_options.temperature is not None
                            else None
                        ),
                        seed=self.generation_options.seed
                        if self.generation_options is not None
                        else None,
                        json_mode=json_mode,
                        max_tokens=max_tokens,
                        status="success",
                        retry_class=None,
                        http_status=None,
                        finish_reason=None,
                        response_id=None,
                        prompt_tokens=0,
                        completion_tokens=0,
                        total_tokens=0,
                        cache_hit_tokens=0,
                        cache_miss_tokens=0,
                        reasoning_tokens=0,
                        billed_usage_unknown=True,
                        request_sha256=request_hash,
                        response_sha256=hashlib.sha256(content.encode()).hexdigest(),
                    )
                )
            except Exception:
                warn_telemetry_failure()
        self.usage.record_attempt_result(
            agent=agent,
            operation=operation,
            provider=self.provider,
            model_ref=model_ref,
        )
        return content
