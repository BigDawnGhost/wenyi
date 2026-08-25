"""流水线共享运行时：唯一构造并持有 Config、LLMClient 与全部 Agent。

Runtime 为单个 Orchestrator 实例私有，不引入全局单例，也不新增跨书并发复用保证。
职责：
  * 唯一构造并持有共享的 Config、LLMClient 和全部 Agent，避免拆分后重复创建
    client、重复计算 usage 或丢失语言状态。
  * 统一处理 manifest 语言恢复，并同步更新 config 与所有 Agent 的 src/tgt。
  * 承担 LLM event sink、usage checkpoint/flush、运行 metrics session、阶段计时、
    状态快照及源文件哈希验证。
  * metrics、usage 或事件记录的故障仍按现有规则降级；metrics 失败只能警告，
    不能改变主流程结果。
"""

from __future__ import annotations

import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from inspect import signature
from typing import Any

from ..agents.analyzer import Analyzer
from ..agents.annotation_aligner import AnnotationAligner
from ..agents.polisher import Polisher
from ..agents.reviewer import Reviewer
from ..agents.synopsis import Synopsizer
from ..agents.translator import Translator
from ..config import Config
from ..glossary.extractor import GlossaryExtractor
from ..llm.base import LLMClient
from ..llm.factory import build_client
from ..llm.usage import merge_usage_summaries, usage_delta
from .language import normalize_lang
from .metrics import RunMetricsRecorder
from .runstore import RunStore, source_sha256

ProgressFn = Callable[[int, int, str], None]

# 单次运行账本（run_metrics/）暂不启用；完善后改为 True 即可放量。
_RUN_METRICS_ENABLED = False


