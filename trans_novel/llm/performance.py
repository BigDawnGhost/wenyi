"""进程内 LLM 调用性能采样。

这里刻意与 ``usage.json`` 的计费统计分离：性能样本只服务于当前命令的
进度估算，不落盘，也不改变已有用量文件格式。
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from threading import Lock

Clock = Callable[[], float]


@dataclass(frozen=True)
class LLMCallMetric:
    """一次已成功返回的逻辑 LLM 调用及其端到端耗时。"""

    sequence: int
    provider: str
    model: str
    tier: str
    stage: str | None
    completion_tokens: int
    started_at: float
    finished_at: float
    prompt_tokens: int = 0
    total_tokens: int = 0

    @property
    def elapsed_seconds(self) -> float:
        """返回非负墙钟耗时；使用单调时钟，不受系统时间校准影响。"""
        return max(0.0, self.finished_at - self.started_at)


@dataclass(frozen=True)
class PerformanceSnapshot:
    """性能追踪器的不可变快照。"""

    samples: tuple[LLMCallMetric, ...]
    total_calls: int
    total_completion_tokens: int
    last_sequence: int
    token_rate: float | None
    total_prompt_tokens: int = 0
    total_tokens: int = 0


def _valid_token_metrics(metrics: Iterable[LLMCallMetric]) -> list[LLMCallMetric]:
    """只保留同时具有 token 和正耗时的样本。"""
    return [
        metric
        for metric in metrics
        if metric.completion_tokens > 0 and metric.elapsed_seconds > 0
    ]


def recent_valid_metrics(
    metrics: Iterable[LLMCallMetric],
    limit: int = 8,
) -> list[LLMCallMetric]:
    """返回最近的有效 token 样本，零 token 样本不占用窗口名额。"""
    valid = _valid_token_metrics(metrics)
    return valid[-max(1, limit) :]


def effective_token_rate(metrics: Iterable[LLMCallMetric]) -> float | None:
    """按调用时间区间并集计算有效吞吐率。

    并发请求的时间区间会重叠。若直接累加各调用耗时，会把四路并发错误地
    当成串行；合并区间后，分母才是用户实际等待的墙钟时间。
    """
    valid = _valid_token_metrics(metrics)
    if not valid:
        return None

    intervals = sorted((metric.started_at, metric.finished_at) for metric in valid)
    merged_seconds = 0.0
    current_start, current_end = intervals[0]
    for start, end in intervals[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
            continue
        merged_seconds += current_end - current_start
        current_start, current_end = start, end
    merged_seconds += current_end - current_start
    if merged_seconds <= 0:
        return None
    return sum(metric.completion_tokens for metric in valid) / merged_seconds


def per_worker_token_rate(metrics: Iterable[LLMCallMetric]) -> float | None:
    """按各调用耗时之和计算单 worker 等效吞吐率。"""
    valid = _valid_token_metrics(metrics)
    elapsed = sum(metric.elapsed_seconds for metric in valid)
    if elapsed <= 0:
        return None
    return sum(metric.completion_tokens for metric in valid) / elapsed


class PerformanceTracker:
    """线程安全地保存当前进程最近的 LLM 调用性能。"""

    def __init__(
        self,
        *,
        clock: Clock = time.monotonic,
        history_size: int = 256,
        rate_window: int = 8,
    ) -> None:
        self._clock = clock
        self._history: deque[LLMCallMetric] = deque(maxlen=max(8, history_size))
        self._rate_window = max(1, rate_window)
        self._valid_history: deque[LLMCallMetric] = deque(maxlen=self._rate_window)
        self._lock = Lock()
        self._sequence = 0
        self._total_calls = 0
        self._total_prompt_tokens = 0
        self._total_completion_tokens = 0
        self._total_tokens = 0

    def now(self) -> float:
        """读取与采样一致的单调时钟，便于覆盖完整重试周期。"""
        return self._clock()

    def record(
        self,
        *,
        provider: str,
        model: str,
        tier: str,
        stage: str | None,
        completion_tokens: int,
        started_at: float,
        prompt_tokens: int = 0,
        total_tokens: int | None = None,
        finished_at: float | None = None,
    ) -> LLMCallMetric:
        """记录一次成功返回；缺失 usage 时 completion_tokens 传 0。"""
        ended = self.now() if finished_at is None else finished_at
        start = min(started_at, ended)
        normalized_prompt = max(0, int(prompt_tokens))
        normalized_completion = max(0, int(completion_tokens))
        normalized_total = (
            normalized_prompt + normalized_completion
            if total_tokens is None
            else max(0, int(total_tokens))
        )
        if normalized_total == 0 and (normalized_prompt or normalized_completion):
            normalized_total = normalized_prompt + normalized_completion
        with self._lock:
            self._sequence += 1
            metric = LLMCallMetric(
                sequence=self._sequence,
                provider=provider,
                model=model,
                tier=tier,
                stage=stage,
                completion_tokens=normalized_completion,
                started_at=start,
                finished_at=ended,
                prompt_tokens=normalized_prompt,
                total_tokens=normalized_total,
            )
            self._history.append(metric)
            if metric.completion_tokens > 0 and metric.elapsed_seconds > 0:
                self._valid_history.append(metric)
            self._total_calls += 1
            self._total_prompt_tokens += metric.prompt_tokens
            self._total_completion_tokens += metric.completion_tokens
            self._total_tokens += metric.total_tokens
            return metric

    def snapshot(self) -> PerformanceSnapshot:
        """复制最近样本并计算最近八个有效样本的并发有效 tok/s。"""
        with self._lock:
            history = tuple(self._history)
            rate_window = tuple(self._valid_history)
            total_calls = self._total_calls
            total_prompt_tokens = self._total_prompt_tokens
            total_completion_tokens = self._total_completion_tokens
            all_tokens = self._total_tokens
            last_sequence = self._sequence
        return PerformanceSnapshot(
            samples=history,
            total_calls=total_calls,
            total_completion_tokens=total_completion_tokens,
            last_sequence=last_sequence,
            token_rate=effective_token_rate(rate_window),
            total_prompt_tokens=total_prompt_tokens,
            total_tokens=all_tokens,
        )
