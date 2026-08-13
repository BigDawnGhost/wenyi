"""旧版全书审校的叶块执行器。

本模块只负责把一个已翻译章节切成连续块，并执行旧版 Reviewer、可选 Agent Loop、
结构化输出恢复和有序并发合并。它不读取或修改 ``RunStore``，不处理全书 Review
轮次，也不依赖新 workflow、graph 或 LangGraph；持久化仅通过调用方传入的
``ReviewRunStore`` 调试接口发生。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from typing import Any, Protocol

from ..services.translation_batches import plan_contiguous_batches


class LegacyReviewSegment(Protocol):
    """叶块执行器读取的最小旧版段落视图。"""

    source: str
    target: str | None


class LegacyReviewResult(Protocol):
    """旧 Reviewer 单次调用返回的最小结果视图。"""

    issues: list[dict[str, Any]]
    repaired: bool


class LegacyReviewer(Protocol):
    """叶块执行器需要的旧 Reviewer 接口。"""

    def review_result(
        self,
        sources: list[str],
        targets: list[str],
        glossary_terms: Sequence[object],
        *,
        trace: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> LegacyReviewResult:
        """审校一个连续块，并返回已校验的局部问题。"""
        ...


class LegacyReviewPipelineOptions(Protocol):
    """叶块恢复和并发逻辑读取的最小 pipeline 配置。"""

    review_concurrency: int
    review_output_retries: int
    review_agent_loop: bool


class LegacyReviewSegmentOptions(Protocol):
    """叶块打包读取的最小 segment 配置。"""

    max_chars_per_batch: int


class LegacyReviewConfig(Protocol):
    """隔离具体 Pydantic 配置类的旧版只读配置视图。"""

    pipeline: LegacyReviewPipelineOptions
    segment: LegacyReviewSegmentOptions


class LegacyReviewDebug(Protocol):
    """叶块执行器使用的 ReviewRunStore 风格调试端口。"""

    def write_json(self, relative: str, data: Any) -> str:
        """原子写入一次叶块诊断快照。"""
        ...

    def log_event(self, event: str, **data: Any) -> None:
        """追加一个线程安全的 Review 结构化事件。"""
        ...

    def record_initial_issues(
        self,
        *,
        chapter: int,
        chunk_base: int,
        issues: list[dict[str, Any]],
    ) -> None:
        """汇总成功叶块的初审问题。"""
        ...

    def record_dismissed(
        self,
        *,
        chapter: int,
        chunk_base: int,
        issues: list[dict[str, Any]],
    ) -> None:
        """汇总 Agent Loop 驳回的问题。"""
        ...


class LegacyReviewAgentOutcome(Protocol):
    """旧 Review Agent Loop 的核验结果视图。"""

    issues: list[dict[str, Any]]
    dismissed: list[dict[str, Any]]
    fallback_reason: str


class LegacyReviewAgent(Protocol):
    """旧 Review Agent Loop 实例的最小调用接口。"""

    def review_chunk(self, **kwargs: Any) -> LegacyReviewAgentOutcome:
        """核验一个 Reviewer 叶块并返回最终问题。"""
        ...


class LegacyReviewAgentFactory(Protocol):
    """由旧 Orchestrator 动态注入的 Agent Loop 构造器。"""

    def __call__(
        self,
        client: object,
        config: LegacyReviewConfig,
        evidence: object,
        debug: LegacyReviewDebug,
    ) -> LegacyReviewAgent:
        """绑定当前旧运行时依赖并创建无共享状态的叶块 Agent。"""
        ...


ChunkFinishedFn = Callable[[int], None]
PackContiguousFn = Callable[
    [Sequence[LegacyReviewSegment], int],
    list[list[LegacyReviewSegment]],
]


def review_legacy_chapter(
    text_segments: Sequence[LegacyReviewSegment],
    terms: Sequence[object],
    *,
    config: LegacyReviewConfig,
    client: object,
    reviewer: LegacyReviewer,
    recoverable_error: type[Exception],
    agent_loop_factory: LegacyReviewAgentFactory | None = None,
    pack_contiguous: PackContiguousFn | None = None,
    chapter_index: int | None = None,
    evidence: object | None = None,
    debug: LegacyReviewDebug | None = None,
    target_overrides: Mapping[tuple[int, int], str] | None = None,
    review_round: int | None = None,
    on_chunk_finished: ChunkFinishedFn | None = None,
) -> list[dict[str, Any]]:
    """并行审校一个旧版章节，并按章节内原始顺序返回问题。

    只有 ``ReviewOutputError`` 会触发递归拆半和单段有限重试；网络、服务或其他
    编程错误会原样上抛。worker 只暂存恢复事件，主线程在全部 worker 退出后按
    稳定顺序写入调试日志，避免线程调度改变审计结果。
    """
    budget = config.segment.max_chars_per_batch * 3
    pack = pack_contiguous or pack_legacy_review_chunks
    chunks = pack(text_segments, budget)
    if not chunks:
        return []

    # 顶层 job 的基准下标只由确定性切块结果计算，后续递归拆分沿用同一坐标系。
    jobs: list[tuple[int, list[LegacyReviewSegment]]] = []
    base = 0
    for chunk in chunks:
        jobs.append((base, chunk))
        base += len(chunk)

    # 并发 worker 不直接追加事件文件；锁只保护内存列表，落盘留给 finally 主路径。
    recovery_events: list[dict[str, Any]] = []
    recovery_lock = Lock()

    def record_recovery(event: str, **data: Any) -> None:
        """线程安全地暂存可恢复协议错误的诊断事件。"""
        with recovery_lock:
            recovery_events.append({"event": event, **data})

    def review_once(
        chunk_base: int,
        chunk: list[LegacyReviewSegment],
        *,
        attempt: int = 1,
    ) -> list[dict[str, Any]]:
        """调用一次 Reviewer，并把合法块内索引映射回章节索引。"""
        sources = [segment.source for segment in chunk]
        overrides = target_overrides or {}

        def target_for(local_index: int, segment: LegacyReviewSegment) -> str:
            """优先读取当前盲审轮的影子译文，无章节坐标时回退正式译文。"""
            if chapter_index is None:
                return segment.target or ""
            return overrides.get(
                (chapter_index, chunk_base + local_index),
                segment.target or "",
            )

        targets = [target_for(local_index, segment) for local_index, segment in enumerate(chunk)]
        local_issues: list[dict[str, Any]] = []
        initial_trace: dict[str, Any] | None = None
        initial_path = ""

        # 每次真实模型调用都有独立 trace；递归拆分和重试不会覆盖先前失败记录。
        if debug is not None:
            round_prefix = f"r{review_round}-" if review_round is not None else ""
            initial_id = (
                f"initial-{round_prefix}ch{chapter_index}-base{chunk_base}"
                f"-n{len(chunk)}-attempt{attempt}"
            )
            initial_path = f"initial/{initial_id}.json"
            initial_trace = {
                "agent_id": initial_id,
                "chapter": chapter_index,
                "chunk_base": chunk_base,
                "segment_count": len(chunk),
                "attempt": attempt,
                "status": "running",
            }
            debug.write_json(initial_path, initial_trace)

        def trace(event: str, data: dict[str, Any]) -> None:
            """逐步保存 Reviewer 的请求、响应、解析结果或服务错误。"""
            if debug is None or initial_trace is None:
                return
            initial_trace[event] = data
            debug.write_json(initial_path, initial_trace)

        try:
            review_result = reviewer.review_result(
                sources,
                targets,
                terms,
                trace=trace if debug is not None else None,
            )
        except Exception as error:
            # 调用失败先封存 trace，再让自适应层决定是否仅恢复协议错误。
            if debug is not None and initial_trace is not None:
                initial_trace["status"] = "failed"
                initial_trace["error"] = {
                    "type": type(error).__name__,
                    "message": str(error),
                }
                debug.write_json(initial_path, initial_trace)
            raise

        if review_result.repaired:
            record_recovery(
                "review_json_repaired",
                start_index=chunk_base,
                count=len(chunk),
            )
        for issue in review_result.issues:
            item = dict(issue)
            index = item.get("index")
            if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(chunk):
                item["index"] = index
                local_issues.append(item)
            else:
                raise recoverable_error("invalid_issue_index")

        if debug is not None and initial_trace is not None:
            initial_trace["status"] = "finished"
            initial_trace["json_repaired"] = review_result.repaired
            initial_trace["issues"] = local_issues
            debug.write_json(initial_path, initial_trace)
            if chapter_index is not None:
                debug.record_initial_issues(
                    chapter=chapter_index,
                    chunk_base=chunk_base,
                    issues=local_issues,
                )

        # Agent Loop 只核验已有叶块；失败时由其既有协议返回带 fallback 标记的问题。
        dismissed: list[dict[str, Any]] = []
        fallback_reason = ""
        if (
            local_issues
            and evidence is not None
            and debug is not None
            and config.pipeline.review_agent_loop
            and chapter_index is not None
        ):
            if agent_loop_factory is None:
                raise RuntimeError("旧 Review Agent Loop 已启用，但未提供构造器")
            outcome = agent_loop_factory(
                client,
                config,
                evidence,
                debug,
            ).review_chunk(
                chapter=chapter_index,
                chunk_base=chunk_base,
                sources=sources,
                targets=targets,
                initial_issues=local_issues,
                review_round=review_round,
            )
            local_issues = outcome.issues
            dismissed = outcome.dismissed
            fallback_reason = outcome.fallback_reason
            debug.record_dismissed(
                chapter=chapter_index,
                chunk_base=chunk_base,
                issues=dismissed,
            )

        # 最终映射只接受当前块内索引，确保 Agent Loop 也不能把问题写到别的段落。
        round_prefix = f"r{review_round}-" if review_round is not None else ""
        chunk_id = f"{round_prefix}ch{chapter_index}-base{chunk_base}-n{len(chunk)}"
        mapped: list[dict[str, Any]] = []
        for issue in local_issues:
            local_index = issue.get("index")
            if (
                isinstance(local_index, int)
                and not isinstance(local_index, bool)
                and 0 <= local_index < len(chunk)
            ):
                item = dict(issue)
                item["index"] = chunk_base + local_index
                item["_chunk_id"] = chunk_id
                if fallback_reason:
                    item["fallback_reason"] = fallback_reason
                mapped.append(item)
        if debug is not None:
            debug.log_event(
                "review_leaf_finished",
                chapter=chapter_index,
                chunk_base=chunk_base,
                segment_count=len(chunk),
                initial_issue_count=len(review_result.issues),
                final_issue_count=len(mapped),
                dismissed_count=len(dismissed),
                fallback=bool(fallback_reason),
            )
        return mapped

    def review_adaptive(
        chunk_base: int,
        chunk: list[LegacyReviewSegment],
    ) -> list[dict[str, Any]]:
        """畸形输出时递归缩小请求，单段仅执行配置允许的有限重试。"""
        try:
            return review_once(chunk_base, chunk)
        except recoverable_error as error:
            reason = _recoverable_reason(error)
            if len(chunk) > 1:
                middle = len(chunk) // 2
                record_recovery(
                    "review_chunk_split",
                    start_index=chunk_base,
                    count=len(chunk),
                    left_count=middle,
                    right_count=len(chunk) - middle,
                    reason=reason,
                )
                return review_adaptive(chunk_base, chunk[:middle]) + review_adaptive(
                    chunk_base + middle,
                    chunk[middle:],
                )

            last_error = error
            retries = config.pipeline.review_output_retries
            for attempt in range(1, retries + 1):
                record_recovery(
                    "review_singleton_retry",
                    start_index=chunk_base,
                    count=1,
                    attempt=attempt,
                    max_retries=retries,
                    reason=_recoverable_reason(last_error),
                )
                try:
                    result = review_once(chunk_base, chunk, attempt=attempt + 1)
                except recoverable_error as retry_error:
                    last_error = retry_error
                    continue
                record_recovery(
                    "review_singleton_recovered",
                    start_index=chunk_base,
                    count=1,
                    attempt=attempt,
                )
                return result
            record_recovery(
                "review_singleton_failed",
                start_index=chunk_base,
                count=1,
                attempts=retries + 1,
                reason=_recoverable_reason(last_error),
            )
            raise last_error

    def review_one(job: tuple[int, list[LegacyReviewSegment]]) -> list[dict[str, Any]]:
        """执行一个初始连续块及其必要的局部恢复。"""
        chunk_base, chunk = job
        return review_adaptive(chunk_base, chunk)

    # worker 数受 job 数限制；无论完成顺序如何，结果始终写回原 job 位置。
    workers = min(
        max(1, config.pipeline.review_concurrency),
        len(jobs),
    )
    try:
        if workers == 1:
            results: list[list[dict[str, Any]]] = []
            for job in jobs:
                results.append(review_one(job))
                if on_chunk_finished:
                    on_chunk_finished(len(job[1]))
        else:
            ordered_results: list[list[dict[str, Any]] | None] = [None] * len(jobs)
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(review_one, job): (position, len(job[1]))
                    for position, job in enumerate(jobs)
                }
                for future in as_completed(futures):
                    position, segment_count = futures[future]
                    ordered_results[position] = future.result()
                    if on_chunk_finished:
                        on_chunk_finished(segment_count)
            results = [result for result in ordered_results if result is not None]
    finally:
        # 失败路径也必须冲刷已发生的恢复事件；排序隔离线程调度造成的非确定性。
        if debug is not None:
            with recovery_lock:
                event_order = {
                    "review_json_repaired": 0,
                    "review_chunk_split": 0,
                    "review_singleton_retry": 1,
                    "review_singleton_recovered": 2,
                    "review_singleton_failed": 2,
                }
                pending_events = sorted(
                    recovery_events,
                    key=lambda row: (
                        row.get("start_index", -1),
                        -row.get("count", 0),
                        event_order.get(row.get("event", ""), 99),
                        row.get("attempt", 0),
                    ),
                )
            for row in pending_events:
                event = row["event"]
                payload = {
                    "chapter": chapter_index,
                    **{key: value for key, value in row.items() if key != "event"},
                }
                debug.log_event(event, **payload)
    return [issue for chunk_issues in results for issue in chunk_issues]


def pack_legacy_review_chunks(
    segments: Sequence[LegacyReviewSegment],
    budget: int,
) -> list[list[LegacyReviewSegment]]:
    """复用公共 O(n) 计划器，并把计划映射回原旧版段落实例。"""
    return [
        list(segments[plan.start_index : plan.stop_index])
        for plan in plan_contiguous_batches(segments, budget)
    ]


def _recoverable_reason(error: Exception) -> str:
    """读取旧 ReviewOutputError 的稳定 reason，不把通用异常纳入恢复路径。"""
    reason = getattr(error, "reason", None)
    return reason if isinstance(reason, str) and reason else str(error)


__all__ = [
    "LegacyReviewAgentFactory",
    "LegacyReviewConfig",
    "LegacyReviewDebug",
    "LegacyReviewer",
    "LegacyReviewSegment",
    "PackContiguousFn",
    "pack_legacy_review_chunks",
    "review_legacy_chapter",
]