def _record_run_metrics(
    operation: str,
    requested_steps: list[str],
    *,
    invocation_fields: tuple[str, ...] = (),
) -> Callable:
    """为固定入口添加单次运行账本，同时允许入口之间安全嵌套。"""

    def decorator(func: Callable) -> Callable:
        call_signature = signature(func)

        @wraps(func)
        def wrapped(
            self,
            input_path: str,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            bound = call_signature.bind(self, input_path, *args, **kwargs)
            bound.apply_defaults()
            invocation = {name: bound.arguments.get(name) for name in invocation_fields}
            # 装饰器挂在 Orchestrator 方法上，账本由 Runtime 持有，避免反向依赖 façade 私有 API。
            with self._runtime.run_metrics_session(
                input_path,
                operation=operation,
                requested_steps=requested_steps,
                invocation=invocation,
            ):
                return func(self, input_path, *args, **kwargs)

        return wrapped

    return decorator


def _record_pipeline_metrics(func: Callable) -> Callable:
    """为动态步骤集合建立单条顶层流水线账本。"""

    call_signature = signature(func)

    @wraps(func)
    def wrapped(
        self,
        input_path: str,
        steps,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        normalized_steps = set(steps)
        bound = call_signature.bind(
            self,
            input_path,
            normalized_steps,
            *args,
            **kwargs,
        )
        bound.apply_defaults()
        with self._runtime.run_metrics_session(
            input_path,
            operation="pipeline",
            requested_steps=sorted(normalized_steps),
            invocation={
                "out_format": bound.arguments["out_format"],
                "pdf_engine": bound.arguments["pdf_engine"],
            },
        ):
            return func(
                self,
                input_path,
                normalized_steps,
                *args,
                **kwargs,
            )

    return wrapped


class PipelineRuntime:
    """流水线共享运行时：客户端、Agent、用量、指标、语言与源文件身份。"""

    def __init__(self, config: Config, client: LLMClient | None = None):
        """初始化共享 LLM 客户端、用量检查点和各流水线 Agent。"""
        self.config = config
        self.client = client or build_client(config)
        # client 的统计是进程内累计；checkpoint 用于每次落盘时只提取新增部分。
        self._usage_checkpoint = self.client.usage_summary()
        self.analyzer = Analyzer(self.client, config)
        self.synopsizer = Synopsizer(self.client, config)
        self.translator = Translator(self.client, config)
        self.reviewer = Reviewer(self.client, config)
        self.polisher = Polisher(self.client, config)
        self.extractor = GlossaryExtractor(self.client, config)
        self.annotation_aligner = AnnotationAligner(self.client, config)
        self._active_run_metrics: RunMetricsRecorder | None = None
        self._run_metrics_suppressed = False

    # ── 事件与用量 ────────────────────────────────────────────────────────
    def log_event(self, store: RunStore, event: str, **payload: Any) -> None:
        """向当前书籍的追加式事件日志写入一条运行级事件。"""
        store.log_event(event, **payload)

    def bind_llm_events(self, store: RunStore) -> None:
        """把 provider 重试事件实时写入当前书籍的追加式事件日志。"""
        self.client.set_event_sink(store.log_event)

    def punctuation_enabled(self) -> bool:
        """判断当前目标语言是否应启用中文标点规范化。"""
        target = (self.config.target_lang or "").lower().replace("_", "-")
        return self.config.punctuation_normalize and (target == "zh" or target.startswith("zh-"))

    def flush_usage(self, store: RunStore, *, scope: str) -> dict[str, Any]:
        """把当前 client 尚未落盘的用量增量合并到本书 usage.json。"""
        current = self.client.usage_summary()
        increment = usage_delta(current, self._usage_checkpoint)
        self._usage_checkpoint = current
        accumulated = store.load_usage() or {
            "totals": {},
            "by_tier": {},
            "by_stage": {},
        }
        if not increment["totals"]["calls"]:
            return merge_usage_summaries(accumulated, increment)
        cumulative = merge_usage_summaries(accumulated, increment)
        store.save_usage(cumulative)
        store.log_event(
            "usage_summary",
            scope=scope,
            increment=increment,
            cumulative=cumulative,
        )
        return cumulative

    # ── 运行指标 ──────────────────────────────────────────────────────────
    @contextmanager
    def run_metrics_session(
        self,
        input_path: str,
        *,
        operation: str,
        requested_steps: list[str],
        invocation: dict[str, Any] | None = None,
    ) -> Iterator[RunMetricsRecorder | None]:
        """为一次顶层操作建立账本；嵌套入口复用同一记录。"""
        active = self._active_run_metrics
        if active is not None:
            yield active
            return
        if self._run_metrics_suppressed or not _RUN_METRICS_ENABLED:
            yield None
            return

        try:
            recorder = RunMetricsRecorder.start(
                operation=operation,
                requested_steps=requested_steps,
                input_path=input_path,
                config=self.config,
                client=self.client,
                invocation=invocation,
            )
        except Exception as metrics_error:
            warnings.warn(
                f"无法启动单次运行指标：{type(metrics_error).__name__}",
                RuntimeWarning,
                stacklevel=2,
            )
            self._run_metrics_suppressed = True
            try:
                yield None
            finally:
                self._run_metrics_suppressed = False
            return

        self._active_run_metrics = recorder
        status = "failed"
        error: BaseException | None = None
        try:
            yield recorder
            status = "completed"
        except BaseException as exc:
            error = exc
            raise
        finally:
            try:
                recorder.finish(self.client, status=status, error=error)
            except Exception as metrics_error:
                warnings.warn(
                    f"无法保存单次运行指标：{type(metrics_error).__name__}",
                    RuntimeWarning,
                    stacklevel=2,
                )
            self._active_run_metrics = None

    @contextmanager
    def metric_stage(self, name: str) -> Iterator[None]:
        """在已有运行账本中统计阶段耗时；无账本时保持原行为。"""
        if self._active_run_metrics is None:
            yield
            return
        with self._active_run_metrics.stage(name):
            yield

    def measure_stage_call(
        self,
        name: str,
        func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """统计一次函数调用所属阶段，并原样返回结果或抛出异常。"""
        with self.metric_stage(name):
            return func(*args, **kwargs)

    def attach_metrics_store(self, store: RunStore) -> None:
        """让顶层运行账本随当前书籍状态一起落盘。"""
        if self._active_run_metrics is not None:
            try:
                self._active_run_metrics.attach_store(store)
            except Exception as metrics_error:
                warnings.warn(
                    f"无法绑定单次运行指标：{type(metrics_error).__name__}",
                    RuntimeWarning,
                    stacklevel=2,
                )

    def capture_metrics_state(self, store: RunStore) -> None:
        """从实时状态或导出快照冻结结束状态，防止后续进度污染账本。"""
        if self._active_run_metrics is None:
            return
        try:
            self._active_run_metrics.capture_state(store)
        except Exception as metrics_error:
            warnings.warn(
                f"无法捕获单次运行结束状态：{type(metrics_error).__name__}",
                RuntimeWarning,
                stacklevel=2,
            )

    # ── 源文件身份 ────────────────────────────────────────────────────────
    def source_sha256(self, input_path: str) -> str:
        """在状态消费边界重新计算源文件哈希，不能信任锁外指标快照。"""
        if self._active_run_metrics is not None:
            verified = self._active_run_metrics.verify_input_sha256(input_path)
            if verified is not None:
                return verified
        digest = source_sha256(input_path)
        if self._active_run_metrics is not None:
            self._active_run_metrics.input["sha256"] = digest
        return digest

    def initial_source_sha256(self, input_path: str) -> str:
        """取得解析前内容快照；有指标时复用其启动快照以少读一次文件。"""
        if self._active_run_metrics is not None:
            initial = self._active_run_metrics.input.get("sha256")
            if isinstance(initial, str):
                return initial
        return source_sha256(input_path)

    def ensure_store_source(self, store: RunStore, input_path: str) -> str:
        """校验候选状态确实属于当前输入文件。"""
        return store.ensure_source_identity(
            input_path,
            actual_sha256=self.source_sha256(input_path),
        )

    # ── 语言解析 ──────────────────────────────────────────────────────────
    def apply_language(self, lang: str) -> None:
        """把解析出的源语言应用到 config 与各 agent（auto 检测后调用）。"""
        resolved = lang or self.config.source_lang
        source = normalize_lang(resolved)
        target = normalize_lang(self.config.target_lang)
        if source and target and source == target:
            raise ValueError(
                f"源语言与目标语言相同（{source}），无需翻译；"
                "请修改 config.yaml 中的 language.source 或 language.target。"
            )
        self.config.source_lang = resolved
        for ag in (
            self.analyzer,
            self.synopsizer,
            self.translator,
            self.reviewer,
            self.polisher,
            self.extractor,
            self.annotation_aligner,
        ):
            ag.src = resolved
            ag.tgt = self.config.target_lang

    def apply_manifest_languages(self, manifest: dict[str, Any]) -> None:
        """从既有状态恢复源语言和目标语言，再同步给全部 agent。"""
        target = manifest.get("target_lang")
        if isinstance(target, str) and target:
            self.config.target_lang = target
        source = manifest.get("source_lang")
        self.apply_language(
            source if isinstance(source, str) and source else self.config.source_lang
        )
