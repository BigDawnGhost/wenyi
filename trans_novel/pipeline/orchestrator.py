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

import hashlib
import json
import os
import random
import warnings
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import wraps
from inspect import signature
from threading import Lock
from typing import Any, Iterator

from ..agents.analyzer import Analyzer
from ..agents.annotation_aligner import (
    AnnotationAligner,
    AnnotationUnit,
    target_digest,
)
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
from ..config import Config
from ..glossary.extractor import GlossaryExtractor, TranslatedSegmentEvidence
from ..glossary.store import GlossaryStore, GlossaryTerm
from ..ingest.models import Chapter, Segment
from ..ingest.segmenter import load_document
from ..llm.base import LLMClient
from ..llm.factory import build_client
from ..llm.usage import merge_usage_summaries, usage_delta
from ..postprocess.punct import normalize_zh_segments
from ..services.document_sampling import sample_document_text
from ..services.translation_batches import (
    plan_contiguous_batches,
    plan_resumable_batches,
)
from .context import RollingContext
from .metrics import RunMetricsRecorder
from .review_evidence import BookEvidenceIndex
from .review_run import ReviewOutcome, ReviewRunStore
from .runstore import STATUS_DONE, RunStore, slugify, source_sha256

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
    """向 Rich 桥报告双层进度，普通回调仍接收原有全书累计值。"""
    if progress is None:
        return
    nested_update = getattr(progress, "update_translation", None)
    if callable(nested_update):
        nested_update(
            chapter_done,
            chapter_total,
            overall_done,
            overall_total,
            label,
        )
        return
    progress(overall_done, overall_total, label)


# 语言名/代码 → ISO 639-1 两字母代码（模型检测结果归一化）
_LANG_ALIASES = {
    "japanese": "ja",
    "日语": "ja",
    "日文": "ja",
    "jp": "ja",
    "jpn": "ja",
    "english": "en",
    "英语": "en",
    "英文": "en",
    "eng": "en",
    "russian": "ru",
    "俄语": "ru",
    "俄文": "ru",
    "rus": "ru",
    "chinese": "zh",
    "中文": "zh",
    "汉语": "zh",
    "zh-cn": "zh",
    "zho": "zh",
    "korean": "ko",
    "韩语": "ko",
    "韩文": "ko",
    "kor": "ko",
    "french": "fr",
    "法语": "fr",
    "法文": "fr",
    "german": "de",
    "德语": "de",
    "德文": "de",
    "spanish": "es",
    "西班牙语": "es",
    "西班牙文": "es",
    "italian": "it",
    "意大利语": "it",
    "意大利文": "it",
    "portuguese": "pt",
    "葡萄牙语": "pt",
    "葡萄牙文": "pt",
}


def _normalize_lang(code: str) -> str:
    """把模型返回的语言名或别名规整为 ISO 639-1 两字母代码。"""
    c = (code or "").strip().lower()
    if not c or c in {"auto", "unknown", "und", "uncertain", "mixed", "多语言", "未知"}:
        return ""
    if c in _LANG_ALIASES:
        return _LANG_ALIASES[c]
    return c[:2] if c[:2].isalpha() else ""


def _resume_batches(segments: list[Segment], max_chars: int) -> list[list[Segment]]:
    """按字符预算分批后，再沿“已完成/待翻译”边界切开。

    用户调整批次预算时，新的批次可能同时包含已有译文和空译文。若直接重跑
    该混合批次会覆盖已确认内容；按完成状态分组可只补译缺失段。
    """
    return [
        segments[plan.start_index : plan.stop_index]
        for plan in plan_resumable_batches(segments, max_chars)
    ]


@dataclass
class _BatchResult:
    targets: list[str]
    bt_samples: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class _ReviewRoundResult:
    """一次全书影子译文 Review 及冲突仲裁后的确定性结果。"""

    issues: list[dict[str, Any]]
    pre_arbitration_issues: list[dict[str, Any]]
    arbitration_superseded: list[dict[str, Any]]
    conflict_groups: list[dict[str, Any]]
    residual_conflicts: list[dict[str, Any]]
    fallback_agent_count: int


