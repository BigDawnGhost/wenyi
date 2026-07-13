"""LLM 抽象接口与具体实现。

设计要点：
- 三档 tier："strong"（deepseek-v4-pro + thinking，翻译/润色/分析/审计）、
  "cheap"（deepseek-v4-flash + thinking，审校/一致性等判断类）、
  "fast"（deepseek-v4-flash 免思考，梗概/术语抽取/回译等机械任务——
  thinking 推理 token 按输出计费，机械任务关掉可大幅省钱提速）。
  缺档时按回退链向"更便宜优先"回退（fast→cheap→strong），老双档配置行为不变。
- complete() 返回纯文本；complete_json() 强制 JSON 输出并 loose 解析。
- DeepSeekClient 经由 OpenAI SDK 调 https://api.deepseek.com，openai 惰性导入；
  未装 openai 时仍可用 FakeClient 跑通离线流程（切分/对齐/术语库/状态机）。
"""

from __future__ import annotations

import json
import re
import threading
from abc import ABC, abstractmethod
from typing import Any, Callable, Optional

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..config import Config, LLMConfig, TierConfig

Messages = list[dict[str, str]]

# 缺档回退链：向"更便宜优先"回退，绝不因缺档反而升到更贵的档
_TIER_FALLBACK = {"fast": ("cheap", "strong"), "cheap": ("strong",), "strong": ()}


def resolve_tier(tiers: dict[str, TierConfig], tier: str) -> TierConfig:
    """按回退链解析 tier 配置。缺 strong 时 KeyError（与旧行为一致）。"""
    if tier in tiers:
        return tiers[tier]
    for fb in _TIER_FALLBACK.get(tier, ("strong",)):
        if fb in tiers:
            return tiers[fb]
    return tiers["strong"]


# ── JSON 宽松解析 ────────────────────────────────────────────────────────
def _repair_unescaped_quotes(text: str) -> str:
    """转义 JSON 字符串值内部未转义的 ASCII 双引号。

    部分模型（尤其无原生 JSON 模式的 provider）会在译文里原样输出英文引号。
    启发式：字符串内的 `"` 后面（跳过空白）若不是 `,:]}`，视为内容引号转义之。
    中文译文以全角标点为主，误判面极小；仅作为常规解析失败后的兜底。
    """
    out: list[str] = []
    in_str = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if not in_str:
            if c == '"':
                in_str = True
            out.append(c)
        elif c == "\\" and i + 1 < n:
            out.append(text[i : i + 2])
            i += 2
            continue
        elif c == '"':
            j = i + 1
            while j < n and text[j] in " \t\r\n":
                j += 1
            if j >= n or text[j] in ",:]}":
                in_str = False
                out.append(c)
            else:
                out.append('\\"')
        else:
            out.append(c)
        i += 1
    return "".join(out)


def parse_json_loose(text: str) -> Any:
    """从模型输出里尽力解析 JSON。

    优先直接 json.loads；失败则剥离 ```json 围栏并截取首个 {…}/[…] 块再试。
    """
    text = (text or "").strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    # 去掉 markdown 代码围栏
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        inner = fenced.group(1).strip()
        try:
            return json.loads(inner)
        except Exception:
            text = inner
    # 截取首个 JSON 数组或对象
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        start = text.find(open_ch)
        end = text.rfind(close_ch)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except Exception:
                continue
    # 从首个 {/[ 起解析第一个完整 JSON 值，忽略尾部多余字符（如重复的 }）
    starts = [i for i in (text.find("{"), text.find("[")) if i != -1]
    if starts:
        try:
            value, _ = json.JSONDecoder().raw_decode(text[min(starts):])
            return value
        except Exception:
            pass
    # 最后兜底：修复字符串内未转义的引号，再从完整文本解析首个 JSON 值。
    # 必须先做这一步：若同时有未转义引号和尾部多余字符，直接截取内部数组
    # 会丢掉外层对象（如 {"translations": [...]}）。
    repaired = _repair_unescaped_quotes(text)
    starts = [i for i in (repaired.find("{"), repaired.find("[")) if i != -1]
    if starts:
        try:
            value, _ = json.JSONDecoder().raw_decode(repaired[min(starts):])
            return value
        except Exception:
            pass

    # 修复后仍无法解析时，才依次尝试完整文本和对象/数组片段。
    for candidate in (text, *(
        text[s : e + 1]
        for o, c in (("[", "]"), ("{", "}"))
        for s, e in [(text.find(o), text.rfind(c))]
        if s != -1 and e > s
    )):
        try:
            return json.loads(_repair_unescaped_quotes(candidate))
        except Exception:
            continue
    raise ValueError(f"无法解析为 JSON：{text[:200]!r}")


