"""编排器：驱动全流程，章级状态机 + 断点续跑。

单章翻译流水线（章内批次**串行**，逐批刷新滚动上下文与术语快照；跨章亦串行传递梗概）：
  每批：渲染上下文（含前一批刚译出的译文）→ 翻译（对齐保证）→ 润色（可选）→
        含注释逻辑段定稿并串行定位链接 → 术语/称呼/固定表达实时抽取入库 →
        立即供下一批参照。
  章末：其余段落标点规范化 → 全章术语兜底抽取 → 回译抽检 → 落盘标记 done。
翻译前先预扫源文建立全书理解（逐章梗概+全书概览，fast 档并行），作恒定前缀注入每章翻译。

全书翻译完成后，独立 Review 阶段使用最终术语库按章并行审校；候选问题进入
有界 Agent Loop 按需检索全书证据，跨块矛盾建议再统一仲裁。结果写入独立的
正式 Review 目录，不改正文；run_all 随后仍以正式章节生成报告和导出。
进度回调 progress(done_segments, total_segments, label) 与 UI 无关，每批完成即触发。
"""

from __future__ import annotations

import random
import warnings
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from functools import wraps
from inspect import signature
from typing import Any, Iterator

from ..agents.analyzer import Analyzer
from ..agents.annotation_aligner import AnnotationAligner
from ..agents.polisher import Polisher
from ..agents.review_fixer import (
    ProvisionalPatch,
    ReviewFixer,
    ReviewFixerProtocolError,
)
from ..agents.review_loop import (
    ReviewAgentLoop,
    ReviewConflictArbiter,
    apply_review_arbitrations,
    build_conflict_groups,
    normalize_review_issues,
)
from ..agents.reviewer import BackTranslator, Reviewer, ReviewOutputError
from ..agents.synopsis import Synopsizer
from ..agents.translator import Translator
from ..application.publishing import PublishingOptions, assemble_outputs
from ..application.review.models import (
    _review_conflict_records,
    _review_content_digest,
    _review_net_changes,
    _review_overlay_digest,
    _review_public_issues,
    _review_unresolved_conflict_records,
    _review_unresolved_fallback_count,
    _ReviewRoundResult,
)
from ..config import Config
from ..glossary.extractor import GlossaryExtractor, TranslatedSegmentEvidence
from ..glossary.store import GlossaryStore, GlossaryTerm
from ..ingest.models import Chapter, Segment
from ..ingest.segmenter import load_document
from ..llm.base import LLMClient
from ..llm.factory import build_client
from ..llm.usage import merge_usage_summaries, usage_delta
from ..services.document_sampling import sample_document_text
from ..services.source_language import (
    LanguageDetectionError,
    ModelSourceLanguageDetector,
    normalize_language_candidate,
)
from .annotations import (
    align_legacy_annotations_after_batch,
    align_legacy_segment_annotation,
    annotation_contexts_for_segments,
    completed_logical_starts_in_range,
    sync_legacy_context_chapter_prefix,
)
from .chapter_translation import (
    BatchResult,
    TranslationPolicy,
    extract_legacy_batch_glossary,
    legacy_chapter_progress_label,
    legacy_chapter_term_snapshot,
    legacy_translation_progress_counts,
    process_legacy_batch,
    report_translation_progress,
    resume_legacy_batches,
    translate_legacy_chapter,
    update_legacy_translation_history,
)
from .context import RollingContext
from .metrics import RunMetricsRecorder
from .preparation import build_legacy_preparation
from .review_chapter import pack_legacy_review_chunks, review_legacy_chapter
from .review_evidence import BookEvidenceIndex
from .review_run import ReviewOutcome, ReviewRunStore
from .runstore import STATUS_DONE, RunStore, source_sha256
from .runtime import SourceIdentityRuntime
from .title_translation import translate_legacy_titles
from .understanding import build_legacy_understanding