def _review_overlay_digest(
    chapters,
    overrides: Mapping[tuple[int, int], str],
) -> str:
    """计算全书有效影子译文指纹，用于检测无进展与 A↔B 振荡。"""
    payload = [
        (
            chapter.index,
            text_index,
            overrides.get((chapter.index, text_index), segment.target or ""),
        )
        for chapter in chapters
        for text_index, segment in enumerate(chapter.text_segments)
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _review_content_digest(chapters) -> str:
    """计算本次 Review 实际读取的正式正文摘要。"""
    payload = [
        (
            chapter.index,
            text_index,
            segment.index,
            segment.anchor or "",
            segment.kind,
            segment.source,
            segment.target or "",
        )
        for chapter in chapters
        for text_index, segment in enumerate(chapter.text_segments)
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _review_net_changes(
    chapters,
    overrides: Mapping[tuple[int, int], str],
    patch_records: list[dict[str, Any]],
    active_patches: Mapping[tuple[int, int], dict[str, Any]],
) -> list[dict[str, Any]]:
    """把多轮影子补丁折叠成每段一条的最终修改建议。"""
    baseline = {
        (chapter.index, text_index): segment.target or ""
        for chapter in chapters
        for text_index, segment in enumerate(chapter.text_segments)
    }
    issue_keys_by_location: dict[tuple[int, int], set[str]] = {}
    for patch in patch_records:
        chapter = patch.get("chapter")
        index = patch.get("index")
        if (
            not isinstance(chapter, int)
            or isinstance(chapter, bool)
            or not isinstance(index, int)
            or isinstance(index, bool)
            or patch.get("status") == "rejected_cycle"
        ):
            continue
        keys = issue_keys_by_location.setdefault((chapter, index), set())
        keys.update(str(key) for key in patch.get("issue_keys", []) if isinstance(key, str) and key)

    changes: list[dict[str, Any]] = []
    for location, suggested_target in sorted(overrides.items()):
        if baseline.get(location) == suggested_target:
            continue
        active = active_patches.get(location) or {}
        changes.append(
            {
                "chapter": location[0],
                "index": location[1],
                "suggested_target": suggested_target,
                "issue_keys": sorted(issue_keys_by_location.get(location, set())),
                "review_result": str(active.get("status") or "provisional"),
            }
        )
    return changes


def _review_public_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """裁剪内部审校字段，生成面向用户的稳定问题列表。"""
    public: dict[str, dict[str, Any]] = {}
    for issue in issues:
        issue_key = issue.get("issue_key")
        chapter = issue.get("chapter")
        index = issue.get("index")
        if (
            not isinstance(issue_key, str)
            or not issue_key
            or not isinstance(chapter, int)
            or isinstance(chapter, bool)
            or not isinstance(index, int)
            or isinstance(index, bool)
        ):
            continue
        public[issue_key] = {
            "issue_key": issue_key,
            "chapter": chapter,
            "index": index,
            "type": str(issue.get("type") or ""),
            "detail": str(issue.get("detail") or ""),
            "suggestion": str(issue.get("suggestion") or ""),
        }
    return sorted(
        public.values(),
        key=lambda issue: (issue["chapter"], issue["index"], issue["issue_key"]),
    )


def _review_conflict_records(
    groups: list[dict[str, Any]],
    arbitrations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """把冲突组及对应仲裁结果序列化为稳定的逐轮记录。"""
    return [
        {
            "conflict_id": group["conflict_id"],
            "consistency_key": group["consistency_key"],
            "issue_ids": [issue["issue_id"] for issue in group["issues"]],
            "proposals": [
                {
                    "issue_id": issue["issue_id"],
                    "chapter": issue["chapter"],
                    "index": issue["index"],
                    "proposed_value": issue["consistency"]["proposed_value"],
                }
                for issue in group["issues"]
            ],
            "arbitration": arbitration,
        }
        for group, arbitration in zip(groups, arbitrations)
    ]


def _review_unresolved_conflict_records(
    issues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """从最终未解决问题重建冲突记录，避免被最后一轮空结果掩盖。"""
    groups = build_conflict_groups(issues)
    arbitrations: list[dict[str, Any]] = []
    for group in groups:
        issue_ids = [str(issue["issue_id"]) for issue in group["issues"]]
        annotations = [
            issue.get("arbitration")
            for issue in group["issues"]
            if isinstance(issue.get("arbitration"), dict)
        ]
        reasons = [
            str(annotation.get("reason", "")).strip()
            for annotation in annotations
            if str(annotation.get("reason", "")).strip()
        ]
        evidence_refs = sorted(
            {
                str(ref)
                for issue in group["issues"]
                for ref in issue.get("evidence_refs", [])
                if isinstance(ref, str) and ref
            }
        )
        arbitrations.append(
            {
                "conflict_id": group["conflict_id"],
                "consistency_key": group["consistency_key"],
                "issue_ids": issue_ids,
                "status": "unresolved",
                "recommended_value": "",
                "reason": reasons[-1] if reasons else "最终未解决问题仍包含互斥建议。",
                "supported_issue_ids": issue_ids,
                "rejected_issue_ids": [],
                "evidence_refs": evidence_refs,
            }
        )
    return _review_conflict_records(groups, arbitrations)


def _review_unresolved_fallback_count(issues: list[dict[str, Any]]) -> int:
    """统计最终未解决问题中仍由降级 Agent 产生的独立审校块。"""
    return len(
        {
            str(issue.get("_chunk_id") or issue.get("issue_key") or issue.get("issue_id"))
            for issue in issues
            if issue.get("agent_fallback")
        }
    )


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
        if self._active_run_metrics is not None:
            verified = self._active_run_metrics.verify_input_sha256(input_path)
            if verified is not None:
                return verified
        digest = source_sha256(input_path)
        if self._active_run_metrics is not None:
            self._active_run_metrics.input["sha256"] = digest
        return digest

    def _initial_source_sha256(self, input_path: str) -> str:
        """取得解析前内容快照；有指标时复用其启动快照以少读一次文件。"""
        if self._active_run_metrics is not None:
            initial = self._active_run_metrics.input.get("sha256")
            if isinstance(initial, str):
                return initial
        return source_sha256(input_path)

    def _ensure_store_source(self, store: RunStore, input_path: str) -> str:
        """校验候选状态确实属于当前输入文件。"""
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
        if os.path.splitext(input_path)[1].lower() == ".pdf":
            title = os.path.splitext(os.path.basename(input_path))[0]
        else:
            if progress:
                progress(0, 0, "查找翻译进度…")
            doc = load_document(
                input_path,
                self.config.source_lang,
                self.config.target_lang,
                split_segments=self.config.segment.max_chars_per_segment,
            )
            title = doc.title

        store = RunStore(
            os.path.join(self.config.state_dir, slugify(title)),
            create=False,
        )
        if not store.exists():
            raise ValueError("尚无翻译进度。请先运行 translate。")
        self._ensure_store_source(store, input_path)
        self._bind_llm_events(store, progress)
        self._attach_metrics_store(store)
        return store

    def prepare(self, input_path: str, *, progress: ProgressFn | None = None) -> RunStore:
        """解析输入并定位状态目录；首次运行时在书级锁内完成初始化。

        PDF 的状态目录可直接由文件名确定，因此续跑时先检查 manifest，
        避免重新调用外部转换服务；首次转换产生的 HTML 缓存在该状态目录中。
        """
        if os.path.splitext(input_path)[1].lower() == ".pdf":
            # PDF 的书名固定取文件名，首次解析前即可确定状态目录。
            pdf_title = os.path.splitext(os.path.basename(input_path))[0]
            run_dir = os.path.join(self.config.state_dir, slugify(pdf_title))
            store = RunStore(run_dir)
            self._bind_llm_events(store, progress)
            self._attach_metrics_store(store)
            with store.lock():
                if store.exists():
                    self._ensure_store_source(store, input_path)
                    store.log_event(
                        "run_resumed",
                        input_path=input_path,
                        run_dir=store.run_dir,
                    )
                    return store
                if progress:
                    progress(0, 0, "解析文档…")
                source_hash = self._initial_source_sha256(input_path)
                # 转换失败也要留下同源初始化标记，确保重试保留失败运行账本。
                store.begin_initialization(source_hash)
                doc = load_document(
                    input_path,
                    self.config.source_lang,
                    self.config.target_lang,
                    split_segments=self.config.segment.max_chars_per_segment,
                    cache_dir=store.source_dir,
                    source_hash=source_hash,
                )
                if self._source_sha256(input_path) != source_hash:
                    raise ValueError("PDF 在解析期间发生变化；请确认文件稳定后重试。")
                return self._prepare_locked(
                    doc,
                    store,
                    input_path,
                    progress,
                    source_hash=source_hash,
                )

        if progress:
            progress(0, 0, "解析文档…")
        source_hash = self._initial_source_sha256(input_path)
        # 超长段按句拆分（max_chars_per_segment），续段标 cont 供回填并回
        doc = load_document(
            input_path,
            self.config.source_lang,
            self.config.target_lang,
            split_segments=self.config.segment.max_chars_per_segment,
        )
        if self._source_sha256(input_path) != source_hash:
            raise ValueError("源文件在解析期间发生变化；请确认文件稳定后重试。")
        run_dir = os.path.join(self.config.state_dir, slugify(doc.title))
        store = RunStore(run_dir)
        self._bind_llm_events(store, progress)
        self._attach_metrics_store(store)
        with store.lock():
            return self._prepare_locked(
                doc,
                store,
                input_path,
                progress,
                source_hash=source_hash,
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
        if store.exists():
            self._ensure_store_source(store, input_path)
            store.log_event("run_resumed", input_path=input_path, run_dir=store.run_dir)
            return store  # 已有进度 → 直接续跑，不重置（语言在 run() 里按 manifest 应用）

        initialization_hash = source_hash or self._source_sha256(input_path)
        store.begin_initialization(initialization_hash)

        # 新建：auto 时只使用模型检测主要语言；失败则要求用户显式指定。
        if self.config.source_lang in ("auto", "", None):
            if progress:
                progress(0, 0, "识别语言…")
            detected = self._detect_language_ai(doc)
            if not detected:
                store.log_event("language_detection_failed", source_lang=doc.source_lang)
                raise RuntimeError(
                    "自动识别源语言失败：请检查模型配置，或在 config.yaml 的 "
                    "language.source 指定 ISO 639-1 语言代码（如 ja/en/ko/ru/fr/de/es）。"
                )
            doc.source_lang = detected
            store.log_event("language_detected", source_lang=doc.source_lang)
        self._apply_language(doc.source_lang)

        manifest = store.stage_document(
            doc,
            source_hash=initialization_hash,
        )
        glossary = GlossaryStore(store.glossary_path)
        try:
            if progress:
                progress(0, 0, "分析全书风格…")
            sample = self._sample_text(doc)
            analysis = self.analyzer.analyze(sample) if sample else {}
            if analysis:
                self.analyzer.seed_glossary(glossary, analysis)
            store.save_analysis(analysis)
            store.log_event("analysis_saved", has_analysis=bool(analysis))
            store.save_context(
                RollingContext(
                    max_recent_keep=max(40, self.config.pipeline.rolling_context_segments)
                ).to_dict()
            )

            # manifest 是初始化完成标志，必须最后原子落盘。
            manifest["initialized"] = True
            store.save_manifest(manifest)
            store.finish_initialization()
            store.log_event(
                "run_initialized",
                input_path=input_path,
                run_dir=store.run_dir,
                title=doc.title,
                fmt=doc.fmt,
                source_lang=doc.source_lang,
                target_lang=doc.target_lang,
                chapters=len(doc.chapters),
                config={
                    "review": self.config.pipeline.review,
                    "polish": self.config.pipeline.polish,
                    "backtranslate_sample": self.config.pipeline.backtranslate_sample,
                    "book_understanding": self.config.pipeline.book_understanding,
                    "review_concurrency": self.config.pipeline.review_concurrency,
                    "review_output_retries": (self.config.pipeline.review_output_retries),
                },
            )
        finally:
            glossary.close()
        return store

    def _detect_language_ai(self, doc) -> str:
        """用模型检测正文主要语言，返回 ISO 代码（如 ja/en/ru）。失败返回空串。"""
        # labeled=False：纯源文样本，防多点采样的中文标签污染语言检测
        sample = self._sample_text(doc, labeled=False)[:1500]
        if not sample.strip():
            return ""
        system = (
            "你是语言识别器。判断给定文本的主要自然语言，"
            '仅输出 JSON：{"language":"<ISO 639-1 两字母代码，如 ja/en/ru/ko/fr/de/zh>"}。'
            "无法判断时 language 置为空字符串。"
        )
        try:
            data = self.client.complete_json(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": sample},
                ],
                tier="cheap",
                stage="language_detect",
            )
            code = (data.get("language") if isinstance(data, dict) else "") or ""
            return _normalize_lang(str(code))
        except Exception:  # noqa: BLE001 - provider errors mean detection failed
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
        """把一批最新原译文写入内存位置索引。"""
        for offset, segment in enumerate(segments):
            target = (segment.target or "").strip()
            if not target:
                continue
            segment_index = start_index + offset
            history[(chapter, segment_index)] = TranslatedSegmentEvidence(
                chapter=chapter,
                segment=segment_index,
                source=segment.source,
                target=target,
            )

    def _progress_counts(self, store: RunStore, chapter_indices: list[int]) -> tuple[int, int]:
        """按全书批次检查点计算进度，续跑从已有译文数量开始显示。

        只有整批译文齐全时才计入 done；不完整批次会整体重跑，提前计入其中
        个别已有段会导致完成数重复累加。
        """
        total = 0
        done = 0
        for ci in chapter_indices:
            segments = store.load_chapter(ci).text_segments
            total += len(segments)
            for batch in _resume_batches(segments, self.config.segment.max_chars_per_batch):
                if all(segment.target and segment.target.strip() for segment in batch):
                    done += len(batch)
        return total, done

    # ── 全书理解预扫（源文逐章梗概 + 全书概览）────────────────────────────────
    def _build_understanding(self, store: RunStore, progress: ProgressFn | None = None) -> str:
        """翻译前预扫源文：逐章梗概存入 chapter.meta，归并出全书概览存入 analysis。

        幂等、可续跑：已有梗概/概览则跳过。返回全书概览（注入各章翻译 prompt）。
        关闭 book_understanding 时直接返回空串。
        """
        if not self.config.pipeline.book_understanding:
            store.log_event("book_understanding_skipped", reason="disabled")
            return ""
        manifest = store.load_manifest()
        chapters = manifest.get("chapters", [])

        # 各章梗概相互独立 → 并行调用（LLM 调用进线程池；落盘全部在主线程，
        # 保持原子写不竞争，且逐章增量落盘、续跑粒度不变）。已有梗概的章跳过（幂等）。
        loaded = {
            c.get("index", i): store.load_chapter(c.get("index", i)) for i, c in enumerate(chapters)
        }
        todo = [
            (ci, "\n".join(s.source for s in ch.text_segments))
            for ci, ch in loaded.items()
            if not ch.meta.get("source_digest")
        ]
        if todo:
            store.log_event(
                "book_understanding_chapter_digest_started",
                chapters=[ci for ci, _ in todo],
                workers=max(1, self.config.pipeline.prescan_concurrency),
            )
            workers = max(1, self.config.pipeline.prescan_concurrency)
            if progress:
                progress(0, len(todo), "预扫章节梗概")
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(self.synopsizer.digest_chapter, src): ci for ci, src in todo}
                for n_done, fut in enumerate(as_completed(futs), 1):
                    ci = futs[fut]
                    loaded[ci].meta["source_digest"] = fut.result()  # 失败时 _ask_text 已回退 ""
                    store.save_chapter(loaded[ci])
                    store.log_event(
                        "book_understanding_chapter_digest_saved",
                        chapter=ci,
                        digest=loaded[ci].meta["source_digest"],
                    )
                    if progress:
                        progress(n_done, len(todo), "预扫章节梗概")

        # 按 manifest 章序组装（与并发完成顺序无关）
        digests = [
            loaded[c.get("index", i)].meta.get("source_digest", "") or ""
            for i, c in enumerate(chapters)
        ]

        analysis = store.load_analysis() or {}
        synopsis = analysis.get("book_synopsis", "")
        if not synopsis and any(d.strip() for d in digests):
            if progress:
                progress(0, 0, "生成全书概览…")
            synopsis = self.synopsizer.book_synopsis(digests, self.analyzer.style_brief(analysis))
            analysis["book_synopsis"] = synopsis
            store.save_analysis(analysis)
            store.log_event("book_synopsis_saved", synopsis=synopsis)
        return synopsis

    # ── 章节标题 / 目录项翻译（书名保持原文）──────────────────────────────
    def _translate_titles(
        self,
        store: RunStore,
        glossary: GlossaryStore,
        progress: ProgressFn | None = None,
    ) -> None:
        """翻译所有逻辑章标题和 NCX/NAV 目录节点并写回 manifest。

        目录节点若已定位到正文 heading Segment，直接复用完整译文，
        使正文与目录严格一致；其它标题再分批调用标题翻译器。每批立即
        落盘，续跑只处理尚未完成的项。书名始终保持原文。
        """
        from ..agents import prompts

        m = store.load_manifest()
        chapters = m.get("chapters", [])

        # 标题压成单行，避免内嵌换行破坏 numbered 对齐
        def _flat(s: object) -> str:
            """把标题压缩为不含换行和连续空白的单行文本。"""
            return " ".join(str(s or "").split())

        raw_meta = m.get("meta")
        meta = raw_meta if isinstance(raw_meta, dict) else {}
        raw_toc_entries = meta.get("toc_entries", [])
        toc_entry_items = raw_toc_entries if isinstance(raw_toc_entries, list) else []
        toc_entries = [
            entry
            for entry in toc_entry_items
            if isinstance(entry, dict) and _flat(entry.get("title", ""))
        ]

        # 长 heading 可能在摄取后被拆成首段 + cont；按 anchor 重新并回完整
        # 译文，且只允许 heading 被目录复用。
        anchor_targets: dict[str, tuple[str, str, str]] = {}
        loaded_chapters = {
            chapter.get("index"): store.load_chapter(chapter["index"])
            for chapter in chapters
            if isinstance(chapter.get("index"), int)
        }

        def flush_anchor(
            active_anchor: str | None,
            active_kind: str,
            complete: bool,
            source_parts: list[str],
            parts: list[str],
        ) -> None:
            """把一个 anchor 的续段译文合并进索引。"""
            if active_anchor and active_kind == "heading" and complete and parts:
                anchor_targets[active_anchor] = (
                    active_kind,
                    "".join(source_parts),
                    "".join(parts),
                )

        for chapter in loaded_chapters.values():
            active_anchor: str | None = None
            active_kind = ""
            parts: list[str] = []
            source_parts: list[str] = []
            complete = True

            for segment in chapter.text_segments:
                if segment.anchor:
                    flush_anchor(
                        active_anchor,
                        active_kind,
                        complete,
                        source_parts,
                        parts,
                    )
                    active_anchor = segment.anchor
                    active_kind = segment.kind
                    parts = [segment.target] if segment.target else []
                    source_parts = [segment.source]
                    complete = bool(segment.target and segment.target.strip())
                elif segment.cont and active_anchor:
                    source_parts.append(segment.source)
                    if segment.target and segment.target.strip():
                        parts.append(segment.target)
                    else:
                        complete = False
                else:
                    flush_anchor(
                        active_anchor,
                        active_kind,
                        complete,
                        source_parts,
                        parts,
                    )
                    active_anchor = None
                    active_kind = ""
                    parts = []
                    source_parts = []
                    complete = True
            flush_anchor(
                active_anchor,
                active_kind,
                complete,
                source_parts,
                parts,
            )

        changed = False
        for entry in toc_entries:
            if entry.get("title_translated"):
                continue
            anchor = entry.get("segment_anchor")
            linked = anchor_targets.get(anchor) if isinstance(anchor, str) else None
            can_reuse = bool(linked and _flat(linked[1]) == _flat(entry.get("title")))
            target = linked[2] if linked and can_reuse else ""
            if target.strip():
                entry["title_translated"] = target.strip()
                changed = True

        entry_by_id = {
            entry.get("entry_id"): entry
            for entry in toc_entries
            if isinstance(entry.get("entry_id"), str)
        }

        def sync_chapter_titles() -> None:
            """让逻辑 Chapter 复用其起始目录节点的同一译名。"""
            nonlocal changed
            for manifest_chapter in chapters:
                if manifest_chapter.get("title_translated"):
                    continue
                entry = entry_by_id.get(manifest_chapter.get("toc_entry_id"))
                translated = entry.get("title_translated") if isinstance(entry, dict) else None
                if isinstance(translated, str) and translated.strip():
                    manifest_chapter["title_translated"] = translated.strip()
                    changed = True

        sync_chapter_titles()

        # spine 回退章没有 toc_entry_id；若章名就是首个 heading，同样复用
        # 正文译文，避免独立翻译后与页内标题不一致。
        for manifest_chapter in chapters:
            if manifest_chapter.get("title_translated"):
                continue
            chapter = loaded_chapters.get(manifest_chapter.get("index"))
            if chapter is None:
                continue
            first_heading = next(
                (segment for segment in chapter.text_segments if segment.kind == "heading"),
                None,
            )
            if (
                first_heading is not None
                and first_heading.anchor
                and _flat(first_heading.source) == _flat(manifest_chapter.get("title"))
            ):
                target = anchor_targets.get(first_heading.anchor, ("", "", ""))[2]
                if target.strip():
                    manifest_chapter["title_translated"] = target.strip()
                    changed = True

        pending: list[dict[str, object]] = []
        for entry in toc_entries:
            if not entry.get("title_translated"):
                pending.append({"record": entry, "source": _flat(entry.get("title"))})
        for chapter in chapters:
            if (
                _flat(chapter.get("title"))
                and not chapter.get("title_translated")
                and not chapter.get("toc_entry_id")
            ):
                pending.append({"record": chapter, "source": _flat(chapter.get("title"))})

        if changed:
            store.save_manifest(m)
        if not pending:
            store.log_event("titles_skipped", reason="already_translated_or_reused")
            return
        if progress:
            progress(0, len(pending), "翻译章节标题…")

        # 目录可能有数百项；同时限制项数和字符数，避免 JSON 输出被截断。
        batches: list[list[dict[str, object]]] = []
        current: list[dict[str, object]] = []
        current_chars = 0
        for item in pending:
            source = str(item["source"])
            if current and (len(current) >= 40 or current_chars + len(source) > 4000):
                batches.append(current)
                current = []
                current_chars = 0
            current.append(item)
            current_chars += len(source)
        if current:
            batches.append(current)

        completed = 0
        glossary_text = prompts.render_glossary(glossary.all_terms())
        for batch_index, batch in enumerate(batches):
            titles = [str(item["source"]) for item in batch]
            system = prompts.render(
                "title_translator_system",
                src=self.config.source_lang,
                tgt=self.config.target_lang,
                n=len(titles),
            )
            user = prompts.render(
                "title_translator_user",
                src=self.config.source_lang,
                tgt=self.config.target_lang,
                glossary=glossary_text,
                n=len(titles),
                numbered_titles=prompts.numbered(titles),
            )
            try:
                data = self.client.complete_json(
                    [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    tier="strong",
                    stage="title_translate",
                )
            except Exception as error:
                store.log_event(
                    "titles_translation_failed",
                    batch=batch_index,
                    count=len(titles),
                    error=repr(error),
                )
                raise
            out = data.get("titles") if isinstance(data, dict) else data
            if not isinstance(out, list) or len(out) != len(titles):
                store.log_event(
                    "titles_translation_rejected",
                    batch=batch_index,
                    reason="count_mismatch",
                    expected=len(titles),
                    actual=len(out) if isinstance(out, list) else None,
                )
                raise RuntimeError(
                    "Chapter/TOC title translation returned an invalid number of items: "
                    f"expected {len(titles)}, got "
                    f"{len(out) if isinstance(out, list) else 'non-list'}"
                )
            translated = [str(title).strip() for title in out]
            for item, target in zip(batch, translated):
                record = item["record"]
                if isinstance(record, dict):
                    record["title_translated"] = target or item["source"]
            sync_chapter_titles()
            store.save_manifest(m)
            store.log_event(
                "titles_translated",
                batch=batch_index,
                titles=[
                    {"source": source, "target": target}
                    for source, target in zip(titles, translated)
                ],
            )
            completed += len(batch)
            if progress:
                progress(completed, len(pending), "翻译章节标题")

    # ── 单章 ──────────────────────────────────────────────────────────────
    @staticmethod
    def _sync_context_chapter_prefix(
        context: RollingContext,
        segments: list[Segment],
        end: int,
    ) -> None:
        """用当前章已完成前缀刷新滚动上下文尾部。

        注释逻辑段跨越批次时，最后一个续段完成后会同时定稿此前批次中的
        target。这里把这些更新同步回内存上下文，确保下一批看到的也是最终
        标点版本，而不是定位前的旧字符串。
        """
        prefix = segments[: max(0, min(end, len(segments)))]
        if not prefix or any(not (segment.target and segment.target.strip()) for segment in prefix):
            return
        targets = [segment.target or "" for segment in prefix]
        retained = min(len(targets), len(context.recent_targets))
        if retained:
            context.recent_targets[-retained:] = targets[-retained:]

    @staticmethod
    def _completed_logical_starts_in_range(
        segments: list[Segment],
        start: int,
        count: int,
    ) -> list[int]:
        """返回最后一片落在当前批次内的逻辑原段起点，保持顺序并去重。

        超长原段可能被切成首段和多个 ``cont`` 续段，且切分后的翻译批次
        可能刚好从续段开始。向前追溯到首段，才能在最后一个续段译完时立即
        合并完整 source/target 并执行一次注释定位。只在逻辑段末片属于当前
        范围时返回，避免同一组续段跨多个批次时重复处理。
        """
        if count <= 0 or not segments:
            return []
        lower = max(0, start)
        upper = min(len(segments), lower + count)
        starts: list[int] = []
        position = lower
        while position < upper:
            logical_start = position
            while logical_start > 0 and segments[logical_start].cont:
                logical_start -= 1
            logical_end = logical_start
            while logical_end + 1 < len(segments) and segments[logical_end + 1].cont:
                logical_end += 1
            if lower <= logical_end < upper:
                starts.append(logical_start)
            position = max(position + 1, logical_end + 1)
        return starts

    def _align_segment_annotation(
        self,
        ci: int,
        chapter: Chapter,
        start_position: int,
        store: RunStore,
    ) -> None:
        """串行定位一个已译完逻辑原段的 EPUB 注释链接。

        超长段会被切成一个带 anchor 的首段和若干 ``cont`` 续段；解析元数据
        只存在首段，因此必须等全部续段都有译文后再合并 source/target。中文
        标点先在该逻辑段内定稿，保证 placement 的字符偏移不会在章末失效。

        定位结果无论正常还是确定性 fallback 都会立即写回章节文件。没有注释
        或译文尚不完整时直接返回，且不会调用模型。
        """
        segments = chapter.text_segments
        if not 0 <= start_position < len(segments):
            return
        while start_position > 0 and segments[start_position].cont:
            start_position -= 1
        segment = segments[start_position]
        metadata = segment.meta.get("epub_annotations")
        if not isinstance(metadata, dict):
            return
        raw_items = metadata.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            return

        logical_segments = [segment]
        cursor = start_position + 1
        while cursor < len(segments) and segments[cursor].cont:
            logical_segments.append(segments[cursor])
            cursor += 1
        if any(not (item.target and item.target.strip()) for item in logical_segments):
            return

        target_changed = False
        if self._punctuation_enabled():
            targets = [item.target or "" for item in logical_segments]
            normalized = normalize_zh_segments(
                targets,
                [item.cont for item in logical_segments],
            )
            target_changed = normalized != targets
            for item, value in zip(logical_segments, normalized):
                item.target = value

        source = "".join(item.source for item in logical_segments)
        target = "".join(item.target or "" for item in logical_segments)
        expected_ids = {
            str(item.get("id")) for item in raw_items if isinstance(item, dict) and item.get("id")
        }
        placements = metadata.get("placements")
        placement_ids = {
            str(item.get("id"))
            for item in placements or []
            if isinstance(item, dict) and item.get("id")
        }
        if (
            metadata.get("target_digest") == target_digest(target)
            and expected_ids
            and placement_ids == expected_ids
        ):
            if target_changed:
                store.save_chapter(chapter)
            return

        items = tuple(dict(item) for item in raw_items if isinstance(item, dict))
        if not items:
            if target_changed:
                store.save_chapter(chapter)
            return
        anchor = segment.anchor or f"segment-{segment.index}"
        unit = AnnotationUnit(
            unit_id=f"ch{ci}:{anchor}",
            source=source,
            target=target,
            items=items,
        )
        if not self.config.pipeline.annotation_alignment:
            store.log_event(
                "annotation_alignment_skipped",
                chapter=ci,
                segment=segment.index,
                anchor=segment.anchor,
                unit_id=unit.unit_id,
                reason="disabled",
            )
            if target_changed:
                store.save_chapter(chapter)
            return

        try:
            result = self.annotation_aligner.align_unit(unit)
        except Exception as error:  # noqa: BLE001 - 单段失败由 writer 安全降级
            if target_changed:
                store.save_chapter(chapter)
            store.log_event(
                "annotation_alignment_failed",
                chapter=ci,
                segment=segment.index,
                anchor=segment.anchor,
                unit_id=unit.unit_id,
                error=type(error).__name__,
                detail=str(error),
            )
            return

        metadata["target_digest"] = result.target_digest
        metadata["placements"] = [dict(item) for item in result.placements]
        # 每个逻辑段完成后立即原子落盘；长书被中断时不必重新支付已完成的
        # 注释定位调用，也能在翻译尚未完成时导出查看当前效果。
        store.save_chapter(chapter)
        store.log_event(
            "annotation_alignment_completed",
            chapter=ci,
            segment=segment.index,
            anchor=segment.anchor,
            unit_id=unit.unit_id,
            annotations=len(items),
            used_fallback=result.used_fallback,
        )

    def _align_annotations_after_batch(
        self,
        ci: int,
        chapter: Chapter,
        start: int,
        count: int,
        store: RunStore,
    ) -> None:
        """按原文顺序串行处理当前批次触及且已完整翻译的注释段。"""
        segments = chapter.text_segments
        for logical_start in self._completed_logical_starts_in_range(
            segments,
            start,
            count,
        ):
            self._align_segment_annotation(ci, chapter, logical_start, store)

    @staticmethod
    def _annotation_contexts_for_segments(
        segments: list[Segment],
        registry: dict[str, Any] | None,
    ) -> list[list[dict[str, str]]]:
        """按源文偏移把书级注释原文分配给对应的实际翻译切片。

        EPUB 布局元数据只保存在一个逻辑段的首片；超长段的 ``cont``
        续片没有独立 metadata。这里使用首片记录的原始字符偏移和各切片
        累计边界，把 point 注释分给所在切片、range 注释分给所有相交
        切片。相同目标在同一切片只注入一次。
        """
        assigned: list[list[dict[str, str]]] = [[] for _ in segments]
        if not isinstance(registry, dict):
            return assigned
        raw_contexts = registry.get("contexts")
        if not isinstance(raw_contexts, dict):
            return assigned

        position = 0
        while position < len(segments):
            logical_start = position
            logical_end = logical_start + 1
            while logical_end < len(segments) and segments[logical_end].cont:
                logical_end += 1
            logical_segments = segments[logical_start:logical_end]

            boundaries: list[tuple[int, int]] = []
            cursor = 0
            for segment in logical_segments:
                end = cursor + len(segment.source)
                boundaries.append((cursor, end))
                cursor = end

            metadata = logical_segments[0].meta.get("epub_annotations")
            raw_items = metadata.get("items") if isinstance(metadata, dict) else None
            items = raw_items if isinstance(raw_items, list) else []
            source_length = metadata.get("source_length") if isinstance(metadata, dict) else None
            if items and (
                not isinstance(source_length, int)
                or isinstance(source_length, bool)
                or source_length != cursor
            ):
                position = logical_end
                continue
            seen_by_piece: list[set[str]] = [set() for _ in logical_segments]

            for raw_item in items:
                if not isinstance(raw_item, dict) or raw_item.get("relation") != "noteref":
                    continue
                target_key = raw_item.get("target_key")
                if not isinstance(target_key, str) or not target_key:
                    continue
                record = raw_contexts.get(target_key)
                if not isinstance(record, dict):
                    continue
                raw_blocks = record.get("source_blocks")
                blocks = (
                    [block for block in raw_blocks if isinstance(block, str) and block.strip()]
                    if isinstance(raw_blocks, list)
                    else []
                )
                if not blocks:
                    continue
                note = {
                    "target_key": target_key,
                    "source": "\n\n".join(blocks),
                }

                start = raw_item.get("source_start")
                end = raw_item.get("source_end")
                if (
                    not isinstance(start, int)
                    or isinstance(start, bool)
                    or not isinstance(end, int)
                    or isinstance(end, bool)
                    or not 0 <= start <= end <= cursor
                ):
                    continue

                piece_indices: list[int]
                if raw_item.get("mode") == "range" and start < end:
                    piece_indices = [
                        index
                        for index, (piece_start, piece_end) in enumerate(boundaries)
                        if start < piece_end and end > piece_start
                    ]
                else:
                    # 边界上的 point 归前片；位置 0 归首片。
                    piece_index = 0
                    if start > 0:
                        piece_index = next(
                            (
                                index
                                for index, (_piece_start, piece_end) in enumerate(boundaries)
                                if start <= piece_end
                            ),
                            len(boundaries) - 1,
                        )
                    piece_indices = [piece_index]

                for piece_index in piece_indices:
                    if target_key in seen_by_piece[piece_index]:
                        continue
                    seen_by_piece[piece_index].add(target_key)
                    assigned[logical_start + piece_index].append(note)

            position = logical_end
        return assigned

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
        """翻译、润色和抽取单章并落盘，返回更新后的完成段数。"""
        chapter = store.load_chapter(ci)
        text_segs = chapter.text_segments
        if not text_segs:
            store.set_chapter_status(ci, STATUS_DONE)
            store.log_event("chapter_skipped", chapter=ci, reason="empty")
            return done
        chapter_digest = chapter.meta.get("source_digest", "")
        annotation_contexts = self._annotation_contexts_for_segments(
            text_segs,
            annotation_context_registry,
        )

        batches = _resume_batches(text_segs, self.config.segment.max_chars_per_batch)
        chapter_done = sum(
            len(batch)
            for batch in batches
            if all(segment.target and segment.target.strip() for segment in batch)
        )
        label = self._chapter_progress_label(chapter.title, ci)
        # prepare() 的最后一个标签通常是“解析文档…”。续跑首批可能先恢复术语，
        # 若不在章首刷新，整个模型请求期间都会错误地显示成仍在解析源文件。
        _report_translation_progress(
            progress,
            chapter_done=chapter_done,
            chapter_total=len(text_segs),
            overall_done=done,
            overall_total=total,
            label=label,
        )
        glossary_checkpoints = store.completed_batch_glossary_keys(ci)
        # 章内术语快照会在每个批次术语抽取后刷新，让新确认的称呼/口癖/固定表达
        # 立即影响后续批次。glossary_scope=chapter 时仍按本章源文裁剪，避免全量表过大。
        term_snapshot = self._chapter_term_snapshot(glossary, text_segs)

        # 逐批串行：每批渲染最新上下文 → 处理 → 立即把译文并入上下文供下一批参照。
        # 不再并发，换取章内跨批的代词/术语/语气连贯。
        # 断点续跑（段/批级）：上次中断前已译完并落盘的批次，整批跳过、不重翻，只重建上下文。
        bt_samples: list[tuple[str, str]] = []
        seg_base = 0  # 当前批首段的章内段号（issue 批内下标 → 章内段号）
        for b in batches:
            batch_start = seg_base
            glossary_key = store.batch_glossary_key(batch_start, len(b))
            existing_targets = [s.target for s in b if s.target and s.target.strip()]
            if len(existing_targets) == len(b):
                # 该批上次已在原位、原上下文中译完 → 复用，重建滚动上下文后跳过
                self._align_annotations_after_batch(
                    ci,
                    chapter,
                    batch_start,
                    len(b),
                    store,
                )
                context.add_targets([s.target or "" for s in b])
                self._sync_context_chapter_prefix(
                    context,
                    text_segs,
                    batch_start + len(b),
                )
                if glossary_key in glossary_checkpoints:
                    summary = {
                        "inserted": 0,
                        "conflict": 0,
                        "unchanged": 0,
                        "updated": 0,
                        "skipped": 1,
                    }
                else:
                    summary = self._extract_batch_glossary(
                        glossary,
                        store,
                        ci,
                        batch_start,
                        b,
                        translation_history,
                        source_corpus,
                    )
                    glossary_checkpoints.add(glossary_key)
                term_snapshot = self._chapter_term_snapshot(glossary, text_segs)
                store.log_event(
                    "batch_skipped",
                    chapter=ci,
                    start_index=batch_start,
                    count=len(b),
                    reason="already_translated",
                    glossary_extraction=summary,
                    segments=[
                        {"index": seg_base + i, "source": s.source, "target": s.target}
                        for i, s in enumerate(b)
                    ],
                )
                seg_base += len(b)
                _report_translation_progress(
                    progress,
                    chapter_done=chapter_done,
                    chapter_total=len(text_segs),
                    overall_done=done,
                    overall_total=total,
                    label=label,
                )
                continue

            ctx_text = context.render(self.config.pipeline.rolling_context_segments)
            res = self._process_batch(
                b,
                term_snapshot,
                ctx_text,
                style,
                book_synopsis,
                chapter_digest,
                annotation_contexts=annotation_contexts[batch_start : batch_start + len(b)],
            )
            for s, t in zip(b, res.targets):
                s.target = t
            bt_samples.extend(res.bt_samples)
            # 增量持久化译文，下次中断从此批之后续跑。
            store.save_chapter(chapter)
            # 只处理当前批次触及的注释逻辑段。多个注释段严格按原文顺序
            # 一段一次调用；若当前批只有超长段的前半部分，则等最后一个
            # cont 续段译完后再合并定位。
            self._align_annotations_after_batch(
                ci,
                chapter,
                batch_start,
                len(b),
                store,
            )
            context.add_targets([s.target or "" for s in b])
            self._sync_context_chapter_prefix(
                context,
                text_segs,
                batch_start + len(b),
            )
            store.log_event(
                "batch_translated",
                chapter=ci,
                start_index=batch_start,
                count=len(b),
                polished=self.config.pipeline.polish,
                punctuation_normalized=self._punctuation_enabled(),
                backtranslate_sample_count=len(res.bt_samples),
                segments=[
                    {
                        "index": batch_start + i,
                        "source": s.source,
                        "target": s.target,
                    }
                    for i, s in enumerate(b)
                ],
            )
            done += len(b)
            chapter_done += len(b)
            seg_base += len(b)
            _report_translation_progress(
                progress,
                chapter_done=chapter_done,
                chapter_total=len(text_segs),
                overall_done=done,
                overall_total=total,
                label=label,
            )
            # 译文落盘后再抽取术语，避免中断时术语库领先章节产物。
            self._extract_batch_glossary(
                glossary,
                store,
                ci,
                batch_start,
                b,
                translation_history,
                source_corpus,
            )
            self._update_translation_history(translation_history, ci, batch_start, b)
            glossary_checkpoints.add(glossary_key)
            term_snapshot = self._chapter_term_snapshot(glossary, text_segs)

        # 不含注释的段落在章末统一完成标点规范化。含注释逻辑段已在其
        # 最后一个续段译完时用同一函数定稿；此处重复处理是幂等的。
        if self._punctuation_enabled():
            translated = [segment.target or "" for segment in text_segs]
            normalized_targets = normalize_zh_segments(
                translated,
                [segment.cont for segment in text_segs],
            )
            for segment, normalized in zip(text_segs, normalized_targets):
                segment.target = normalized
            # 当前章译文已在逐批处理中加入滚动上下文；同步替换其保留在尾部的
            # 部分，确保下一章看到的是最终规范化版本。
            retained = min(len(normalized_targets), len(context.recent_targets))
            if retained:
                context.recent_targets[-retained:] = normalized_targets[-retained:]
            self._update_translation_history(translation_history, ci, 0, text_segs)

        # 全章术语抽取入库：保留为兜底，捕捉跨段才能确认的称呼/口癖/固定表达。
        # 最终 Review 会在全书翻译完成后读取此时已经稳定的最终术语库。
        src_text = "\n".join(s.source for s in text_segs)
        tgt_text = "\n".join(s.target or "" for s in text_segs)
        chapter_glossary_summary = self.extractor.extract_and_store(
            glossary,
            src_text,
            tgt_text,
            ci,
            history=translation_history.values(),
            before=(ci, len(text_segs)),
            source_corpus=source_corpus,
        )
        store.log_event(
            "chapter_glossary_extracted",
            chapter=ci,
            summary=chapter_glossary_summary,
        )

        # 回译抽检
        bt_issues: list[dict] = []
        if bt_samples:
            srcs = [a for a, _ in bt_samples]
            tgts = [b for _, b in bt_samples]
            for it in self.backtrans.check(srcs, tgts):
                it["chapter"] = ci
                bt_issues.append(it)
            store.log_event(
                "chapter_backtranslation_checked",
                chapter=ci,
                sample_count=len(bt_samples),
                issue_count=len(bt_issues),
                issues=bt_issues,
            )

        chapter.meta["backtranslation_issues"] = bt_issues
        store.save_chapter_with_status(chapter, STATUS_DONE)
        store.log_event(
            "chapter_done",
            chapter=ci,
            title=chapter.title,
            segment_count=len(text_segs),
            backtranslation_issue_count=len(bt_issues),
        )
        return done

    def _chapter_term_snapshot(self, glossary: GlossaryStore, text_segs) -> list:
        """返回当前章节要注入的术语快照；实时入库后可重新调用刷新。"""
        terms = glossary.all_terms()
        if self.config.pipeline.glossary_scope != "chapter":
            return terms
        src_text = "\n".join(s.source for s in text_segs)
        hit = {t.source for t in GlossaryStore.terms_in(terms, src_text)}
        return [t for t in terms if t.source in hit]

    @staticmethod
    def _chapter_progress_label(title: str, index: int) -> str:
        """进度展示用章节名：优先用书内标题，避免内部序号与“第一章”等标题冲突。"""
        title = (title or "").strip()
        return title or f"章节 {index + 1}"

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
        """每批译完/续跑跳过后即时抽取术语，供同章后续批次使用。"""
        src_text = "\n".join(s.source for s in batch)
        tgt_text = "\n".join(s.target or "" for s in batch)
        summary = self.extractor.extract_and_store(
            glossary,
            src_text,
            tgt_text,
            chapter,
            history=translation_history.values(),
            before=(chapter, start_index),
            source_corpus=source_corpus,
        )
        store.log_event(
            "batch_glossary_extracted",
            chapter=chapter,
            start_index=start_index,
            count=len(batch),
            summary=summary,
        )
        return summary

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
            final_conflicts = _review_unresolved_conflict_records(unresolved)
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
        """把一章切成连续块并行审校，返回映射到章内段号的问题。

        块 = 连续段序列（约 3 倍翻译批大小，减少调用次数与重复注入的输入 token）；
        块内 reviewer 返回的 index 是块内下标，加块首段偏移映射回章内段号；
        越界 index 直接丢弃（模型幻觉防御）。各块只读固定译文和术语快照，
        可并行调用；结构化输出畸形时递归拆半，单段按配置有限重试；
        结果始终按原块顺序合并，保持确定性。
        """
        budget = self.config.segment.max_chars_per_batch * 3
        chunks = self._pack_contiguous(text_segs, budget)
        if not chunks:
            return []

        jobs: list[tuple[int, list]] = []
        base = 0
        for chunk in chunks:
            jobs.append((base, chunk))
            base += len(chunk)

        recovery_events: list[dict[str, Any]] = []
        recovery_lock = Lock()

        def record_recovery(event: str, **data: Any) -> None:
            """线程安全地暂存恢复事件，待并行任务结束后由主线程写日志。"""
            with recovery_lock:
                recovery_events.append({"event": event, **data})

        def review_once(chunk_base: int, chunk: list, *, attempt: int = 1) -> list[dict]:
            """调用一次审校，并把合法块内索引映射为章内索引。"""
            srcs = [s.source for s in chunk]
            overrides = target_overrides or {}

            def target_for(local_index: int, segment) -> str:
                """读取本轮影子译文；无章位置时回退正式译文。"""
                if chapter_index is None:
                    return segment.target or ""
                return overrides.get(
                    (chapter_index, chunk_base + local_index),
                    segment.target or "",
                )

            tgts = [target_for(local_index, segment) for local_index, segment in enumerate(chunk)]
            local_issues: list[dict] = []
            initial_trace: dict[str, Any] | None = None
            initial_path = ""
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
                """逐步保存初审完整请求、原始响应或服务错误。"""
                if debug is None or initial_trace is None:
                    return
                initial_trace[event] = data
                debug.write_json(initial_path, initial_trace)

            try:
                review_result = self.reviewer.review_result(
                    srcs,
                    tgts,
                    terms,
                    trace=trace if debug is not None else None,
                )
            except Exception as error:
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
            for it in review_result.issues:
                it = dict(it)
                idx = it.get("index")
                if isinstance(idx, int) and not isinstance(idx, bool) and 0 <= idx < len(chunk):
                    it["index"] = idx
                    local_issues.append(it)
                else:
                    raise ReviewOutputError("invalid_issue_index")
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

            dismissed: list[dict[str, Any]] = []
            fallback_reason = ""
            if (
                local_issues
                and evidence is not None
                and debug is not None
                and self.config.pipeline.review_agent_loop
                and chapter_index is not None
            ):
                outcome = ReviewAgentLoop(
                    self.client,
                    self.config,
                    evidence,
                    debug,
                ).review_chunk(
                    chapter=chapter_index,
                    chunk_base=chunk_base,
                    sources=srcs,
                    targets=tgts,
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
                    issue = dict(issue)
                    issue["index"] = chunk_base + local_index
                    issue["_chunk_id"] = chunk_id
                    if fallback_reason:
                        issue["fallback_reason"] = fallback_reason
                    mapped.append(issue)
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

        def review_adaptive(chunk_base: int, chunk: list) -> list[dict]:
            """畸形输出时缩小请求；单段仍失败才进行有限同输入重试。"""
            try:
                return review_once(chunk_base, chunk)
            except ReviewOutputError as error:
                if len(chunk) > 1:
                    mid = len(chunk) // 2
                    record_recovery(
                        "review_chunk_split",
                        start_index=chunk_base,
                        count=len(chunk),
                        left_count=mid,
                        right_count=len(chunk) - mid,
                        reason=error.reason,
                    )
                    return review_adaptive(chunk_base, chunk[:mid]) + review_adaptive(
                        chunk_base + mid, chunk[mid:]
                    )

                last_error = error
                retries = self.config.pipeline.review_output_retries
                for attempt in range(1, retries + 1):
                    record_recovery(
                        "review_singleton_retry",
                        start_index=chunk_base,
                        count=1,
                        attempt=attempt,
                        max_retries=retries,
                        reason=last_error.reason,
                    )
                    try:
                        result = review_once(chunk_base, chunk, attempt=attempt + 1)
                    except ReviewOutputError as retry_error:
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
                    reason=last_error.reason,
                )
                raise last_error

        def review_one(job: tuple[int, list]) -> list[dict]:
            """审校一个初始连续块，并在必要时执行局部恢复。"""
            chunk_base, chunk = job
            return review_adaptive(chunk_base, chunk)

        workers = min(
            max(1, self.config.pipeline.review_concurrency),
            len(jobs),
        )
        try:
            if workers == 1:
                results = []
                for job in jobs:
                    results.append(review_one(job))
                    if on_chunk_finished:
                        on_chunk_finished(len(job[1]))
            else:
                ordered_results: list[list[dict] | None] = [None] * len(jobs)
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    futures = {
                        ex.submit(review_one, job): (position, len(job[1]))
                        for position, job in enumerate(jobs)
                    }
                    for future in as_completed(futures):
                        position, segment_count = futures[future]
                        ordered_results[position] = future.result()
                        if on_chunk_finished:
                            on_chunk_finished(segment_count)
                results = [result for result in ordered_results if result is not None]
        finally:
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

    @staticmethod
    def _pack_contiguous(segs: list[Segment], budget: int) -> list[list[Segment]]:
        """按源文字符预算把段保序打包成若干连续块。"""
        return [
            segs[plan.start_index : plan.stop_index]
            for plan in plan_contiguous_batches(segs, budget)
        ]

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
        """单个批次：整批翻译 → 润色。

        每段都在自身上下文里翻译，不跨位置复用译文（避免丢失语境信息）。
        全书概览/本章梗概作为恒定前缀注入，让译者把握全局。
        标点规范化在章末统一执行，以维持跨段引号状态。
        LLM 审校不在翻译批内做；全书完成后由独立 Review 阶段统一执行。
        """
        sources = [s.source for s in batch]
        targets = self.translator.translate_batch(
            sources,
            glossary_terms=terms,
            style=style,
            context=ctx_text,
            book_synopsis=book_synopsis,
            chapter_digest=chapter_digest,
            annotation_contexts=annotation_contexts,
        )

        if self.config.pipeline.polish:
            polished = self.polisher.polish(targets, glossary_terms=terms, style=style)
            if len(polished) == len(targets):
                targets = polished

        bt_samples: list[tuple[str, str]] = []
        rate = self.config.pipeline.backtranslate_sample
        if rate > 0:
            for s, t in zip(sources, targets):
                if random.random() < rate:
                    bt_samples.append((s, t or ""))

        return _BatchResult(targets=targets, bt_samples=bt_samples)

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

        if progress:
            progress(0, 0, "回填译文…")
        out_cfg = self.config.output
        do_mono, do_bilingual = out_cfg.mono, out_cfg.bilingual
        if not do_mono and not do_bilingual:
            do_mono = True

        outputs: list[str] = []
        if do_mono:
            outputs.append(
                self._measure_stage_call(
                    "assemble",
                    assemble,
                    store,
                    input_path,
                    out_path=out_path,
                    out_format=out_format,
                    bilingual=False,
                    about_page=out_cfg.about_page,
                    pdf_engine=pdf_engine,
                )
            )
        if do_bilingual:
            bi_out_path = bilingual_out_path(out_path) if out_path else None
            outputs.append(
                self._measure_stage_call(
                    "assemble",
                    assemble,
                    store,
                    input_path,
                    out_path=bi_out_path,
                    out_format=out_format,
                    bilingual=True,
                    order=out_cfg.bilingual_order,
                    preserve_source_style=out_cfg.bilingual_preserve_source_style,
                    about_page=out_cfg.about_page,
                    pdf_engine=pdf_engine,
                )
            )
        return outputs

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
