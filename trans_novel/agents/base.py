"""Agent 基类：统一 client/config/src/tgt 初始化，与带默认值的 LLM 调用帮助方法。

各 agent 的"渲染 system/user → complete_json → 失败回退默认值"模式收敛到这里；
默认值语义留在 agent 层（LLM provider 层不掺业务回退）。
workflow 组合根按解析后的语言构造每个 agent，依赖每个 agent 都有 .src 属性——基类把该契约显式化。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from trans_novel.config import Config
from trans_novel.ingest.models import sanitize_generated_text
from trans_novel.llm.base import LLMClient
from trans_novel.llm.errors import JSONParseError, LLMError
from trans_novel.llm.usage_persistence import UsagePersistenceError


def _sanitize_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_generated_text(value)
    if isinstance(value, list):
        return [_sanitize_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _sanitize_json_value(item) for key, item in value.items()}
    return value


_RAISE = object()  # 哨兵：未提供 default 时异常照常抛出，由调用方自理


class WorkflowProtocolError(RuntimeError, LLMError):
    """Agent 输出的协议错误（缺失键/形状错误/数量不符等）。

    workflow 必需节点据此按 protocol 失败分类（可重试、失败态落盘），
    可与“Provider 失败”和“业务拒绝”区分。reason 是稳定标识；message 可选，
    用于提供可读说明（未指定时沿用 reason）。
    """

    def __init__(self, reason: str, message: str | None = None):
        self.reason = reason
        self.message = message
        super().__init__(message or reason)


T = TypeVar("T")


def retry_protocol(operation: Callable[[], T], *, retries: int) -> T:
    """Retry one logical model call when parsing or output validation rejects its response."""
    for attempt in range(retries + 1):
        try:
            return operation()
        except (WorkflowProtocolError, JSONParseError):
            if attempt == retries:
                raise
    raise AssertionError("protocol retry loop must return or raise")


class Agent:
    def __init__(
        self,
        client: LLMClient,
        config: Config,
        *,
        src: str | None = None,
        tgt: str | None = None,
    ):
        self.client = client
        self.config = config
        self.src = config.source_lang if src is None else src
        self.tgt = config.target_lang if tgt is None else tgt

    def _ask_json(
        self,
        system: str,
        user: str,
        *,
        agent: str,
        operation: str,
        key: str | None = None,
        default: Any = _RAISE,
        max_tokens: int | None = None,
        strict: bool = False,
        items_are_dicts: bool = False,
    ) -> Any:
        """system/user → complete_json。

        异常时返回 default（未给 default 则照常抛出，如 Translator 交由重试逻辑处理）；
        strict=True 时异常一律照常抛出（workflow 必需节点用，杜绝“provider 失败伪装
        成成功空结果”）。key 给出时：结果为 dict 取 data[key]（缺失回退）；结果为
        非空 list 直接用；否则回退。agent 是内置功能标识；operation 是内部业务标签。
        两者都在调用点显式硬编码，缺一不可。
        """
        try:
            data = self.client.complete_json(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                max_tokens=max_tokens,
                stage=type(self).__name__,
                agent=agent,
                operation=operation,
            )
        except UsagePersistenceError:
            raise
        except Exception as exc:
            if default is _RAISE or strict:
                if strict and isinstance(exc, JSONParseError):
                    # 解析失败（parse_json_loose 抛 JSONParseError）归一为协议错误，
                    # 避免被通用 ValueError 分支归为业务错误；其余 ValueError 原样向上抛出。
                    raise WorkflowProtocolError("invalid_json") from exc
                raise
            return default
        data = _sanitize_json_value(data)
        if key is None:
            return data
        fb = None if default is _RAISE else default
        if isinstance(data, dict):
            if key in data:
                value = data.get(key, fb)
                if strict:
                    if value is None:
                        # 必需节点：显式 null 是不合法的空结果（须为有效空列表）。
                        raise WorkflowProtocolError(f"null_value:{key}")
                    if not isinstance(value, list):
                        # 必需节点的键控响应必须是集合（列表）；错误形状不得被
                        # dict_items() 宽松过滤成成功空结果。
                        raise WorkflowProtocolError(f"invalid_collection:{key}")
                    if items_are_dicts and not all(isinstance(item, dict) for item in value):
                        # 集合元素必须是 dict：malformed 元素不得被 dict_items()
                        # 静默丢弃成“零发现成功”。
                        raise WorkflowProtocolError(f"invalid_items:{key}")
                return value
            if strict:
                # 必需节点：缺失键 = 协议错误（不得把默认值伪装成成功空结果）。
                raise WorkflowProtocolError(f"missing_key:{key}")
            return fb
        if strict:
            raise WorkflowProtocolError("invalid_schema")
        return data if data else fb

    def _ask_text(
        self,
        system: str,
        user: str,
        *,
        agent: str,
        operation: str,
        default: str = "",
        max_tokens: int | None = None,
        strict: bool = False,
    ) -> str:
        """complete 纯文本并 strip；异常返回 default（strict=True 时照常抛出）。"""
        try:
            return sanitize_generated_text(
                self.client.complete(
                    [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    max_tokens=max_tokens,
                    stage=type(self).__name__,
                    agent=agent,
                    operation=operation,
                )
                or ""
            ).strip()
        except UsagePersistenceError:
            raise
        except Exception:
            if strict:
                raise
            return default

    @staticmethod
    def dict_items(items: Any) -> list[dict]:
        """过滤出 dict 元素（issues/terms 等模型返回列表的通用清洗）。"""
        return [i for i in items or [] if isinstance(i, dict)]