# ── Token 用量统计 ────────────────────────────────────────────────────────
_USAGE_FIELDS = (
    "calls", "prompt_tokens", "completion_tokens", "total_tokens",
    "cache_hit_tokens", "cache_miss_tokens",
)


def _usage_int(usage: Any, name: str) -> int:
    """从响应 usage 对象/字典读取整数字段，缺失或非数返回 0。"""
    val = getattr(usage, name, None)
    if val is None and isinstance(usage, dict):
        val = usage.get(name)
    try:
        return int(val) if val is not None else 0
    except (TypeError, ValueError):
        return 0


def _hit_rate(hit: int, miss: int) -> float:
    total = hit + miss
    return round(hit / total, 4) if total else 0.0


def _usage_summary_from_tiers(by_tier: dict[str, dict[str, int]]) -> dict[str, Any]:
    """由各档计数生成规范汇总，并重新计算总计与缓存命中率。"""
    tiers: dict[str, dict[str, Any]] = {
        tier: {field: int(values.get(field, 0)) for field in _USAGE_FIELDS}
        for tier, values in by_tier.items()
    }
    totals: dict[str, Any] = dict.fromkeys(_USAGE_FIELDS, 0)
    for values in tiers.values():
        for field in _USAGE_FIELDS:
            totals[field] += values[field]
    for slot in (*tiers.values(), totals):
        slot["cache_hit_rate"] = _hit_rate(
            slot["cache_hit_tokens"], slot["cache_miss_tokens"]
        )
    return {"totals": totals, "by_tier": tiers}