ProgressFn = Callable[[int, int, str], None]


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
            self: Orchestrator,
            input_path: str,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            bound = call_signature.bind(self, input_path, *args, **kwargs)
            bound.apply_defaults()
            invocation = {name: bound.arguments.get(name) for name in invocation_fields}
            with self._run_metrics_session(
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
        self: Orchestrator,
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
        with self._run_metrics_session(
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


def _report_translation_progress(
    progress: ProgressFn | None,
    *,
    chapter_done: int,
    chapter_total: int,
    overall_done: int,
    overall_total: int,
    label: str,
) -> None:
    """兼容旧模块级入口；实际双层进度逻辑位于旧正文翻译模块。"""
    report_translation_progress(
        progress,
        chapter_done=chapter_done,
        chapter_total=chapter_total,
        overall_done=overall_done,
        overall_total=overall_total,
        label=label,
    )


def _normalize_lang(code: str) -> str:
    """把模型返回的语言名或别名规整为 ISO 639-1 两字母代码。"""
    return normalize_language_candidate(code)


def _resume_batches(segments, max_chars: int) -> list[list[Segment]]:
    """按字符预算分批后，再沿“已完成/待翻译”边界切开。

    用户调整批次预算时，新的批次可能同时包含已有译文和空译文。若直接重跑
    该混合批次会覆盖已确认内容；按完成状态分组可只补译缺失段。
    """
    return resume_legacy_batches(segments, max_chars)


# 保留历史导入名：外部测试与集成仍可从 orchestrator 导入批次结果类型。
_BatchResult = BatchResult


class Orchestrator:
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
        self.backtrans = BackTranslator(self.client, config)
        self.polisher = Polisher(self.client, config)
        self.extractor = GlossaryExtractor(self.client, config)
        self.annotation_aligner = AnnotationAligner(self.client, config)
        self._active_run_metrics: RunMetricsRecorder | None = None
        self._run_metrics_suppressed = False
        # Lambda intentionally resolves the compatibility-level module symbol at call
        # time, preserving existing tests and integrations that patch this hasher.
        self._source_identity = SourceIdentityRuntime(lambda path: source_sha256(path))

    def _bind_llm_events(
        self,
        store: RunStore,
        progress: ProgressFn | None = None,
    ) -> None:
        """绑定正式重试日志，并把瞬时模型活动交给可选 UI 桥。"""
        self.client.set_event_sink(store.log_event)
        activity_sink = getattr(progress, "on_llm_activity", None)
        set_activity_sink = getattr(self.client, "set_activity_sink", None)
        if callable(set_activity_sink):
            set_activity_sink(activity_sink if callable(activity_sink) else None)

    def _punctuation_enabled(self) -> bool:
        """判断当前目标语言是否应启用中文标点规范化。"""
        target = (self.config.target_lang or "").lower().replace("_", "-")
        return self.config.punctuation_normalize and (target == "zh" or target.startswith("zh-"))

    def _flush_usage(self, store: RunStore, *, scope: str) -> dict[str, Any]:
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

    @contextmanager
    def _run_metrics_session(
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
        if self._run_metrics_suppressed:
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
    def _metric_stage(self, name: str) -> Iterator[None]:
        """在已有运行账本中统计阶段耗时；无账本时保持原行为。"""
        if self._active_run_metrics is None:
            yield
            return
        with self._active_run_metrics.stage(name):
            yield

    def _measure_stage_call(
        self,
        name: str,
        func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """统计一次函数调用所属阶段，并原样返回结果或抛出异常。"""
        with self._metric_stage(name):
            return func(*args, **kwargs)

    def _attach_metrics_store(self, store: RunStore) -> None:
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

    def _capture_metrics_state(self, store: RunStore) -> None:
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

    def _source_sha256(self, input_path: str) -> str:
        """在状态消费边界重新计算源文件哈希，不能信任锁外指标快照。"""
        return self._source_identity.verified_sha256(
            input_path,
            recorder=self._active_run_metrics,
        )

    def _initial_source_sha256(self, input_path: str) -> str:
        """取得解析前内容快照；有指标时复用其启动快照以少读一次文件。"""
        return self._source_identity.initial_sha256(
            input_path,
            recorder=self._active_run_metrics,
        )

    def _ensure_store_source(self, store: RunStore, input_path: str) -> str:
        """校验候选状态确实属于当前输入文件。"""
        # Preserve the compatibility seam: subclasses and integrations may
        # override ``_source_sha256`` to supply the invocation's verified digest.
        return store.ensure_source_identity(
            input_path,
            actual_sha256=self._source_sha256(input_path),
        )

    # ── 语言解析 ────────────────────────────────────────────────────────────
    def _apply_language(self, lang: str) -> None:
        """把解析出的源语言应用到 config 与各 agent（auto 检测后调用）。"""
        resolved = lang or self.config.source_lang
        source = _normalize_lang(resolved)
        target = _normalize_lang(self.config.target_lang)
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
            self.backtrans,
            self.polisher,
            self.extractor,
            self.annotation_aligner,
        ):
            ag.src = resolved
            ag.tgt = self.config.target_lang

    def _apply_manifest_languages(self, manifest: dict[str, Any]) -> None:
        """从既有状态恢复源语言和目标语言，再同步给全部 agent。"""
        target = manifest.get("target_lang")
        if isinstance(target, str) and target:
            self.config.target_lang = target
        source = manifest.get("source_lang")
        self._apply_language(
            source if isinstance(source, str) and source else self.config.source_lang
        )

    # ── 准备 / 续跑入口 ──────────────────────────────────────────────────
    def _locate_existing_store(
        self,
        input_path: str,
        *,
        progress: ProgressFn | None = None,
    ) -> RunStore:
        """定位输入文件对应的既有状态，不创建或初始化新的翻译任务。

        PDF 的状态目录直接取自文件名，因此可在调用 MinerU 前完成检查；其它
        格式仍需本地解析书名来得到与 ``prepare`` 相同的状态目录。
        """
        return build_legacy_preparation(self, loader=load_document).locate_existing(
            input_path,
            progress=progress,
        )

    def prepare(self, input_path: str, *, progress: ProgressFn | None = None) -> RunStore:
        """解析输入并定位状态目录；首次运行时在书级锁内完成初始化。

        PDF 的状态目录可直接由文件名确定，因此续跑时先检查 manifest，
        避免重新调用外部转换服务；首次转换产生的 HTML 缓存在该状态目录中。
        """
        return build_legacy_preparation(self, loader=load_document).prepare(
            input_path,
            progress=progress,
        )

    def _prepare_locked(
        self,
        doc,
        store: RunStore,
        input_path: str,
        progress: ProgressFn | None,
        *,
        source_hash: str | None = None,
    ) -> RunStore:
        """恢复已有状态；新运行分阶段写入，并以 manifest 原子提交完成标志。"""
        return build_legacy_preparation(self, loader=load_document).initialize_locked(
            doc,
            store,
            input_path,
            progress,
            source_hash=source_hash,
        )

    def _detect_language_ai(self, doc) -> str:
        """用模型检测正文主要语言，返回 ISO 代码（如 ja/en/ru）。失败返回空串。"""
        # labeled=False：纯源文样本，防多点采样的中文标签污染语言检测
        sample = self._sample_text(doc, labeled=False)[:1500]
        try:
            candidate = ModelSourceLanguageDetector(self.client).detect(sample)
            return normalize_language_candidate(candidate)
        except LanguageDetectionError:
            return ""

    @staticmethod
    def _sample_text(doc, *, labeled: bool = True) -> str:
        """取风格分析样章。labeled=True 时多点采样（开头/中部/结尾各一段，带中文标注），
        让分析覆盖全书风格全貌；labeled=False 返回单段纯源文（语言检测用，不能混入中文标签）。"""
        return sample_document_text(doc, labeled=labeled)

    @_record_run_metrics(
        "translate",
        ["translate"],
        invocation_fields=("only_chapter",),
    )
    def run(
        self,
        input_path: str,
        *,
        only_chapter: int | None = None,
        progress: ProgressFn | None = None,
    ) -> RunStore:
        """准备运行状态并在书级锁内翻译待处理章节。"""
        store = self._measure_stage_call(
            "prepare",
            self.prepare,
            input_path,
            progress=progress,
        )
        with store.lock():
            result = self._run_locked(
                store,
                only_chapter=only_chapter,
                progress=progress,
            )
            self._capture_metrics_state(store)
            return result

    @_record_run_metrics("prepare", ["prepare", "understanding"])
    def prepare_for_translation(
        self,
        input_path: str,
        *,
        progress: ProgressFn | None = None,
    ) -> RunStore:
        """完成全部译前准备并停止，不翻译正文。

        包括文档解析、语言识别、风格/初始术语分析，以及配置开启时的
        逐章预扫和全书概览。所有阶段均可续跑，再次调用会复用已落盘结果。
        """
        store = self._measure_stage_call(
            "prepare",
            self.prepare,
            input_path,
            progress=progress,
        )
        with store.lock():
            manifest = store.load_manifest()
            self._apply_manifest_languages(manifest)
            try:
                self._measure_stage_call(
                    "understanding",
                    self._build_understanding,
                    store,
                    progress=progress,
                )
                store.log_event(
                    "translation_prepared",
                    input_path=input_path,
                    book_understanding=self.config.pipeline.book_understanding,
                )
            finally:
                self._flush_usage(store, scope="prepare")
            self._capture_metrics_state(store)
        return store

    def _run_locked(
        self,
        store: RunStore,
        *,
        only_chapter: int | None,
        progress: ProgressFn | None,
    ) -> RunStore:
        """恢复语言和上下文，依次翻译章节并持续保存用量与进度。"""
        manifest = store.load_manifest()
        self._apply_manifest_languages(manifest)
        chapter_indices = {chapter.get("index") for chapter in manifest.get("chapters", [])}
        if only_chapter is not None and only_chapter not in chapter_indices:
            available = sorted(index for index in chapter_indices if isinstance(index, int))
            valid_range = f"0–{available[-1]}" if available else "无可翻译章节"
            raise ValueError(f"章节编号 {only_chapter} 不存在；可用范围：{valid_range}")
        glossary = GlossaryStore(store.glossary_path)
        context = RollingContext.from_dict(
            store.load_context() or {},
            min_recent_keep=max(40, self.config.pipeline.rolling_context_segments),
        )
        style = self.analyzer.style_brief(store.load_analysis() or {})
        # 翻译前预扫源文，建立全书理解（幂等、可续跑）；全书概览注入每章翻译
        with self._metric_stage("understanding"):
            book_synopsis = self._build_understanding(store, progress=progress)

        if only_chapter is not None:
            targets = [only_chapter]
            progress_chapters = targets
        else:
            targets = store.pending_chapters()
            progress_chapters = [chapter["index"] for chapter in manifest.get("chapters", [])]

        total, done = self._progress_counts(store, progress_chapters)
        translation_history, source_corpus = self._load_translation_inputs(store)
        annotation_context_registry = store.load_annotation_contexts()
        store.log_event(
            "translate_run_started",
            only_chapter=only_chapter,
            chapters=targets,
            total_segments=total,
        )
        try:
            with self._metric_stage("translate"):
                for ci in targets:
                    done = self._translate_chapter(
                        ci,
                        store,
                        glossary,
                        context,
                        style,
                        book_synopsis,
                        translation_history=translation_history,
                        source_corpus=source_corpus,
                        annotation_context_registry=annotation_context_registry,
                        progress=progress,
                        done=done,
                        total=total,
                    )
                    store.save_context(context.to_dict())
                    self._flush_usage(store, scope="chapter")
                # 全书译完后翻译各章标题和目录项（书名保持原文，借术语表保持专名一致）
                if not store.pending_chapters():
                    self._translate_titles(store, glossary, progress=progress)
        finally:
            glossary.close()
            self._flush_usage(store, scope="translate")
        if progress and total:
            progress(total, total, "翻译完成")
        store.log_event("translate_run_finished", total_segments=total)
        return store

    @staticmethod
    def _load_translation_inputs(
        store: RunStore,
    ) -> tuple[dict[tuple[int, int], TranslatedSegmentEvidence], str]:
        """一次读取章节，重建历史译文索引并拼接完整源文。"""
        history: dict[tuple[int, int], TranslatedSegmentEvidence] = {}
        source_parts: list[str] = []
        manifest = store.load_manifest()
        chapter_indices = sorted(
            chapter["index"]
            for chapter in manifest.get("chapters", [])
            if isinstance(chapter.get("index"), int)
        )
        for chapter_index in chapter_indices:
            chapter = store.load_chapter(chapter_index)
            for segment_index, segment in enumerate(chapter.text_segments):
                source_parts.append(segment.source)
                target = (segment.target or "").strip()
                if not target:
                    continue
                history[(chapter_index, segment_index)] = TranslatedSegmentEvidence(
                    chapter=chapter_index,
                    segment=segment_index,
                    source=segment.source,
                    target=target,
                )
        return history, "\n".join(source_parts)

    @staticmethod
    def _update_translation_history(
        history: dict[tuple[int, int], TranslatedSegmentEvidence],
        chapter: int,
        start_index: int,
        segments,
    ) -> None:
        """兼容旧私有入口；更新正文翻译证据的内存位置索引。"""
        update_legacy_translation_history(history, chapter, start_index, segments)

    def _progress_counts(self, store: RunStore, chapter_indices: list[int]) -> tuple[int, int]:
        """按全书批次检查点计算进度，续跑从已有译文数量开始显示。

        只有整批译文齐全时才计入 done；不完整批次会整体重跑，提前计入其中
        个别已有段会导致完成数重复累加。
        """
        return legacy_translation_progress_counts(
            store,
            chapter_indices,
            max_chars_per_batch=self.config.segment.max_chars_per_batch,
            plan_batches=_resume_batches,
        )

    # ── 全书理解预扫（源文逐章梗概 + 全书概览）────────────────────────────────
    def _build_understanding(self, store: RunStore, progress: ProgressFn | None = None) -> str:
        """翻译前预扫源文：逐章梗概存入 chapter.meta，归并出全书概览存入 analysis。

        幂等、可续跑：已有梗概/概览则跳过。返回全书概览（注入各章翻译 prompt）。
        关闭 book_understanding 时直接返回空串。
        """
        return build_legacy_understanding(
            store,
            enabled=self.config.pipeline.book_understanding,
            concurrency=self.config.pipeline.prescan_concurrency,
            digest_chapter=self.synopsizer.digest_chapter,
            summarize_book=self.synopsizer.book_synopsis,
            style_brief=self.analyzer.style_brief,
            progress=progress,
        )

    # ── 章节标题 / 目录项翻译（书名保持原文）──────────────────────────────
    def _translate_titles(
        self,
        store: RunStore,
        glossary: GlossaryStore,
        progress: ProgressFn | None = None,
    ) -> None:
        """保留旧私有入口，并把依赖收窄后委托给旧版专属协调模块。"""
        translate_legacy_titles(
            store,
            glossary,
            complete_json=self.client.complete_json,
            source_lang=self.config.source_lang,
            target_lang=self.config.target_lang,
            progress=progress,
        )

    # ── 单章 ──────────────────────────────────────────────────────────────
    @staticmethod
    def _sync_context_chapter_prefix(
        context: RollingContext,
        segments: list[Segment],
        end: int,
    ) -> None:
        """兼容旧私有入口；实际上下文尾部同步由 annotation adapter 执行。"""
        sync_legacy_context_chapter_prefix(context, segments, end)

    @staticmethod
    def _completed_logical_starts_in_range(
        segments: list[Segment],
        start: int,
        count: int,
    ) -> list[int]:
        """兼容旧私有入口；返回当前半开批次内完成的逻辑段起点。"""
        return completed_logical_starts_in_range(segments, start, count)

    def _align_segment_annotation(
        self,
        ci: int,
        chapter: Chapter,
        start_position: int,
        store: RunStore,
    ) -> None:
        """兼容旧私有入口；适配配置、aligner、事件与章节持久化。"""
        align_legacy_segment_annotation(
            ci,
            chapter,
            start_position,
            store,
            punctuation_enabled=self._punctuation_enabled,
            alignment_enabled=lambda: self.config.pipeline.annotation_alignment,
            align_unit=lambda unit: self.annotation_aligner.align_unit(unit),
        )

    def _align_annotations_after_batch(
        self,
        ci: int,
        chapter: Chapter,
        start: int,
        count: int,
        store: RunStore,
    ) -> None:
        """兼容旧私有入口；保持逐逻辑段串行执行及副作用顺序。"""
        align_legacy_annotations_after_batch(
            ci,
            chapter,
            start,
            count,
            store,
            align_segment=self._align_segment_annotation,
            completed_starts=self._completed_logical_starts_in_range,
        )

    @staticmethod
    def _annotation_contexts_for_segments(
        segments: list[Segment],
        registry: dict[str, Any] | None,
    ) -> list[list[dict[str, str]]]:
        """兼容旧私有入口；按源文偏移生成逐物理切片的注释上下文。"""
        return annotation_contexts_for_segments(segments, registry)

    def _translate_chapter(
        self,
        ci: int,
        store: RunStore,
        glossary: GlossaryStore,
        context: RollingContext,
        style: str,
        book_synopsis: str = "",
        *,
        translation_history: dict[tuple[int, int], TranslatedSegmentEvidence],
        source_corpus: str,
        annotation_context_registry: dict[str, Any] | None,
        progress: ProgressFn | None = None,
        done: int = 0,
        total: int = 0,
    ) -> int:
        """兼容旧私有入口；把旧 seam 注入正文翻译协调器后返回累计进度。"""
        policy = TranslationPolicy(
            max_chars_per_batch=self.config.segment.max_chars_per_batch,
        )
        return translate_legacy_chapter(
            ci,
            store,
            glossary,
            context,
            style,
            book_synopsis,
            policy=policy,
            translation_history=translation_history,
            source_corpus=source_corpus,
            annotation_context_registry=annotation_context_registry,
            # 注入 bound methods，确保 monkeypatch 与子类覆写继续生效。
            process_batch=self._process_batch,
            term_snapshot=self._chapter_term_snapshot,
            extract_batch_glossary=self._extract_batch_glossary,
            align_after_batch=self._align_annotations_after_batch,
            sync_context_chapter_prefix=self._sync_context_chapter_prefix,
            update_translation_history=self._update_translation_history,
            annotation_contexts_for_segments=self._annotation_contexts_for_segments,
            chapter_progress_label=self._chapter_progress_label,
            extract_chapter_glossary=self.extractor.extract_and_store,
            backtranslation_check=self.backtrans.check,
            polish_enabled=lambda: self.config.pipeline.polish,
            punctuation_enabled=self._punctuation_enabled,
            rolling_context_segments=lambda: self.config.pipeline.rolling_context_segments,
            plan_batches=_resume_batches,
            report_progress=_report_translation_progress,
            progress=progress,
            done=done,
            total=total,
        )

    def _chapter_term_snapshot(self, glossary: GlossaryStore, text_segs) -> list:
        """兼容旧私有入口；返回当前章节要注入的最新术语快照。"""
        return legacy_chapter_term_snapshot(
            glossary,
            text_segs,
            glossary_scope=self.config.pipeline.glossary_scope,
        )

    @staticmethod
    def _chapter_progress_label(title: str, index: int) -> str:
        """兼容旧私有入口；优先使用书内标题。"""
        return legacy_chapter_progress_label(title, index)

    def _extract_batch_glossary(
        self,
        glossary: GlossaryStore,
        store: RunStore,
        chapter: int,
        start_index: int,
        batch,
        translation_history: dict[tuple[int, int], TranslatedSegmentEvidence],
        source_corpus: str,
    ) -> dict[str, int]:
        """兼容旧私有入口；提交批次术语后写入恢复检查点事件。"""
        return extract_legacy_batch_glossary(
            glossary,
            store,
            chapter,
            start_index,
            batch,
            translation_history,
            source_corpus,
            extract_and_store=self.extractor.extract_and_store,
        )

    # ── 只读全书 Agent Review ───────────────────────────────────────────────

    def _review_round(
        self,
        loaded,
        all_terms: list[GlossaryTerm],
        evidence: BookEvidenceIndex,
        debug: ReviewRunStore,
        *,
        review_round: int,
        target_overrides: Mapping[tuple[int, int], str],
        progress: ProgressFn | None = None,
    ) -> _ReviewRoundResult:
        """对同一份只读影子译文完成一轮全书审校和冲突仲裁。"""
        total = sum(len(chapter.text_segments) for chapter in loaded)
        done = 0
        review_label = (
            f"全书审校 R{review_round}" if review_round == 1 else f"全书盲审 R{review_round}"
        )
        if progress:
            progress(0, total, review_label)
        raw_issues: list[dict[str, Any]] = []
        for chapter in loaded:
            text_segs = chapter.text_segments
            if self.config.pipeline.glossary_scope == "chapter":
                source_text = "\n".join(segment.source for segment in text_segs)
                term_snapshot = GlossaryStore.terms_in(all_terms, source_text)
            else:
                term_snapshot = all_terms

            def on_chunk_finished(segment_count: int) -> None:
                """在一个顶层审校块完成后推进本轮全书段落进度。"""
                nonlocal done
                done += segment_count
                if progress:
                    progress(done, total, review_label)

            chapter_issues = self._review_chapter(
                text_segs,
                term_snapshot,
                chapter_index=chapter.index,
                evidence=evidence,
                debug=debug,
                target_overrides=target_overrides,
                review_round=review_round,
                on_chunk_finished=on_chunk_finished,
            )
            for issue in chapter_issues:
                issue["chapter"] = chapter.index
                issue["stage"] = "review_agent"
                issue["review_round"] = review_round
            raw_issues.extend(chapter_issues)
            debug.log_event(
                "review_chapter_finished",
                chapter=chapter.index,
                segment_count=len(text_segs),
                issue_count=len(chapter_issues),
            )

        pre_arbitration_issues = normalize_review_issues(raw_issues, evidence)
        for issue in pre_arbitration_issues:
            issue["issue_id"] = f"r{review_round}-{issue['issue_id']}"
        conflict_groups = build_conflict_groups(pre_arbitration_issues)
        arbitrations: list[dict[str, Any]] = []
        if conflict_groups and self.config.pipeline.review_conflict_arbitration:
            arbitration_label = f"冲突仲裁 R{review_round}"
            arbitration_total = len(conflict_groups)
            if progress:
                progress(0, arbitration_total, arbitration_label)
            workers = min(
                max(1, self.config.pipeline.review_concurrency),
                arbitration_total,
            )

            def arbitrate(group: dict[str, Any]) -> dict[str, Any]:
                return ReviewConflictArbiter(
                    self.client,
                    self.config,
                    evidence,
                    debug,
                ).arbitrate(group)

            if workers == 1:
                for done_count, group in enumerate(conflict_groups, start=1):
                    arbitrations.append(arbitrate(group))
                    if progress:
                        progress(done_count, arbitration_total, arbitration_label)
            else:
                ordered_arbitrations: list[dict[str, Any] | None] = [None] * arbitration_total
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    futures = {
                        executor.submit(arbitrate, group): position
                        for position, group in enumerate(conflict_groups)
                    }
                    for done_count, future in enumerate(as_completed(futures), start=1):
                        ordered_arbitrations[futures[future]] = future.result()
                        if progress:
                            progress(done_count, arbitration_total, arbitration_label)
                arbitrations = [
                    arbitration for arbitration in ordered_arbitrations if arbitration is not None
                ]
        elif conflict_groups:
            arbitrations = [
                {
                    "conflict_id": group["conflict_id"],
                    "consistency_key": group["consistency_key"],
                    "issue_ids": [issue["issue_id"] for issue in group["issues"]],
                    "status": "unresolved",
                    "recommended_value": "",
                    "reason": "配置已关闭全书冲突仲裁。",
                    "supported_issue_ids": [issue["issue_id"] for issue in group["issues"]],
                    "rejected_issue_ids": [],
                    "evidence_refs": [],
                }
                for group in conflict_groups
            ]

        final_issues, arbitration_superseded = apply_review_arbitrations(
            pre_arbitration_issues,
            arbitrations,
        )
        fallback_agent_count = len(
            {
                issue["_chunk_id"]
                for issue in pre_arbitration_issues
                if issue.get("agent_fallback") and isinstance(issue.get("_chunk_id"), str)
            }
        )
        residual_conflicts = build_conflict_groups(final_issues)
        initial_issues, dismissed = debug.result_snapshots(review_round)
        debug.write_json("initial_issues.json", initial_issues)
        debug.write_json("dismissed_issues.json", dismissed)
        debug.write_json("pre_arbitration_issues.json", pre_arbitration_issues)
        debug.write_json("arbitration_superseded_issues.json", arbitration_superseded)
        debug.write_json("final_issues.json", final_issues)
        debug.write_json(
            "residual_conflicts.json",
            [
                {
                    "conflict_id": group["conflict_id"],
                    "consistency_key": group["consistency_key"],
                    "issue_ids": [issue["issue_id"] for issue in group["issues"]],
                }
                for group in residual_conflicts
            ],
        )
        debug.write_json(
            "conflicts.json",
            _review_conflict_records(conflict_groups, arbitrations),
        )
        debug.log_event(
            "review_round_finished",
            issue_count=len(final_issues),
            conflict_count=len(conflict_groups),
            unresolved_conflict_count=len(residual_conflicts),
            fallback_agent_count=fallback_agent_count,
        )
        return _ReviewRoundResult(
            issues=final_issues,
            pre_arbitration_issues=pre_arbitration_issues,
            arbitration_superseded=arbitration_superseded,
            conflict_groups=conflict_groups,
            residual_conflicts=residual_conflicts,
            fallback_agent_count=fallback_agent_count,
        )

    def _propose_review_patches(
        self,
        round_result: _ReviewRoundResult,
        evidence: BookEvidenceIndex,
        all_terms: list[GlossaryTerm],
        analysis: dict[str, Any],
        debug: ReviewRunStore,
        *,
        review_round: int,
        fix_round: int,
        progress: ProgressFn | None = None,
    ) -> tuple[list[ProvisionalPatch], list[dict[str, Any]]]:
        """按段聚合已确认问题，并行生成仅供下一轮验证的完整段落替换。"""
        grouped: dict[tuple[int, int], list[dict[str, Any]]] = {}
        skipped: list[dict[str, Any]] = []
        for issue in round_result.issues:
            chapter = issue.get("chapter")
            index = issue.get("index")
            issue_id = issue.get("issue_id")
            if (
                isinstance(chapter, bool)
                or not isinstance(chapter, int)
                or isinstance(index, bool)
                or not isinstance(index, int)
                or not isinstance(issue_id, str)
            ):
                skipped.append(
                    {
                        "issue_id": issue_id,
                        "status": "skipped",
                        "reason": "invalid_issue_location",
                    }
                )
                continue
            arbitration = issue.get("arbitration")
            if isinstance(arbitration, dict) and arbitration.get("status") == "unresolved":
                skipped.append(
                    {
                        "issue_id": issue_id,
                        "chapter": chapter,
                        "index": index,
                        "status": "skipped",
                        "reason": "unresolved_consistency_conflict",
                    }
                )
                continue
            if self.config.pipeline.review_agent_loop and issue.get("agent_fallback"):
                skipped.append(
                    {
                        "issue_id": issue_id,
                        "chapter": chapter,
                        "index": index,
                        "status": "skipped",
                        "reason": "unverified_agent_fallback",
                    }
                )
                continue
            grouped.setdefault((chapter, index), []).append(issue)

        jobs = sorted(grouped.items())
        if not jobs:
            return [], skipped
        fix_label = f"影子修订 R{fix_round}"
        fix_total = len(jobs)
        if progress:
            progress(0, fix_total, fix_label)
        style = self.analyzer.style_brief(analysis)
        book_synopsis = str(analysis.get("book_synopsis", "") or "")
        fixer = ReviewFixer(self.client, self.config)

        def propose(
            job: tuple[tuple[int, int], list[dict[str, Any]]],
        ) -> tuple[ProvisionalPatch | None, dict[str, Any] | None]:
            (chapter, index), issues = job
            segment = evidence.segment_ref(chapter, index)
            if segment is None:
                return None, {
                    "issue_ids": [issue["issue_id"] for issue in issues],
                    "chapter": chapter,
                    "index": index,
                    "status": "skipped",
                    "reason": "segment_not_found",
                }
            context = evidence.segment_context(
                {
                    "chapter": chapter,
                    "index": index,
                    "before": 4,
                    "after": 4,
                }
            )
            context_segments = context.get("segments", []) if context.get("ok") else []
            nearby_pairs = [
                (str(item.get("source", "")), str(item.get("target", "")))
                for item in context_segments
                if isinstance(item, dict) and item.get("ref") != segment.ref
            ]
            context_source = "\n".join(
                str(item.get("source", "")) for item in context_segments if isinstance(item, dict)
            )
            relevant_terms = GlossaryStore.terms_in(
                all_terms,
                context_source or segment.source,
            )
            trace_path = f"fixers/ch{chapter}-text{index}.json"
            trace: dict[str, Any] = {
                "chapter": chapter,
                "index": index,
                "segment_ref": segment.ref,
                "issue_ids": [issue["issue_id"] for issue in issues],
                "status": "running",
            }
            debug.write_json(trace_path, trace)

            def record(event: str, data: dict[str, Any]) -> None:
                trace[event] = data
                debug.write_json(trace_path, trace)

            try:
                patch = fixer.propose(
                    review_round,
                    segment.ref,
                    chapter,
                    index,
                    segment.source,
                    segment.target,
                    issues,
                    style=style,
                    book_synopsis=book_synopsis,
                    chapter_digest=evidence.chapter_digests.get(chapter, ""),
                    relevant_glossary=relevant_terms,
                    nearby_pairs=nearby_pairs,
                    trace=record,
                )
            except Exception as error:  # noqa: BLE001 - 单段 Fix 失败保留为未解决建议
                trace["status"] = "failed"
                trace["error"] = {
                    "type": type(error).__name__,
                    "message": str(error),
                }
                debug.write_json(trace_path, trace)
                return None, {
                    "issue_ids": [issue["issue_id"] for issue in issues],
                    "chapter": chapter,
                    "index": index,
                    "segment_ref": segment.ref,
                    "status": "failed",
                    "reason": (
                        str(error)
                        if isinstance(error, ReviewFixerProtocolError)
                        else f"{type(error).__name__}: {error}"
                    ),
                }
            trace["status"] = "finished"
            trace["patch"] = patch.as_dict()
            debug.write_json(trace_path, trace)
            return patch, None

        workers = min(
            max(1, self.config.pipeline.review_concurrency),
            fix_total,
        )
        if workers == 1:
            results = []
            for done_count, job in enumerate(jobs, start=1):
                results.append(propose(job))
                if progress:
                    progress(done_count, fix_total, fix_label)
        else:
            ordered_results: list[tuple[ProvisionalPatch | None, dict[str, Any] | None] | None] = [
                None
            ] * fix_total
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(propose, job): position for position, job in enumerate(jobs)
                }
                for done_count, future in enumerate(as_completed(futures), start=1):
                    ordered_results[futures[future]] = future.result()
                    if progress:
                        progress(done_count, fix_total, fix_label)
            results = [result for result in ordered_results if result is not None]
        patches = [patch for patch, _ in results if patch is not None]
        failures = [failure for _, failure in results if failure is not None]
        debug.log_event(
            "review_fix_round_finished",
            patch_count=len(patches),
            skipped_count=len(skipped),
            failed_count=len(failures),
        )
        return patches, [*skipped, *failures]

    def _run_review_session(
        self,
        store: RunStore,
        all_terms: list[GlossaryTerm],
        *,
        progress: ProgressFn | None = None,
    ) -> ReviewOutcome:
        """在只读影子译文上循环 Review→临时 Fix→盲复审。

        正式 chapter、manifest 和术语库始终不变。每轮 Fix 只更新内存 overlay
        与本次 Review 目录；下一轮全书 Review 不接收旧问题说明，只读取修改后
        的影子译文。会话摘要、用量与正式事件在结束时持久化。
        """
        manifest = store.load_manifest()
        pending = [
            chapter["index"]
            for chapter in manifest.get("chapters", [])
            if chapter.get("status") != STATUS_DONE
        ]
        if pending:
            joined = ", ".join(str(index) for index in pending[:10])
            suffix = "…" if len(pending) > 10 else ""
            raise ValueError(f"全书审校要求所有章节先完成翻译；仍待翻译章节：{joined}{suffix}")

        chapter_rows = manifest.get("chapters", [])
        loaded = [store.load_chapter(item["index"]) for item in chapter_rows]
        total = sum(len(chapter.text_segments) for chapter in loaded)
        analysis = store.load_analysis() or {}
        reviewed_content_digest = _review_content_digest(loaded)
        debug = ReviewRunStore(store.run_dir)
        debug.start(
            reviewed_content_digest=reviewed_content_digest,
            metadata={
                "source_sha256": manifest.get("source_sha256"),
                "title": manifest.get("title"),
                "source_lang": self.config.source_lang,
                "target_lang": self.config.target_lang,
                "chapter_count": len(loaded),
                "total_segments": total,
                "config": {
                    "review_concurrency": self.config.pipeline.review_concurrency,
                    "review_output_retries": self.config.pipeline.review_output_retries,
                    "review_agent_loop": self.config.pipeline.review_agent_loop,
                    "review_agent_tier": self.config.pipeline.review_agent_tier,
                    "review_agent_max_evidence_rounds": (
                        self.config.pipeline.review_agent_max_evidence_rounds
                    ),
                    "review_conflict_arbitration": (
                        self.config.pipeline.review_conflict_arbitration
                    ),
                    "review_fix_loop": self.config.pipeline.review_fix_loop,
                    "review_fix_max_rounds": self.config.pipeline.review_fix_max_rounds,
                    "review_clean_confirmations": (self.config.pipeline.review_clean_confirmations),
                },
            },
        )
        store.log_event(
            "review_started",
            review_id=debug.review_id,
            review_dir=debug.run_dir,
            reviewed_content_digest=reviewed_content_digest,
        )
        usage_before = self.client.usage_summary()

        def save_review_usage() -> dict[str, Any]:
            """保存本次 Review 增量并合并到本书累计用量。"""
            usage = usage_delta(self.client.usage_summary(), usage_before)
            debug.save_usage(usage)
            self._flush_usage(store, scope="review")
            return usage

        target_overrides: dict[tuple[int, int], str] = {}
        seen_overlays = {_review_overlay_digest(loaded, target_overrides)}
        patch_records: list[dict[str, Any]] = []
        active_patches: dict[tuple[int, int], dict[str, Any]] = {}
        fix_failures: list[dict[str, Any]] = []
        blocked_issues: dict[str, dict[str, Any]] = {}
        round_summaries: list[dict[str, Any]] = []
        latest: _ReviewRoundResult | None = None
        clean_streak = 0
        fix_rounds = 0
        termination = "not_started"
        fix_loop = self.config.pipeline.review_fix_loop
        required_clean = self.config.pipeline.review_clean_confirmations if fix_loop else 1
        # 每次 Fix 前最多可能先出现 ``required_clean - 1`` 轮干净结果；
        # 每个已接受补丁后仍须保留完整盲审，并最终容纳连续 clean 确认。
        # 该上界避免在最后一轮接受一个从未被下一轮 Reviewer 看见的补丁。
        max_review_rounds = (
            (self.config.pipeline.review_fix_max_rounds + 1) * required_clean if fix_loop else 1
        )

        def register_blocked(
            issues: list[dict[str, Any]],
            failures: list[dict[str, Any]],
        ) -> None:
            """按稳定问题键保留 Fix 失败项，避免后续 Reviewer 漏报后假 clean。"""
            by_id = {
                str(issue["issue_id"]): issue
                for issue in issues
                if isinstance(issue.get("issue_id"), str)
            }
            for failure in failures:
                failure_ids = failure.get("issue_ids")
                if not isinstance(failure_ids, list):
                    failure_id = failure.get("issue_id")
                    failure_ids = [failure_id] if isinstance(failure_id, str) else []
                for issue_id in failure_ids:
                    issue = by_id.get(str(issue_id))
                    if issue is None:
                        continue
                    issue_key = issue.get("issue_key")
                    if not isinstance(issue_key, str) or not issue_key:
                        continue
                    blocked_issues[issue_key] = {
                        **dict(issue),
                        "fix_failure": {
                            "status": failure.get("status"),
                            "reason": failure.get("reason"),
                            "review_round": failure.get("review_round"),
                        },
                    }

        def effective_issues(current: _ReviewRoundResult) -> list[dict[str, Any]]:
            """合并本轮问题与历史未修项，按书序返回公开的未解决问题。"""
            combined = {
                str(issue["issue_key"]): dict(issue)
                for issue in current.issues
                if isinstance(issue.get("issue_key"), str)
            }
            for issue_key, blocked in blocked_issues.items():
                current_issue = combined.get(issue_key)
                if current_issue is None:
                    combined[issue_key] = dict(blocked)
                    continue
                fix_failure = blocked.get("fix_failure")
                if isinstance(fix_failure, dict):
                    current_issue["fix_failure"] = dict(fix_failure)
            return sorted(
                combined.values(),
                key=lambda issue: (
                    issue.get("chapter", -1),
                    issue.get("index", -1),
                    issue.get("review_round", -1),
                    issue.get("issue_id", ""),
                ),
            )

        try:
            for review_round in range(1, max_review_rounds + 1):
                overlay_digest = _review_overlay_digest(loaded, target_overrides)
                evidence = BookEvidenceIndex(
                    loaded,
                    all_terms,
                    analysis,
                    target_overrides=target_overrides,
                )
                with debug.round_scope(review_round):
                    debug.log_event(
                        "review_round_started",
                        overlay_digest=overlay_digest,
                        override_count=len(target_overrides),
                    )
                    debug.write_json(
                        "overlay.json",
                        [
                            {
                                "chapter": chapter,
                                "index": index,
                                "target": target,
                            }
                            for (chapter, index), target in sorted(target_overrides.items())
                        ],
                    )
                    latest = self._review_round(
                        loaded,
                        all_terms,
                        evidence,
                        debug,
                        review_round=review_round,
                        target_overrides=target_overrides,
                        progress=progress,
                    )

                    current_issue_keys = {
                        str(issue["issue_key"])
                        for issue in latest.issues
                        if isinstance(issue.get("issue_key"), str)
                    }
                    for patch_record in active_patches.values():
                        if patch_record.get("round", review_round) >= review_round:
                            continue
                        covered_issue_keys = {
                            str(issue_key)
                            for issue_key in patch_record.get("issue_keys", [])
                            if isinstance(issue_key, str)
                        }
                        rereported = sorted(covered_issue_keys & current_issue_keys)
                        not_rereported = sorted(covered_issue_keys - current_issue_keys)
                        for issue_key in not_rereported:
                            blocked_issues.pop(issue_key, None)
                        patch_record["rereported_issue_keys"] = rereported
                        patch_record["not_rereported_issue_keys"] = not_rereported
                        if rereported:
                            patch_record["status"] = "needs_revision"
                            patch_record["failed_review_round"] = review_round
                        else:
                            if patch_record.get("status") != "not_rereported":
                                patch_record["not_rereported_in_round"] = review_round
                            patch_record["status"] = "not_rereported"

                    round_summary: dict[str, Any] = {
                        "review_round": review_round,
                        "overlay_digest": overlay_digest,
                        "override_count": len(target_overrides),
                        "issue_count": len(latest.issues),
                        "conflict_count": len(latest.conflict_groups),
                        "unresolved_conflict_count": len(latest.residual_conflicts),
                        "fallback_agent_count": latest.fallback_agent_count,
                        "clean_streak_before": clean_streak,
                        "blocked_issue_count": len(blocked_issues),
                    }
                    if not latest.issues:
                        if blocked_issues:
                            clean_streak = 0
                            if progress:
                                progress(0, required_clean, "干净确认")
                            termination = "unresolved_fixes"
                            round_summary["clean_streak_after"] = 0
                            round_summary["patch_count"] = 0
                            round_summary["termination"] = termination
                            debug.write_json("summary.json", round_summary)
                            round_summaries.append(round_summary)
                            break
                        clean_streak += 1
                        if progress:
                            progress(clean_streak, required_clean, "干净确认")
                        round_summary["clean_streak_after"] = clean_streak
                        round_summary["patch_count"] = 0
                        if clean_streak >= required_clean:
                            termination = "clean_confirmed"
                            round_summary["termination"] = termination
                        debug.write_json("summary.json", round_summary)
                        round_summaries.append(round_summary)
                        if termination == "clean_confirmed":
                            break
                        continue

                    if clean_streak and progress:
                        progress(0, required_clean, "干净确认")
                    clean_streak = 0
                    round_summary["clean_streak_after"] = 0
                    if not fix_loop:
                        termination = "issues_reported"
                        round_summary["patch_count"] = 0
                        round_summary["termination"] = termination
                        debug.write_json("summary.json", round_summary)
                        round_summaries.append(round_summary)
                        break
                    if fix_rounds >= self.config.pipeline.review_fix_max_rounds:
                        termination = "max_rounds"
                        round_summary["patch_count"] = 0
                        round_summary["termination"] = termination
                        debug.write_json("summary.json", round_summary)
                        round_summaries.append(round_summary)
                        break

                    patches, failures = self._propose_review_patches(
                        latest,
                        evidence,
                        all_terms,
                        analysis,
                        debug,
                        review_round=review_round,
                        fix_round=fix_rounds + 1,
                        progress=progress,
                    )
                    fix_failures.extend(
                        [
                            {
                                **failure,
                                "review_round": review_round,
                            }
                            for failure in failures
                        ]
                    )
                    register_blocked(
                        latest.issues,
                        [
                            {
                                **failure,
                                "review_round": review_round,
                            }
                            for failure in failures
                        ],
                    )
                    round_summary["patch_count"] = len(patches)
                    if not patches:
                        termination = "no_progress"
                        round_summary["fix_failure_count"] = len(failures)
                        round_summary["blocked_issue_count"] = len(blocked_issues)
                        round_summary["termination"] = termination
                        debug.write_json("patches.json", [])
                        debug.write_json("fix_failures.json", failures)
                        debug.write_json("summary.json", round_summary)
                        round_summaries.append(round_summary)
                        break

                    issue_keys_by_id = {
                        str(issue["issue_id"]): str(issue["issue_key"])
                        for issue in latest.issues
                        if isinstance(issue.get("issue_id"), str)
                        and isinstance(issue.get("issue_key"), str)
                    }
                    candidate_overrides = dict(target_overrides)
                    applicable: list[ProvisionalPatch] = []
                    hash_failures: list[dict[str, Any]] = []
                    for patch in patches:
                        location = (patch.chapter, patch.index)
                        current = evidence.segment_ref(*location)
                        if (
                            current is None
                            or ReviewFixer.target_hash(current.target) != patch.before_hash
                        ):
                            failure = {
                                "patch_id": patch.patch_id,
                                "issue_ids": list(patch.issue_ids),
                                "chapter": patch.chapter,
                                "index": patch.index,
                                "status": "failed",
                                "reason": "before_hash_changed",
                                "review_round": review_round,
                            }
                            fix_failures.append(failure)
                            failures.append(failure)
                            hash_failures.append(failure)
                            continue
                        candidate_overrides[location] = patch.after
                        applicable.append(patch)

                    register_blocked(
                        latest.issues,
                        hash_failures,
                    )
                    round_summary["fix_failure_count"] = len(failures)
                    round_summary["blocked_issue_count"] = len(blocked_issues)
                    candidate_digest = _review_overlay_digest(
                        loaded,
                        candidate_overrides,
                    )
                    debug.write_json(
                        "patches.json",
                        [patch.as_dict() for patch in patches],
                    )
                    debug.write_json("fix_failures.json", failures)
                    round_summary["candidate_overlay_digest"] = candidate_digest
                    round_summary["applicable_patch_count"] = len(applicable)
                    if not applicable or candidate_digest == overlay_digest:
                        termination = "no_progress"
                        round_summary["termination"] = termination
                        debug.write_json("summary.json", round_summary)
                        round_summaries.append(round_summary)
                        break
                    if candidate_digest in seen_overlays:
                        termination = "cycle_detected"
                        for patch in applicable:
                            record = {
                                **patch.as_dict(),
                                "issue_keys": sorted(
                                    {
                                        issue_keys_by_id[issue_id]
                                        for issue_id in patch.issue_ids
                                        if issue_id in issue_keys_by_id
                                    }
                                ),
                                "status": "rejected_cycle",
                            }
                            patch_records.append(record)
                        round_summary["termination"] = termination
                        debug.write_json("summary.json", round_summary)
                        round_summaries.append(round_summary)
                        break

                    fix_rounds += 1
                    for patch in applicable:
                        location = (patch.chapter, patch.index)
                        previous = active_patches.get(location)
                        record = patch.as_dict()
                        record["issue_keys"] = sorted(
                            {
                                issue_keys_by_id[issue_id]
                                for issue_id in patch.issue_ids
                                if issue_id in issue_keys_by_id
                            }
                        )
                        if previous is not None:
                            previous["status"] = "superseded"
                            previous["superseded_by"] = patch.patch_id
                        patch_records.append(record)
                        active_patches[location] = record
                    target_overrides = candidate_overrides
                    seen_overlays.add(candidate_digest)
                    round_summary["fix_round"] = fix_rounds
                    debug.write_json("summary.json", round_summary)
                    round_summaries.append(round_summary)
            else:
                termination = "max_rounds"

            if latest is None:  # pragma: no cover - max_review_rounds 至少为 1
                raise RuntimeError("Review loop finished without a review round")

            unresolved = effective_issues(latest)
            final_conflicts = _review_unresolved_conflict_records(
                unresolved,
                build_conflict_groups,
            )
            final_residual_conflicts = [
                record
                for record in final_conflicts
                if record.get("arbitration", {}).get("status") == "unresolved"
            ]
            final_fallback_agent_count = _review_unresolved_fallback_count(unresolved)
            initial_issues, dismissed = debug.result_snapshots()
            debug.write_json("rounds/final/initial_issues.json", initial_issues)
            debug.write_json("rounds/final/dismissed_issues.json", dismissed)
            debug.write_json(
                "rounds/final/pre_arbitration_issues.json",
                latest.pre_arbitration_issues,
            )
            debug.write_json(
                "rounds/final/arbitration_superseded_issues.json",
                latest.arbitration_superseded,
            )
            debug.write_json(
                "rounds/final/residual_conflicts.json",
                [
                    {
                        "conflict_id": record["conflict_id"],
                        "consistency_key": record["consistency_key"],
                        "issue_ids": record["issue_ids"],
                    }
                    for record in final_residual_conflicts
                ],
            )
            debug.write_json("rounds/final/conflicts.json", final_conflicts)
            debug.write_json("rounds/final/patch-history.json", patch_records)
            debug.write_json(
                "rounds/final/not_rereported_patches.json",
                [patch for patch in patch_records if patch["status"] == "not_rereported"],
            )
            debug.write_json(
                "rounds/final/unresolved_issues.json",
                unresolved,
            )
            debug.write_json("rounds/final/fix_failures.json", fix_failures)
            debug.write_json("rounds/final/rounds.json", round_summaries)
            debug.write_json(
                "rounds/final/shadow_targets.json",
                [
                    {
                        "chapter": chapter,
                        "index": index,
                        "target": target,
                    }
                    for (chapter, index), target in sorted(target_overrides.items())
                ],
            )
            public_issues = _review_public_issues(unresolved)
            changes = _review_net_changes(
                loaded,
                target_overrides,
                patch_records,
                active_patches,
            )
            summary = {
                "initial_issue_count": len(initial_issues),
                "dismissed_issue_count": len(dismissed),
                "pre_arbitration_issue_count": len(latest.pre_arbitration_issues),
                "arbitration_superseded_count": len(latest.arbitration_superseded),
                "issue_count": len(public_issues),
                "conflict_count": len(final_conflicts),
                "unresolved_conflict_count": len(final_residual_conflicts),
                "fallback_agent_count": final_fallback_agent_count,
                "review_round_count": len(round_summaries),
                "fix_round_count": fix_rounds,
                "patch_count": len(patch_records),
                "change_count": len(changes),
                "not_rereported_patch_count": sum(
                    patch["status"] == "not_rereported" for patch in patch_records
                ),
                "shadow_override_count": len(target_overrides),
                "blocked_issue_count": len(blocked_issues),
                "clean_streak": clean_streak,
            }
            debug.write_json("rounds/final/summary.json", summary)
            result = debug.finish(
                status="completed",
                termination=termination,
                summary=summary,
                issues=public_issues,
                changes=changes,
            )
            usage = save_review_usage()
            store.log_event(
                "review_finished",
                review_id=debug.review_id,
                review_dir=debug.run_dir,
                status="completed",
                termination=termination,
                issue_count=len(public_issues),
                change_count=len(changes),
            )
            return ReviewOutcome(
                run_dir=debug.run_dir,
                result=result,
                usage=usage,
            )
        except Exception as error:
            initial_issues, dismissed = debug.result_snapshots()
            partial_issues = effective_issues(latest) if latest is not None else []
            public_issues = _review_public_issues(partial_issues)
            partial_changes = _review_net_changes(
                loaded,
                target_overrides,
                patch_records,
                active_patches,
            )
            debug.write_json("rounds/final/initial_issues.json", initial_issues)
            debug.write_json("rounds/final/dismissed_issues.json", dismissed)
            debug.write_json(
                "rounds/final/partial_issues.json",
                partial_issues,
            )
            debug.write_json("rounds/final/partial_patches.json", patch_records)
            debug.write_json("rounds/final/fix_failures.json", fix_failures)
            debug.finish(
                status="failed",
                termination="error",
                summary={
                    "issue_count": len(public_issues),
                    "change_count": len(partial_changes),
                    "conflict_count": (len(latest.conflict_groups) if latest is not None else 0),
                    "fallback_agent_count": (
                        latest.fallback_agent_count if latest is not None else 0
                    ),
                },
                issues=public_issues,
                changes=partial_changes,
                error={"type": type(error).__name__, "message": str(error)},
            )
            save_review_usage()
            store.log_event(
                "review_finished",
                review_id=debug.review_id,
                review_dir=debug.run_dir,
                status="failed",
                termination="error",
                issue_count=len(public_issues),
                change_count=len(partial_changes),
                error_type=type(error).__name__,
                error=str(error),
            )
            raise

    def _review_chapter(
        self,
        text_segs,
        terms,
        *,
        chapter_index: int | None = None,
        evidence: BookEvidenceIndex | None = None,
        debug: ReviewRunStore | None = None,
        target_overrides: Mapping[tuple[int, int], str] | None = None,
        review_round: int | None = None,
        on_chunk_finished: Callable[[int], None] | None = None,
    ) -> list[dict]:
        """保留旧私有 seam，并把叶块审校依赖动态注入专属模块。"""
        return review_legacy_chapter(
            text_segs,
            terms,
            config=self.config,
            client=self.client,
            reviewer=self.reviewer,
            recoverable_error=ReviewOutputError,
            agent_loop_factory=ReviewAgentLoop,
            pack_contiguous=self._pack_contiguous,
            chapter_index=chapter_index,
            evidence=evidence,
            debug=debug,
            target_overrides=target_overrides,
            review_round=review_round,
            on_chunk_finished=on_chunk_finished,
        )

    @staticmethod
    def _pack_contiguous(segs, budget: int) -> list[list[Segment]]:
        """兼容旧私有入口；按字符预算保序打包审校段落。"""
        return pack_legacy_review_chunks(segs, budget)

    def _process_batch(
        self,
        batch,
        terms,
        ctx_text: str,
        style: str,
        book_synopsis: str = "",
        chapter_digest: str = "",
        annotation_contexts: list[list[dict[str, str]]] | None = None,
    ) -> _BatchResult:
        """兼容旧私有入口；把旧 Agent 与抽样策略注入批次处理函数。"""
        return process_legacy_batch(
            batch,
            terms,
            ctx_text,
            style,
            book_synopsis,
            chapter_digest,
            annotation_contexts,
            translate_batch=self.translator.translate_batch,
            polish_batch=self.polisher.polish,
            # 保持旧读取时机：Translator/Polisher 回调可在本批内更新动态配置。
            polish_enabled=lambda: self.config.pipeline.polish,
            backtranslate_sample=lambda: self.config.pipeline.backtranslate_sample,
            random_sample=random.random,
        )

    # ── 可选步骤 / 连续全流程 ────────────────────────────────────────────────
    ALL_STEPS = ("translate", "review", "report", "assemble")

    @_record_run_metrics("review", ["review"])
    def run_review(
        self,
        input_path: str,
        *,
        progress: ProgressFn | None = None,
    ) -> dict[str, Any]:
        """全量执行只读 Review，并保存正式结果、事件与用量。"""
        store = self._measure_stage_call(
            "prepare",
            self._locate_existing_store,
            input_path,
            progress=progress,
        )
        with store.lock():
            manifest = store.load_manifest()
            self._apply_manifest_languages(manifest)
            terms = GlossaryStore.load_terms_readonly(store.glossary_path)
            outcome = self._measure_stage_call(
                "review",
                self._run_review_session,
                store,
                terms,
                progress=progress,
            )
            self._capture_metrics_state(store)
        return {
            "store": store,
            "review_issues": outcome.issues,
            "review_changes": outcome.changes,
            "review_result": outcome.result,
            "review_dir": outcome.run_dir,
        }

    def _run_existing_steps(
        self,
        input_path: str,
        steps: set[str],
        *,
        progress: ProgressFn | None,
        out_format: str = "epub",
        out_path: str | None = None,
        pdf_engine: str = "weasyprint",
    ) -> dict[str, Any]:
        """仅从既有状态执行本地收尾阶段，不创建新的翻译任务。"""
        store = self._measure_stage_call(
            "prepare",
            self._locate_existing_store,
            input_path,
            progress=progress,
        )
        with store.lock():
            manifest = store.load_manifest()
            self._apply_manifest_languages(manifest)
            result = self._finish_steps_locked(
                store,
                input_path=input_path,
                steps=steps,
                run_steps_input=sorted(steps),
                progress=progress,
                out_format=out_format,
                out_path=out_path,
                pdf_engine=pdf_engine,
            )
            self._capture_metrics_state(store)
            return result

    @_record_run_metrics("report", ["report"])
    def run_report(
        self,
        input_path: str,
        *,
        progress: ProgressFn | None = None,
    ) -> dict[str, Any]:
        """从既有状态重新生成报告，并记录独立运行指标。"""
        return self._run_existing_steps(
            input_path,
            {"report"},
            progress=progress,
        )

    @_record_run_metrics(
        "assemble",
        ["assemble"],
        invocation_fields=("out_format", "pdf_engine"),
    )
    def run_assemble(
        self,
        input_path: str,
        *,
        out_format: str = "epub",
        out_path: str | None = None,
        pdf_engine: str = "weasyprint",
        progress: ProgressFn | None = None,
    ) -> dict[str, Any]:
        """从既有状态快照导出成品，不等待正在进行的整本翻译。"""
        store = self._measure_stage_call(
            "prepare",
            self._locate_existing_store,
            input_path,
            progress=progress,
        )
        store.log_event("run_steps_started", steps=["assemble"], input_path=input_path)

        with store.assemble_lock():
            snapshot = self._measure_stage_call(
                "prepare",
                store.create_export_snapshot,
                actual_sha256=self._source_sha256(input_path),
            )
            manifest = snapshot.load_manifest()
            self._apply_manifest_languages(manifest)
            self._capture_metrics_state(snapshot)
            # 等待另一个导出期间源文件也可能变化，因此真正渲染前再次确认。
            self._ensure_store_source(store, input_path)
            outputs = self._assemble_outputs(
                snapshot,
                input_path=input_path,
                progress=progress,
                out_format=out_format,
                out_path=out_path,
                pdf_engine=pdf_engine,
            )
            # 原模板也属于导出输入；渲染后再次核验，避免把中途替换的源文件记为成功。
            self._ensure_store_source(store, input_path)
        store.log_event("assembled", outputs=outputs, out_format=out_format)
        store.log_event("run_steps_finished", steps=["assemble"], outputs=outputs)
        return {
            "store": store,
            "output": outputs[0] if outputs else None,
            "outputs": outputs,
            "report": None,
            "review_issues": [],
            "review_changes": [],
            "review_result": None,
            "review_dir": None,
        }

    @_record_pipeline_metrics
    def run_steps(
        self,
        input_path: str,
        steps,
        *,
        progress: ProgressFn | None = None,
        out_format: str = "epub",
        out_path: str | None = None,
        pdf_engine: str = "weasyprint",
    ) -> dict[str, Any]:
        """按需执行步骤子集（可单选可全选）。steps ⊆ ALL_STEPS。"""
        steps = set(steps)
        run_steps_input = sorted(steps)
        if steps == {"review"}:
            reviewed = self.run_review(input_path, progress=progress)
            return {
                "store": reviewed["store"],
                "output": None,
                "outputs": [],
                "report": None,
                "review_issues": reviewed["review_issues"],
                "review_changes": reviewed["review_changes"],
                "review_result": reviewed["review_result"],
                "review_dir": reviewed["review_dir"],
            }
        if steps == {"assemble"}:
            return self.run_assemble(
                input_path,
                out_format=out_format,
                out_path=out_path,
                pdf_engine=pdf_engine,
                progress=progress,
            )

        if "translate" in steps:
            store = self.run(input_path, progress=progress)
        else:
            store = self._measure_stage_call(
                "prepare",
                self.prepare,
                input_path,
                progress=progress,
            )
            m = store.load_manifest()
            self._apply_manifest_languages(m)
        with store.lock():
            result = self._finish_steps_locked(
                store,
                input_path=input_path,
                steps=steps,
                run_steps_input=run_steps_input,
                progress=progress,
                out_format=out_format,
                out_path=out_path,
                pdf_engine=pdf_engine,
            )
            self._capture_metrics_state(store)
            return result

    def _assemble_outputs(
        self,
        store: RunStore,
        *,
        input_path: str,
        progress: ProgressFn | None,
        out_format: str,
        out_path: str | None,
        pdf_engine: str,
    ) -> list[str]:
        """从给定实时状态或只读快照生成配置要求的全部产物。"""
        from ..assemble.writer import assemble, bilingual_out_path

        out_cfg = self.config.output
        # 锁、快照与 source hash 校验属于上层调用路径；服务只消费这里交给它的状态视图。
        return assemble_outputs(
            store,
            input_path=input_path,
            progress=progress,
            out_format=out_format,
            out_path=out_path,
            pdf_engine=pdf_engine,
            options=PublishingOptions(
                mono=out_cfg.mono,
                bilingual=out_cfg.bilingual,
                bilingual_order=out_cfg.bilingual_order,
                bilingual_preserve_source_style=out_cfg.bilingual_preserve_source_style,
                about_page=out_cfg.about_page,
            ),
            renderer=assemble,
            bilingual_path=bilingual_out_path,
            stage_call=self._measure_stage_call,
        )

    def _finish_steps_locked(
        self,
        store: RunStore,
        *,
        input_path: str,
        steps: set[str],
        run_steps_input: list[str],
        progress: ProgressFn | None,
        out_format: str,
        out_path: str | None,
        pdf_engine: str,
    ) -> dict[str, Any]:
        """在书级锁内执行审校、报告和导出收尾步骤并返回结果汇总。"""
        from ..assemble.report import build_report

        store.log_event("run_steps_started", steps=run_steps_input, input_path=input_path)

        glossary = GlossaryStore(store.glossary_path) if "report" in steps else None
        review_issues: list[dict] = []
        review_changes: list[dict] = []
        review_result: dict[str, Any] | None = None
        review_dir: str | None = None
        report: dict[str, Any] | None = None
        try:
            if "review" in steps:
                # 先保存此前阶段的增量，使会话 usage.json 只包含 Review 调用。
                self._flush_usage(store, scope="pipeline")
                outcome = self._measure_stage_call(
                    "review",
                    self._run_review_session,
                    store,
                    (
                        glossary.all_terms()
                        if glossary is not None
                        else GlossaryStore.load_terms_readonly(store.glossary_path)
                    ),
                    progress=progress,
                )
                review_issues = outcome.issues
                review_changes = outcome.changes
                review_result = outcome.result
                review_dir = outcome.run_dir

            self._flush_usage(store, scope="pipeline")
            if "report" in steps:
                if glossary is None:  # pragma: no cover - 由 needs 条件保证
                    raise RuntimeError("报告生成需要术语库")
                if progress:
                    progress(0, 0, "生成报告…")
                report = self._measure_stage_call(
                    "report",
                    build_report,
                    store,
                    glossary,
                )
                assert report is not None
                store.save_report(report)
                store.log_event("report_saved", path=store.report_path)
        finally:
            if glossary is not None:
                glossary.close()
            self._flush_usage(store, scope="pipeline")

        outputs: list[str] = []
        if "assemble" in steps:
            with store.assemble_lock():
                # 导出会重新读取源书模板；在读取前后都验证，避免运行期间替换文件。
                self._ensure_store_source(store, input_path)
                outputs = self._assemble_outputs(
                    store,
                    input_path=input_path,
                    progress=progress,
                    out_format=out_format,
                    out_path=out_path,
                    pdf_engine=pdf_engine,
                )
                self._ensure_store_source(store, input_path)
            store.log_event("assembled", outputs=outputs, out_format=out_format)

        store.log_event(
            "run_steps_finished",
            steps=run_steps_input,
            outputs=outputs,
        )
        return {
            "store": store,
            "output": outputs[0] if outputs else None,
            "outputs": outputs,
            "report": report,
            "review_issues": review_issues,
            "review_changes": review_changes,
            "review_result": review_result,
            "review_dir": review_dir,
        }

    def run_all(
        self,
        input_path: str,
        *,
        progress: ProgressFn | None = None,
        out_format: str = "epub",
        out_path: str | None = None,
        pdf_engine: str = "weasyprint",
    ) -> dict[str, Any]:
        """翻译 → 最终审校 → 报告 → 回填，返回结果汇总。"""
        steps = {"translate", "report", "assemble"}
        if self.config.pipeline.review:
            steps.add("review")
        return self.run_steps(
            input_path,
            steps,
            progress=progress,
            out_format=out_format,
            out_path=out_path,
            pdf_engine=pdf_engine,
        )
