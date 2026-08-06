"""基于当前进程 LLM 吞吐率的阶段与全程 ETA 估算。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock

from ..llm.performance import (
    LLMCallMetric,
    PerformanceSnapshot,
    PerformanceTracker,
    effective_token_rate,
    per_worker_token_rate,
    recent_valid_metrics,
)

Clock = Callable[[], float]
ProgressCallback = Callable[[int, int, str], None]


@dataclass(frozen=True)
class ProgressEstimate:
    """供 UI 读取的不可变 ETA 与 token 预算快照。"""

    stage_remaining_seconds: float | None
    overall_remaining_seconds: float | None
    token_rate: float | None
    sample_count: int
    updated_at: float
    finishing: bool = False
    used_tokens: int = 0
    stage_remaining_tokens: float | None = None
    overall_remaining_tokens: float | None = None
    estimated_total_tokens: float | None = None


@dataclass
class _StageState:
    key: str
    label: str
    kind: str
    total_work: float
    completed_work: float
    workers: int
    tier: str | None
    manual: bool
    started_at: float | None = None
    baseline_tokens: int = 0
    baseline_total_tokens: int = 0
    baseline_sequence: int = 0
    start_completed_work: float = 0.0
    active_work: float = 0.0
    active_sequence_baseline: int = 0
    finished: bool = False
    progress_total: int | None = None
    progress_done: int = 0
    stats_frozen: bool = False
    final_density: float | None = None
    final_total_token_density: float | None = None
    final_aggregate_rate: float | None = None
    final_per_worker_rate: float | None = None
    final_elapsed_per_work: float | None = None
    final_sample_count: int = 0


@dataclass(frozen=True)
class _StageStats:
    density: float | None
    total_token_density: float | None
    aggregate_rate: float | None
    per_worker_rate: float | None
    elapsed_per_work: float | None
    sample_count: int


class _TrackedProgress:
    """保持三参数回调不变，同时把同一事件送入估算器。"""

    def __init__(self, callback: ProgressCallback, estimator: PipelineETAEstimator) -> None:
        self.callback = callback
        self.estimator = estimator

    def __call__(self, done: int, total: int, label: str) -> None:
        self.estimator.note_progress(done, total, label)
        self.callback(done, total, label)


class PipelineETAEstimator:
    """维护动态工作计划，并把 token 速度换算为剩余墙钟时间。"""

    def __init__(
        self,
        performance: PerformanceTracker,
        *,
        clock: Clock = time.monotonic,
    ) -> None:
        self._performance = performance
        self._clock = clock
        self._lock = RLock()
        initial = performance.snapshot()
        self._run_baseline_sequence = initial.last_sequence
        self._run_baseline_total_tokens = initial.total_tokens
        self._stages: dict[str, _StageState] = {}
        self._current_key: str | None = None
        self._auto_sequence = 0
        self._finishing = False
        self._dirty = True
        self._last_performance_sequence = initial.last_sequence
        self._stage_deadline: float | None = None
        self._overall_deadline: float | None = None
        self._cached_rate: float | None = None
        self._cached_sample_count = 0
        self._cached_used_tokens = 0
        self._cached_stage_remaining_tokens: float | None = None
        self._cached_overall_remaining_tokens: float | None = None
        self._cached_estimated_total_tokens: float | None = None

    def track(self, callback: ProgressCallback | None) -> ProgressCallback | None:
        """包装 UI 回调；嵌套流水线重复传递时保持单层包装。"""
        if callback is None:
            return None
        if isinstance(callback, _TrackedProgress) and callback.estimator is self:
            return callback
        return _TrackedProgress(callback, self)

    def plan_stage(
        self,
        key: str,
        total_work: float,
        *,
        kind: str = "chars",
        workers: int = 1,
        tier: str | None = None,
        label: str | None = None,
    ) -> None:
        """把确定会执行的阶段加入全程 ETA；重复调用只扩充计划。"""
        total = max(0.0, float(total_work))
        with self._lock:
            existing = self._stages.get(key)
            if existing is None:
                self._stages[key] = _StageState(
                    key=key,
                    label=label or key,
                    kind=kind,
                    total_work=total,
                    completed_work=0.0,
                    workers=max(1, workers),
                    tier=tier,
                    manual=True,
                )
            else:
                existing.total_work = max(existing.total_work, total)
                existing.workers = max(1, workers)
                existing.tier = tier or existing.tier
                if label:
                    existing.label = label
            self._dirty = True

    def add_stage_work(self, key: str, amount: float) -> None:
        """把运行时才发现的工作加入既有阶段。"""
        value = max(0.0, float(amount))
        if value <= 0:
            return
        with self._lock:
            stage = self._stages.get(key)
            if stage is None:
                self.plan_stage(key, value)
                return
            stage.total_work += value
            stage.finished = False
            self._dirty = True

    def begin_stage(
        self,
        key: str,
        *,
        label: str,
        total_work: float | None = None,
        kind: str = "chars",
        workers: int = 1,
        tier: str | None = None,
    ) -> None:
        """激活一个阶段；预先规划过的阶段会复用其总工作量。"""
        if total_work is not None:
            self.plan_stage(
                key,
                total_work,
                kind=kind,
                workers=workers,
                tier=tier,
                label=label,
            )
        snapshot = self._performance.snapshot()
        now = self._clock()
        with self._lock:
            stage = self._stages.get(key)
            if stage is None:
                stage = _StageState(
                    key=key,
                    label=label,
                    kind=kind,
                    total_work=max(0.0, float(total_work or 0.0)),
                    completed_work=0.0,
                    workers=max(1, workers),
                    tier=tier,
                    manual=True,
                )
                self._stages[key] = stage
            stage.label = label
            stage.kind = kind
            stage.workers = max(1, workers)
            stage.tier = tier or stage.tier
            stage.manual = True
            if stage.started_at is None or stage.finished:
                stage.started_at = now
                stage.baseline_tokens = snapshot.total_completion_tokens
                stage.baseline_total_tokens = snapshot.total_tokens
                stage.baseline_sequence = snapshot.last_sequence
                stage.start_completed_work = stage.completed_work
                stage.active_work = 0.0
                stage.active_sequence_baseline = snapshot.last_sequence
                stage.finished = False
                stage.stats_frozen = False
            self._current_key = key
            self._finishing = False
            self._dirty = True

    def set_active_work(self, amount: float) -> None:
        """声明当前串行工作项，使首个响应后即可推算 token 密度。"""
        snapshot = self._performance.snapshot()
        with self._lock:
            stage = self._current_stage()
            if stage is None:
                return
            stage.active_work = max(0.0, float(amount))
            stage.active_sequence_baseline = snapshot.last_sequence
            self._dirty = True

    def advance(self, amount: float | None = None) -> None:
        """完成当前阶段的一块工作；省略 amount 时使用 active_work。"""
        with self._lock:
            stage = self._current_stage()
            if stage is None:
                return
            value = stage.active_work if amount is None else max(0.0, float(amount))
            if value <= 0:
                stage.active_work = 0.0
                return
            if stage.total_work < stage.completed_work + value:
                stage.total_work = stage.completed_work + value
            stage.completed_work = min(stage.total_work, stage.completed_work + value)
            stage.active_work = 0.0
            self._dirty = True

    def finish_stage(self, key: str | None = None) -> None:
        """把阶段标记为完成；未知总量阶段保留已完成量。"""
        snapshot = self._performance.snapshot()
        now = self._clock()
        with self._lock:
            target = self._stages.get(key) if key is not None else self._current_stage()
            if target is None:
                return
            target.completed_work = max(target.completed_work, target.total_work)
            target.active_work = 0.0
            target.finished = True
            self._freeze_stage_stats(target, snapshot, now)
            self._dirty = True

    def mark_finishing(self) -> None:
        """进入无法可靠量化的本地报告/回填收尾。"""
        with self._lock:
            self._current_key = None
            self._finishing = True
            self._dirty = True

    def note_progress(self, done: int, total: int, label: str) -> None:
        """接收原三参数进度事件，并为未显式建模的阶段提供墙钟回退。"""
        snapshot = self._performance.snapshot()
        now = self._clock()
        with self._lock:
            current = self._current_stage()
            if current is not None and current.manual and not current.finished:
                current.label = label
                self._dirty = True
                return
            if total <= 0:
                self._current_key = None
                self._finishing = False
                self._dirty = True
                return

            needs_new = (
                current is None
                or current.manual
                or current.label != label
                or current.progress_total != total
                or done < current.progress_done
            )
            if needs_new:
                self._auto_sequence += 1
                key = f"auto:{self._auto_sequence}:{label}"
                current = _StageState(
                    key=key,
                    label=label,
                    kind="items",
                    total_work=max(0.0, float(total)),
                    completed_work=max(0.0, float(done)),
                    workers=1,
                    tier=None,
                    manual=False,
                    started_at=now,
                    baseline_tokens=snapshot.total_completion_tokens,
                    baseline_total_tokens=snapshot.total_tokens,
                    baseline_sequence=snapshot.last_sequence,
                    start_completed_work=max(0.0, float(done)),
                    active_sequence_baseline=snapshot.last_sequence,
                    progress_total=total,
                    progress_done=done,
                )
                self._stages[key] = current
                self._current_key = key
            else:
                current.completed_work = max(current.completed_work, float(done))
                current.progress_done = done
                if done >= total:
                    current.finished = True
                    self._freeze_stage_stats(current, snapshot, now)
            self._finishing = False
            self._dirty = True

    def snapshot(self) -> ProgressEstimate:
        """读取动态倒计时；只有新样本或进度变化时才重算截止时间。"""
        performance = self._performance.snapshot()
        now = self._clock()
        with self._lock:
            if self._dirty or performance.last_sequence != self._last_performance_sequence:
                self._recalculate(performance, now)
                self._last_performance_sequence = performance.last_sequence
                self._dirty = False

            stage_seconds = self._remaining_to_deadline(self._stage_deadline, now)
            overall_seconds = self._remaining_to_deadline(self._overall_deadline, now)
            return ProgressEstimate(
                stage_remaining_seconds=stage_seconds,
                overall_remaining_seconds=overall_seconds,
                token_rate=self._cached_rate,
                sample_count=self._cached_sample_count,
                updated_at=now,
                finishing=self._finishing,
                used_tokens=self._cached_used_tokens,
                stage_remaining_tokens=self._cached_stage_remaining_tokens,
                overall_remaining_tokens=self._cached_overall_remaining_tokens,
                estimated_total_tokens=self._cached_estimated_total_tokens,
            )

    def _current_stage(self) -> _StageState | None:
        return self._stages.get(self._current_key) if self._current_key is not None else None

    @staticmethod
    def _remaining_to_deadline(deadline: float | None, now: float) -> float | None:
        if deadline is None:
            return None
        return max(0.0, deadline - now)

    def _stage_stats(
        self,
        stage: _StageState,
        performance: PerformanceSnapshot,
        now: float,
    ) -> _StageStats:
        if stage.stats_frozen:
            return _StageStats(
                density=stage.final_density,
                total_token_density=stage.final_total_token_density,
                aggregate_rate=stage.final_aggregate_rate,
                per_worker_rate=stage.final_per_worker_rate,
                elapsed_per_work=stage.final_elapsed_per_work,
                sample_count=stage.final_sample_count,
            )
        if stage.started_at is None:
            return _StageStats(None, None, None, None, None, 0)

        tokens = max(0, performance.total_completion_tokens - stage.baseline_tokens)
        total_tokens = max(0, performance.total_tokens - stage.baseline_total_tokens)
        observed_work = max(0.0, stage.completed_work - stage.start_completed_work)
        if (
            stage is self._current_stage()
            and stage.active_work > 0
            and performance.last_sequence > stage.active_sequence_baseline
        ):
            observed_work += stage.active_work

        metrics = recent_valid_metrics(
            (
                metric
                for metric in performance.samples
                if metric.sequence > stage.baseline_sequence
            ),
            8,
        )
        valid_count = len(metrics)
        density = tokens / observed_work if tokens > 0 and observed_work > 0 else None
        total_token_density = (
            total_tokens / observed_work if total_tokens > 0 and observed_work > 0 else None
        )
        aggregate_rate = effective_token_rate(metrics)
        worker_rate = per_worker_token_rate(metrics)
        elapsed = max(0.0, now - stage.started_at)
        elapsed_per_work = elapsed / observed_work if elapsed > 0 and observed_work > 0 else None
        return _StageStats(
            density=density,
            total_token_density=total_token_density,
            aggregate_rate=aggregate_rate,
            per_worker_rate=worker_rate,
            elapsed_per_work=elapsed_per_work,
            sample_count=valid_count,
        )

    def _freeze_stage_stats(
        self,
        stage: _StageState,
        performance: PerformanceSnapshot,
        now: float,
    ) -> None:
        """冻结已完成阶段的速率，防止后续阶段 token 被错误归入。"""
        stage.stats_frozen = False
        stats = self._stage_stats(stage, performance, now)
        stage.final_density = stats.density
        stage.final_total_token_density = stats.total_token_density
        stage.final_aggregate_rate = stats.aggregate_rate
        stage.final_per_worker_rate = stats.per_worker_rate
        stage.final_elapsed_per_work = stats.elapsed_per_work
        stage.final_sample_count = stats.sample_count
        stage.stats_frozen = True

    @staticmethod
    def _own_stage_seconds(stage: _StageState, stats: _StageStats) -> float | None:
        remaining = max(0.0, stage.total_work - stage.completed_work)
        if remaining <= 0:
            return 0.0
        if stats.density is not None and stats.aggregate_rate is not None:
            return remaining * stats.density / stats.aggregate_rate
        if stats.elapsed_per_work is not None:
            return remaining * stats.elapsed_per_work
        return None

    @staticmethod
    def _matching_stage_stats(
        stage: _StageState,
        references: list[tuple[_StageState, _StageStats]],
    ) -> list[_StageStats]:
        """依次返回同阶段族、同模型档位和本次运行同工作单位的统计。"""
        same_kind = [item for item in references if item[0].kind == stage.kind]
        stage_family = stage.key.partition(":")[0]
        candidate_groups = (
            [item for item in same_kind if item[0].key.partition(":")[0] == stage_family],
            [item for item in same_kind if stage.tier and item[0].tier == stage.tier],
            same_kind,
        )
        matched: list[_StageStats] = []
        seen: set[str] = set()
        for candidates in candidate_groups:
            for reference, stats in reversed(candidates):
                if reference.key in seen:
                    continue
                seen.add(reference.key)
                matched.append(stats)
        return matched

    def _recent_call_metrics(
        self,
        stage: _StageState,
        performance: PerformanceSnapshot,
    ) -> list[LLMCallMetric]:
        """优先返回当前命令中同模型档位的最近八次逻辑调用。"""
        recent_calls = [
            metric
            for metric in performance.samples
            if metric.sequence > self._run_baseline_sequence
        ]
        same_tier = [
            metric for metric in recent_calls if stage.tier and metric.tier == stage.tier
        ]
        return (same_tier or recent_calls)[-8:]

    def _future_stage_seconds(
        self,
        stage: _StageState,
        references: list[tuple[_StageState, _StageStats]],
        performance: PerformanceSnapshot,
    ) -> float | None:
        remaining = max(0.0, stage.total_work - stage.completed_work)
        if remaining <= 0:
            return 0.0

        for stats in self._matching_stage_stats(stage, references):
            if stats.density is not None and stats.per_worker_rate is not None:
                return remaining * stats.density / (
                    stats.per_worker_rate * max(1, stage.workers)
                )

        # 单次 QA 或尚未采样的审校块，以本次运行最近调用耗时作最佳估算。
        if stage.kind in {"blocks", "calls"}:
            recent_calls = [
                metric
                for metric in self._recent_call_metrics(stage, performance)
                if metric.elapsed_seconds > 0
            ]
            elapsed = [metric.elapsed_seconds for metric in recent_calls]
            if elapsed:
                return remaining * (sum(elapsed) / len(elapsed)) / max(1, stage.workers)
        return None

    def _own_stage_tokens(
        self,
        stage: _StageState,
        stats: _StageStats,
        performance: PerformanceSnapshot,
    ) -> float | None:
        """估算当前阶段尚未实际计入 usage 的总 token。"""
        remaining = max(0.0, stage.total_work - stage.completed_work)
        if (
            stage is self._current_stage()
            and stage.active_work > 0
            and performance.last_sequence > stage.active_sequence_baseline
        ):
            remaining = max(0.0, remaining - stage.active_work)
        if remaining <= 0:
            return 0.0
        if stats.total_token_density is None:
            return None
        return remaining * stats.total_token_density

    def _future_stage_tokens(
        self,
        stage: _StageState,
        references: list[tuple[_StageState, _StageStats]],
        performance: PerformanceSnapshot,
    ) -> float | None:
        """用同阶段/同档位样本外推尚未开始阶段的总 token。"""
        remaining = max(0.0, stage.total_work - stage.completed_work)
        if remaining <= 0:
            return 0.0
        for stats in self._matching_stage_stats(stage, references):
            if stats.total_token_density is not None:
                return remaining * stats.total_token_density
        if stage.kind in {"blocks", "calls"}:
            totals = [
                metric.total_tokens
                for metric in self._recent_call_metrics(stage, performance)
                if metric.total_tokens > 0
            ]
            if totals:
                return remaining * (sum(totals) / len(totals))
        return None

    def _recalculate(self, performance: PerformanceSnapshot, now: float) -> None:
        current = self._current_stage()
        started: list[tuple[_StageState, _StageStats]] = []
        for stage in self._stages.values():
            if stage.started_at is not None:
                started.append((stage, self._stage_stats(stage, performance, now)))

        current_stats = next((stats for stage, stats in started if stage is current), None)
        stage_seconds = (
            self._own_stage_seconds(current, current_stats)
            if current is not None and current_stats is not None
            else None
        )
        stage_tokens = (
            self._own_stage_tokens(current, current_stats, performance)
            if current is not None and current_stats is not None
            else None
        )

        estimates: list[float] = []
        token_estimates: list[float] = []
        unfinished_stages = 0
        for stage in self._stages.values():
            if stage.completed_work >= stage.total_work:
                continue
            unfinished_stages += 1
            stats = next((item for candidate, item in started if candidate is stage), None)
            value = self._own_stage_seconds(stage, stats) if stats is not None else None
            if value is None:
                value = self._future_stage_seconds(stage, started, performance)
            if value is not None:
                estimates.append(max(0.0, value))
            token_value = (
                self._own_stage_tokens(stage, stats, performance)
                if stats is not None
                else None
            )
            if token_value is None:
                token_value = self._future_stage_tokens(stage, started, performance)
            if token_value is not None:
                token_estimates.append(max(0.0, token_value))
        overall_seconds = sum(estimates) if estimates else None
        used_tokens = max(0, performance.total_tokens - self._run_baseline_total_tokens)
        if token_estimates:
            overall_tokens = sum(token_estimates)
        elif unfinished_stages == 0 and used_tokens > 0:
            overall_tokens = 0.0
        else:
            overall_tokens = None
        estimated_total_tokens = (
            used_tokens + overall_tokens if overall_tokens is not None else None
        )

        current_metrics = (
            recent_valid_metrics(
                (
                    metric
                    for metric in performance.samples
                    if metric.sequence > current.baseline_sequence
                ),
                8,
            )
            if current is not None
            else []
        )
        run_metrics = recent_valid_metrics(
            (
                metric
                for metric in performance.samples
                if metric.sequence > self._run_baseline_sequence
            ),
            8,
        )
        rate = effective_token_rate(current_metrics) or effective_token_rate(run_metrics)
        sample_count = len(current_metrics)
        if sample_count == 0:
            sample_count = len(run_metrics)
        if self._finishing:
            rate = None
            sample_count = 0

        self._stage_deadline = now + stage_seconds if stage_seconds is not None else None
        self._overall_deadline = now + overall_seconds if overall_seconds is not None else None
        self._cached_rate = rate
        self._cached_sample_count = sample_count
        self._cached_used_tokens = used_tokens
        self._cached_stage_remaining_tokens = stage_tokens
        self._cached_overall_remaining_tokens = overall_tokens
        self._cached_estimated_total_tokens = estimated_total_tokens