def usage_delta(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    """计算两个累计快照之间的非负增量，用于避免重复落盘。"""
    current_tiers = current.get("by_tier") or {}
    previous_tiers = previous.get("by_tier") or {}
    delta: dict[str, dict[str, int]] = {}
    for tier, values in current_tiers.items():
        old = previous_tiers.get(tier) or {}
        slot = {
            field: max(0, _usage_int(values, field) - _usage_int(old, field))
            for field in _USAGE_FIELDS
        }
        if any(slot.values()):
            delta[tier] = slot
    return _usage_summary_from_tiers(delta)


def merge_usage_summaries(
    accumulated: dict[str, Any], increment: dict[str, Any]
) -> dict[str, Any]:
    """把一次运行增量合并进某本书的历史累计用量。"""
    merged: dict[str, dict[str, int]] = {}
    for summary in (accumulated, increment):
        for tier, values in (summary.get("by_tier") or {}).items():
            slot = merged.setdefault(tier, dict.fromkeys(_USAGE_FIELDS, 0))
            for field in _USAGE_FIELDS:
                slot[field] += _usage_int(values, field)
    return _usage_summary_from_tiers(merged)


class UsageTracker:
    """线程安全的 token 用量累加器，按 tier 分档统计（worker 线程并发共享一个 client）。

    DeepSeek 的 usage 里 prompt_cache_hit_tokens + prompt_cache_miss_tokens == prompt_tokens；
    缓存命中率 = cache_hit /(cache_hit + cache_miss)。fake provider 不产生 usage，保持空。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_tier: dict[str, dict[str, int]] = {}

    def record(self, tier: str, usage: Any) -> None:
        """累加一次响应的 usage；usage 缺失时静默跳过（不影响正常返回）。"""
        if usage is None:
            return
        pt = _usage_int(usage, "prompt_tokens")
        ct = _usage_int(usage, "completion_tokens")
        tt = _usage_int(usage, "total_tokens") or (pt + ct)
        hit = _usage_int(usage, "prompt_cache_hit_tokens")
        miss = _usage_int(usage, "prompt_cache_miss_tokens")
        with self._lock:
            slot = self._by_tier.setdefault(tier, dict.fromkeys(_USAGE_FIELDS, 0))
            slot["calls"] += 1
            slot["prompt_tokens"] += pt
            slot["completion_tokens"] += ct
            slot["total_tokens"] += tt
            slot["cache_hit_tokens"] += hit
            slot["cache_miss_tokens"] += miss

    def summary(self) -> dict[str, Any]:
        """返回 {"totals": {...}, "by_tier": {tier: {...}}}，各档含 cache_hit_rate。"""
        with self._lock:
            by_tier = {t: dict(v) for t, v in self._by_tier.items()}
        return _usage_summary_from_tiers(by_tier)


# ── 抽象接口 ──────────────────────────────────────────────────────────────
class LLMClient(ABC):
    """所有 provider 实现此接口。"""

    def __init__(self) -> None:
        self.usage = UsageTracker()

    def usage_summary(self) -> dict[str, Any]:
        """返回累计 token 用量快照（totals + by_tier + cache_hit_rate）。"""
        return self.usage.summary()

    @abstractmethod
    def complete(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        json_mode: bool = False,
        max_tokens: Optional[int] = None,
    ) -> str:
        """返回模型回复的纯文本。"""
        raise NotImplementedError

    def complete_json(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        max_tokens: Optional[int] = None,
    ) -> Any:
        """要求 JSON 输出并解析。"""
        text = self.complete(messages, tier=tier, json_mode=True, max_tokens=max_tokens)
        return parse_json_loose(text)


# ── DeepSeek（OpenAI SDK 兼容）────────────────────────────────────────────
class DeepSeekClient(LLMClient):
    def __init__(self, cfg: LLMConfig):
        super().__init__()
        self.cfg = cfg
        if not cfg.tiers:
            raise ValueError("配置缺少 llm.tiers")
        self._client = None  # 惰性创建
        self._client_lock = threading.Lock()  # 预扫并行时防惰性初始化竞态

    def _ensure_client(self):
        with self._client_lock:
            return self._ensure_client_locked()

    def _ensure_client_locked(self):
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as e:  # pragma: no cover
                raise RuntimeError(
                    "需要 openai SDK：pip install openai（或把 llm.provider 设为 fake 做离线测试）"
                ) from e
            api_key = self.cfg.api_key
            if not api_key:
                raise RuntimeError(
                    f"未设置环境变量 {self.cfg.api_key_env}（DeepSeek API key）"
                )
            self._client = OpenAI(
                api_key=api_key,
                base_url=self.cfg.base_url,
                timeout=self.cfg.timeout,
            )
        return self._client

    def complete(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        json_mode: bool = False,
        max_tokens: Optional[int] = None,
    ) -> str:
        tcfg = resolve_tier(self.cfg.tiers, tier)
        client = self._ensure_client()

        kwargs: dict[str, Any] = {
            "model": tcfg.model,
            "messages": messages,
            "stream": False,
        }
        if tcfg.thinking:
            kwargs["reasoning_effort"] = tcfg.reasoning_effort
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if max_tokens:
            # DeepSeek thinking 模式下 max_tokens 含推理 token（总输出上限）。
            # 带紧上限的调用若经回退链落到 thinking 档，抬到安全下限防推理被截断。
            kwargs["max_tokens"] = max(max_tokens, 4096) if tcfg.thinking else max_tokens

        # 网络/限流/超时 → tenacity 指数退避重试（最多 max_retries 次重试）
        @retry(
            stop=stop_after_attempt(self.cfg.max_retries + 1),
            wait=wait_exponential(multiplier=1, max=30),
            retry=retry_if_exception_type(Exception),
            reraise=True,
        )
        def _call() -> str:
            resp = client.chat.completions.create(**kwargs)
            self.usage.record(tier, getattr(resp, "usage", None))
            return resp.choices[0].message.content or ""

        return _call()


# ── 离线 Fake（测试 / 不发网络请求）───────────────────────────────────────
class FakeClient(LLMClient):
    """可编程的离线 client。

    handler(messages, tier, json_mode) -> str。默认对 json_mode 返回 "[]"，
    否则返回空串。测试通过注入 handler 模拟翻译/抽取等行为。
    """

    def __init__(self, handler: Optional[Callable[[Messages, str, bool], str]] = None):
        super().__init__()
        self.handler = handler
        self.calls: list[dict[str, Any]] = []  # 记录调用，便于断言

    def complete(
        self,
        messages: Messages,
        *,
        tier: str = "strong",
        json_mode: bool = False,
        max_tokens: Optional[int] = None,
    ) -> str:
        self.calls.append({"messages": messages, "tier": tier,
                           "json_mode": json_mode, "max_tokens": max_tokens})
        if self.handler is not None:
            return self.handler(messages, tier, json_mode)
        return "[]" if json_mode else ""


def build_client(config: Config) -> LLMClient:
    provider = config.llm.provider.lower()
    if provider == "deepseek":
        return DeepSeekClient(config.llm)
    if provider == "fake":
        return FakeClient()
    raise ValueError(f"未知 provider：{provider}（支持 deepseek / fake）")
