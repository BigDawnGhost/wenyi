"""编排器：驱动全流程，章级状态机 + 断点续跑。

存储无关（tech-stack §7）：编排器通过注入的 :class:`wenyi_core.storage.Storage`
读写全部状态（运行态 + 术语库），不直接接触 RunStore/GlossaryStore/Postgres。
未注入时（CLI/测试）自动构造 :class:`FileStorage`，行为与升级前一致。

单章流水线（章内批次**串行**，逐批刷新滚动上下文与术语快照；跨章亦串行传递梗概）：
  每批：渲染上下文（含前一批刚译出的译文）→ 翻译（对齐保证）→ 润色（可选）→
        术语/称呼/固定表达实时抽取入库 → 立即供下一批参照。
  章末：跨段标点规范化 → 全章术语兜底抽取 → 回译抽检 → 写 TM → 落盘标记 done。
翻译前先预扫源文建立全书理解（逐章梗概+全书概览，fast 档并行），作恒定前缀注入每章翻译。

全书翻译完成后，独立 Review 阶段使用最终术语库按章审校；章内连续块并行检测，
严重项再按书序串行定向重译。run_all 随后执行一致性 QA、报告和导出。
进度回调 progress(done_segments, total_segments, label) 与 UI 无关，每批完成即触发。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..agents.analyzer import Analyzer
from ..agents.polisher import Polisher
from ..agents.reviewer import BackTranslator, Reviewer
from ..agents.synopsis import Synopsizer
from ..agents.translator import Translator
from ..config import Config
from ..glossary.extractor import GlossaryExtractor
from ..glossary.store import TYPE_PERSON
from ..ingest.segmenter import batch_segments, load_document
from ..llm.base import LLMClient
from ..llm.factory import build_client
from ..llm.usage import merge_usage_summaries, usage_delta
from ..pipeline.runstore import slugify
from ..postprocess.punct import normalize_zh, normalize_zh_segments
from ..storage import STATUS_DONE, FileStorage, Storage
from . import checks
from .context import RollingContext
from .runstore import (
    REVIEW_DONE,
    REVIEW_FAILED,
    REVIEW_PENDING,
    REVIEW_RUNNING,
    RunStore,
    STATUS_DONE,
    slugify,
)

ProgressFn = Callable[[int, int, str], None]


# 语言名/代码 → ISO 639-1 两字母代码（模型检测结果归一化）
_LANG_ALIASES = {
    "japanese": "ja", "日语": "ja", "日文": "ja", "jp": "ja", "jpn": "ja",
    "english": "en", "英语": "en", "英文": "en", "eng": "en",
    "russian": "ru", "俄语": "ru", "俄文": "ru", "rus": "ru",
    "chinese": "zh", "中文": "zh", "汉语": "zh", "zh-cn": "zh", "zho": "zh",
    "korean": "ko", "韩语": "ko", "韩文": "ko", "kor": "ko",
    "french": "fr", "法语": "fr", "法文": "fr",
    "german": "de", "德语": "de", "德文": "de",
    "spanish": "es", "西班牙语": "es", "西班牙文": "es",
    "italian": "it", "意大利语": "it", "意大利文": "it",
    "portuguese": "pt", "葡萄牙语": "pt", "葡萄牙文": "pt",
}


def _normalize_lang(code: str) -> str:
    """把模型返回的语言名或别名规整为 ISO 639-1 两字母代码。"""
    c = (code or "").strip().lower()
    if not c or c in {"auto", "unknown", "und", "uncertain", "mixed", "多语言", "未知"}:
        return ""
    if c in _LANG_ALIASES:
        return _LANG_ALIASES[c]
    return c[:2] if c[:2].isalpha() else ""


def _resume_batches(segments, max_chars: int) -> list[list]:
    """按字符预算分批后，再沿“已完成/待翻译”边界切开。

    用户调整批次预算时，新的批次可能同时包含已有译文和空译文。若直接重跑
    该混合批次会覆盖已确认内容；按完成状态分组可只补译缺失段。
    """
    batches: list[list] = []
    for raw_batch in batch_segments(segments, max_chars):
        current: list = []
        current_done: bool | None = None
        for segment in raw_batch:
            done = bool(segment.target and segment.target.strip())
            if current and done != current_done:
                batches.append(current)
                current = []
            current.append(segment)
            current_done = done
        if current:
            batches.append(current)
    return batches


@dataclass
class _BatchResult:
    targets: list[str]
    bt_samples: list[tuple[str, str]] = field(default_factory=list)


class Orchestrator:
    def __init__(self, config: Config, client: LLMClient | None = None,
                 *, storage: Storage | None = None):
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
        # 存储注入：未注入时由 prepare() 按输入文档自动构造 FileStorage（CLI/测试）。
        self.storage: Storage | None = storage
        self._owns_storage = storage is None  # 自动构造的存储由本对象负责关闭

    def _storage_lock(self):
        """书级锁：串行化同一本书的跨进程写入。

        存储实现暴露 ``lock()`` 时启用（如文件后端封装的 RunStore）；否则退化为
        空上下文（如 Postgres 由事务/行锁保证一致性）。
        """
        lock = getattr(self.storage, "lock", None)
        if callable(lock):
            return lock()
        return nullcontext()

    def _punctuation_enabled(self) -> bool:
        """判断当前目标语言是否应启用中文标点规范化。"""
        target = (self.config.target_lang or "").lower().replace("_", "-")
        return self.config.punctuation_normalize and (
            target == "zh" or target.startswith("zh-")
        )

    def _flush_usage(self, *, scope: str) -> dict[str, Any]:
        """把当前 client 尚未落盘的用量增量合并到本书 usage 记录。"""
        current = self.client.usage_summary()
        increment = usage_delta(current, self._usage_checkpoint)
        self._usage_checkpoint = current
        storage = self.storage
        accumulated = storage.load_usage() or {
            "totals": {},
            "by_tier": {},
            "by_stage": {},
        }
        if not increment["totals"]["calls"]:
            return merge_usage_summaries(accumulated, increment)
        cumulative = merge_usage_summaries(accumulated, increment)
        storage.save_usage(cumulative)
        storage.log_event(
            "usage_summary",
            scope=scope,
            increment=increment,
            cumulative=cumulative,
        )
        return cumulative

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
        for ag in (self.analyzer, self.synopsizer, self.translator, self.reviewer,
                   self.backtrans, self.polisher, self.extractor):
            ag.src = resolved

    # ── 准备 / 续跑入口 ──────────────────────────────────────────────────
    def _source_cache_dir(self) -> str | None:
        """PDF 转换缓存目录：state_dir/<slug>/source（仅文件后端可用）。

        存储实现暴露 ``run_dir`` 时以其下的 ``source/`` 为缓存目录；否则返回
        None（注入式后端如 Postgres 不缓存转换产物）。
        """
        run_dir = getattr(self.storage, "run_dir", None)
        return os.path.join(run_dir, "source") if run_dir else None

    def _locate_existing_store(
        self,
        input_path: str,
        *,
        progress: Optional[ProgressFn] = None,
    ) -> Storage:
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

        # 存储注入：未注入时自动构造 FileStorage
        if self.storage is None:
            run_dir = os.path.join(self.config.state_dir, slugify(title))
            self.storage = FileStorage(run_dir)

        if not self.storage.exists():
            raise ValueError("尚无翻译进度。请先运行 translate。")
        return self.storage

    def prepare(self, input_path: str, *,
                progress: Optional[ProgressFn] = None) -> Storage:
        """解析输入并定位状态目录；首次运行时在书级锁内完成初始化。

        PDF 的状态目录可直接由文件名确定，因此续跑时先检查已有状态，
        避免重新调用外部转换服务；首次转换产生的 HTML 缓存在该状态目录中。
        """
        is_pdf = os.path.splitext(input_path)[1].lower() == ".pdf"

        # PDF 的书名固定取文件名，首次解析前即可确定状态目录（CLI/测试自动构造 FileStorage）。
        if self.storage is None and is_pdf:
            pdf_title = os.path.splitext(os.path.basename(input_path))[0]
            run_dir = os.path.join(self.config.state_dir, slugify(pdf_title))
            self.storage = FileStorage(run_dir)

        # 已确定状态目录且已有进度 → 直接续跑，不重新解析/转换（语言在 run() 里按 manifest 应用）
        if self.storage is not None and self.storage.exists():
            self.storage.log_event("run_resumed", input_path=input_path)
            return self.storage

        if progress:
            progress(0, 0, "解析文档…")
        # 超长段按句拆分（max_chars_per_segment），续段标 cont 供回填并回。
        # PDF 首次转换产生的 HTML 缓存在状态目录 source/ 下，续跑复用。
        cache_dir = self._source_cache_dir() if is_pdf else None
        doc = load_document(
            input_path,
            self.config.source_lang,
            self.config.target_lang,
            split_segments=self.config.segment.max_chars_per_segment,
            cache_dir=cache_dir,
        )

        # 存储注入：Web 模式由调用方传入 PostgresStorage；CLI/测试自动构造 FileStorage。
        if self.storage is None:
            run_dir = os.path.join(self.config.state_dir, slugify(doc.title))
            self.storage = FileStorage(run_dir)
        storage = self.storage

        if storage.exists():
            storage.log_event("run_resumed", input_path=input_path)
            return storage  # 已有进度 → 直接续跑，不重置

        with self._storage_lock():
            return self._prepare_new(doc, input_path, progress)

    def _prepare_new(self, doc, input_path: str,
                     progress: Optional[ProgressFn]) -> Storage:
        """新运行初始化：分阶段写入，manifest 最后落盘作为完成标志。"""
        storage = self.storage
        # 新建：auto 时只使用模型检测主要语言；失败则要求用户显式指定。
        if self.config.source_lang in ("auto", "", None):
            if progress:
                progress(0, 0, "识别语言…")
            detected = self._detect_language_ai(doc)
            if not detected:
                storage.log_event("language_detection_failed", source_lang=doc.source_lang)
                raise RuntimeError(
                    "自动识别源语言失败：请检查模型配置，或在 config.yaml 的 "
                    "language.source 指定 ISO 639-1 语言代码（如 ja/en/ko/ru/fr/de/es）。"
                )
            doc.source_lang = detected
            storage.log_event("language_detected", source_lang=doc.source_lang)
        self._apply_language(doc.source_lang)

        # Prefer deferred-manifest staging when available; else init immediately.
        stage = getattr(storage, "stage_document", None)
        if callable(stage):
            manifest = stage(doc)
        else:
            manifest = storage.init_from_document(doc)

        if progress:
            progress(0, 0, "分析全书风格…")
        sample = self._sample_text(doc)
        analysis = self.analyzer.analyze(sample) if sample else {}
        if analysis:
            self.analyzer.seed_glossary(storage, analysis)
        storage.save_analysis(analysis)
        storage.log_event("analysis_saved", has_analysis=bool(analysis))
        storage.save_context(
            RollingContext(
                max_recent_keep=max(40, self.config.pipeline.rolling_context_segments)
            ).to_dict()
        )

        # manifest 是初始化完成标志，必须最后原子落盘（staging 路径）。
        if callable(stage):
            manifest["initialized"] = True
            storage.save_manifest(manifest)

        storage.log_event(
            "run_initialized",
            input_path=input_path,
            title=doc.title,
            fmt=doc.fmt,
            source_lang=doc.source_lang,
            target_lang=doc.target_lang,
            chapters=len(doc.chapters),
            config={
                "review": self.config.pipeline.review,
                "autofix_severe": self.config.pipeline.autofix_severe,
                "polish": self.config.pipeline.polish,
                "backtranslate_sample": self.config.pipeline.backtranslate_sample,
                "consistency_qa": self.config.pipeline.consistency_qa,
                "book_understanding": self.config.pipeline.book_understanding,
                "review_concurrency": self.config.pipeline.review_concurrency,
            },
        )
        return storage

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
                [{"role": "system", "content": system},
                 {"role": "user", "content": sample}], tier="cheap",
                stage="language_detect")
            code = (data.get("language") if isinstance(data, dict) else "") or ""
            return _normalize_lang(str(code))
        except Exception:
            return ""

    @staticmethod
    def _sample_text(doc, *, labeled: bool = True) -> str:
        """取风格分析样章。labeled=True 时多点采样（开头/中部/结尾各一段，带中文标注），
        让分析覆盖全书风格全貌；labeled=False 返回单段纯源文（语言检测用，不能混入中文标签）。"""
        texts = ["\n".join(s.source for s in ch.text_segments) for ch in doc.chapters]
        texts = [t for t in texts if len(t) > 200]
        if not texts:  # 兜底：全书都是短章
            joined = "\n".join(
                s.source for ch in doc.chapters[:2] for s in ch.text_segments)
            return joined[:6000]
        if not labeled:
            return texts[0][:6000]
        picks = [(0, "开头样章"), (len(texts) // 2, "中部样章"), (len(texts) - 1, "结尾样章")]
        parts: list[str] = []
        seen: set[int] = set()
        for idx, tag in picks:
            if idx in seen:  # 短书（1-2 章）去重，不重复取同一章
                continue
            seen.add(idx)
            t = texts[idx]
            chunk = t[-2800:] if tag == "结尾样章" else t[:2800]
            parts.append(f"【{tag}】\n{chunk}")
        return "\n\n".join(parts)

    def run(self, input_path: str, *, only_chapter: int | None = None,
            progress: Optional[ProgressFn] = None) -> Storage:
        """准备运行状态并在书级锁内翻译待处理章节。"""
        storage = self.prepare(input_path, progress=progress)
        with self._storage_lock():
            manifest = storage.load_manifest()
            self._apply_language(manifest.get("source_lang") or self.config.source_lang)
            # 章节编号校验：--chapter 越界立即报错，避免静默空跑。
            chapter_indices = {
                chapter.get("index") for chapter in manifest.get("chapters", [])
            }
            if only_chapter is not None and only_chapter not in chapter_indices:
                available = sorted(
                    index for index in chapter_indices if isinstance(index, int)
                )
                valid_range = f"0–{available[-1]}" if available else "无可翻译章节"
                raise ValueError(
                    f"章节编号 {only_chapter} 不存在；可用范围：{valid_range}"
                )
            context = RollingContext.from_dict(
                storage.load_context() or {},
                min_recent_keep=max(40, self.config.pipeline.rolling_context_segments),
            )
            )
            style = self.analyzer.style_brief(storage.load_analysis() or {})
            # 翻译前预扫源文，建立全书理解（幂等、可续跑）；全书概览注入每章翻译
            book_synopsis = self._build_understanding(progress=progress)

            if only_chapter is not None:
                targets = [only_chapter]
                progress_chapters = targets
            else:
                targets = storage.pending_chapters()
                progress_chapters = [
                    chapter["index"] for chapter in manifest.get("chapters", [])
                ]

            total, done = self._progress_counts(progress_chapters)
            storage.log_event(
                "translate_run_started",
                only_chapter=only_chapter,
                chapters=targets,
                total_segments=total,
            )
            try:
                for ci in targets:
                    done = self._translate_chapter(
                        ci, context, style, book_synopsis,
                        progress=progress, done=done, total=total)
                    storage.save_context(context.to_dict())
                    self._flush_usage(scope="chapter")
                # 全书译完后翻译各章标题和目录项（书名保持原文，借术语表保持专名一致）
                if not storage.pending_chapters():
                    self._translate_titles(progress=progress)
            finally:
                self._flush_usage(scope="translate")
                if self._owns_storage:
                    storage.close()
            if progress and total:
                progress(total, total, "翻译完成")
            storage.log_event("translate_run_finished", total_segments=total)
            return storage

    def _progress_counts(self, chapter_indices: list[int]) -> tuple[int, int]:
        """按全书批次检查点计算进度，续跑从已有译文数量开始显示。

        只有整批译文齐全时才计入 done；不完整批次会整体重跑，提前计入其中
        个别已有段会导致完成数重复累加。
        """
        total = 0
        done = 0
        for ci in chapter_indices:
            segments = self.storage.load_chapter(ci).text_segments
            total += len(segments)
            for batch in _resume_batches(
                segments, self.config.segment.max_chars_per_batch
            ):
                if all(segment.target and segment.target.strip()
                       for segment in batch):
                    done += len(batch)
        return total, done

    # ── 全书理解预扫（源文逐章梗概 + 全书概览）────────────────────────────────
    def _build_understanding(self,
                             progress: Optional[ProgressFn] = None) -> str:
        """翻译前预扫源文：逐章梗概存入 chapter.meta，归并出全书概览存入 analysis。

        幂等、可续跑：已有梗概/概览则跳过。返回全书概览（注入各章翻译 prompt）。
        关闭 book_understanding 时直接返回空串。
        """
        storage = self.storage
        if not self.config.pipeline.book_understanding:
            storage.log_event("book_understanding_skipped", reason="disabled")
            return ""
        manifest = storage.load_manifest()
        chapters = manifest.get("chapters", [])

        # 各章梗概相互独立 → 并行调用（LLM 调用进线程池；落盘全部在主线程，
        # 保持原子写不竞争，且逐章增量落盘、续跑粒度不变）。已有梗概的章跳过（幂等）。
        loaded = {c.get("index", i): storage.load_chapter(c.get("index", i))
                  for i, c in enumerate(chapters)}
        todo = [(ci, "\n".join(s.source for s in ch.text_segments))
                for ci, ch in loaded.items() if not ch.meta.get("source_digest")]
        if todo:
            storage.log_event(
                "book_understanding_chapter_digest_started",
                chapters=[ci for ci, _ in todo],
                workers=max(1, self.config.pipeline.prescan_concurrency),
            )
            workers = max(1, self.config.pipeline.prescan_concurrency)
            if progress:
                progress(0, len(todo), "预扫章节梗概")
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(self.synopsizer.digest_chapter, src): ci
                        for ci, src in todo}
                for n_done, fut in enumerate(as_completed(futs), 1):
                    ci = futs[fut]
                    loaded[ci].meta["source_digest"] = fut.result()  # 失败时 _ask_text 已回退 ""
                    storage.save_chapter(loaded[ci])
                    storage.log_event(
                        "book_understanding_chapter_digest_saved",
                        chapter=ci,
                        digest=loaded[ci].meta["source_digest"],
                    )
                    if progress:
                        progress(n_done, len(todo), "预扫章节梗概")

        # 按 manifest 章序组装（与并发完成顺序无关）
        digests = [loaded[c.get("index", i)].meta.get("source_digest", "") or ""
                   for i, c in enumerate(chapters)]

        analysis = storage.load_analysis() or {}
        synopsis = analysis.get("book_synopsis", "")
        if not synopsis and any(d.strip() for d in digests):
            if progress:
                progress(0, 0, "生成全书概览…")
            synopsis = self.synopsizer.book_synopsis(
                digests, self.analyzer.style_brief(analysis))
            analysis["book_synopsis"] = synopsis
            storage.save_analysis(analysis)
            storage.log_event("book_synopsis_saved", synopsis=synopsis)
        return synopsis

    # ── 章节标题 / 目录项翻译（书名保持原文）──────────────────────────────
    def _translate_titles(self, progress: Optional[ProgressFn] = None) -> None:
        """翻译所有逻辑章标题和 NCX/NAV 目录节点并写回 manifest。

        目录节点若已定位到正文 heading Segment，直接复用完整译文，
        使正文与目录严格一致；其它标题再分批调用标题翻译器。每批立即
        落盘，续跑只处理尚未完成的项。书名始终保持原文。
        """
        from ..agents import prompts

        storage = self.storage
        m = storage.load_manifest()
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
            chapter.get("index"): storage.load_chapter(chapter["index"])
            for chapter in chapters
            if isinstance(chapter.get("index"), int)
        }
        for chapter in loaded_chapters.values():
            active_anchor: str | None = None
            active_kind = ""
            parts: list[str] = []
            source_parts: list[str] = []
            complete = True

            def flush_anchor() -> None:
                """把当前 anchor 的续段译文合并进索引。"""
                if active_anchor and active_kind == "heading" and complete and parts:
                    anchor_targets[active_anchor] = (
                        active_kind,
                        "".join(source_parts),
                        "".join(parts),
                    )

            for segment in chapter.text_segments:
                if segment.anchor:
                    flush_anchor()
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
                    flush_anchor()
                    active_anchor = None
                    active_kind = ""
                    parts = []
                    source_parts = []
                    complete = True
            flush_anchor()

        changed = False
        for entry in toc_entries:
            if entry.get("title_translated"):
                continue
            anchor = entry.get("segment_anchor")
            linked = anchor_targets.get(anchor) if isinstance(anchor, str) else None
            can_reuse = bool(
                linked and _flat(linked[1]) == _flat(entry.get("title"))
            )
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
            storage.save_manifest(m)
        if not pending:
            storage.log_event("titles_skipped", reason="already_translated_or_reused")
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
        glossary_text = prompts.render_glossary(storage.all_terms())
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
                storage.log_event(
                    "titles_translation_failed",
                    batch=batch_index,
                    count=len(titles),
                    error=repr(error),
                )
                raise
            out = data.get("titles") if isinstance(data, dict) else data
            if not isinstance(out, list) or len(out) != len(titles):
                storage.log_event(
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
            storage.save_manifest(m)
            storage.log_event(
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
    def _translate_chapter(self, ci: int, context: RollingContext,
                           style: str, book_synopsis: str = "", *,
                           progress: Optional[ProgressFn] = None,
                           done: int = 0, total: int = 0) -> int:
        """翻译、润色、抽取、审校并落盘单章，返回更新后的完成段数。"""
        storage = self.storage
        chapter = storage.load_chapter(ci)
        text_segs = chapter.text_segments
        if not text_segs:
            storage.set_chapter_status(ci, STATUS_DONE)
            storage.log_event("chapter_skipped", chapter=ci, reason="empty")
            return done
        chapter_digest = chapter.meta.get("source_digest", "")

        batches = _resume_batches(
            text_segs, self.config.segment.max_chars_per_batch
        )
        label = self._chapter_progress_label(chapter.title, ci)
        # prepare() 的最后一个标签通常是“解析文档…”。续跑首批可能先恢复术语，
        # 若不在章首刷新，整个模型请求期间都会错误地显示成仍在解析源文件。
        if progress:
            progress(done, total, label)
        glossary_checkpoints = self._completed_batch_glossary_keys(ci)
        # 章内术语快照会在每个批次术语抽取后刷新，让新确认的称呼/口癖/固定表达
        # 立即影响后续批次。glossary_scope=chapter 时仍按本章源文裁剪，避免全量表过大。
        term_snapshot = self._chapter_term_snapshot(text_segs)

        # 逐批串行：每批渲染最新上下文 → 处理 → 立即把译文并入上下文供下一批参照。
        # 不再并发，换取章内跨批的代词/术语/语气连贯。
        # 断点续跑（段/批级）：上次中断前已译完并落盘的批次，整批跳过、不重翻，只重建上下文。
        bt_samples: list[tuple[str, str]] = []
        translation_changed = False
        seg_base = 0   # 当前批首段的章内段号（issue 批内下标 → 章内段号）
        for b in batches:
            batch_start = seg_base
            glossary_key = self._batch_glossary_key(batch_start, len(b))
            existing_targets = [s.target for s in b if s.target and s.target.strip()]
            if len(existing_targets) == len(b):
                # 该批上次已在原位、原上下文中译完 → 复用，重建滚动上下文后跳过
                context.add_targets(existing_targets)
                if glossary_key in glossary_checkpoints:
                    summary = {"inserted": 0, "conflict": 0, "unchanged": 0,
                               "updated": 0, "skipped": 1}
                else:
                    summary = self._extract_batch_glossary(ci, batch_start, b)
                    glossary_checkpoints.add(glossary_key)
                term_snapshot = self._chapter_term_snapshot(text_segs)
                storage.log_event(
                    "batch_skipped",
                    chapter=ci,
                    start_index=batch_start,
                    count=len(b),
                    reason="already_translated",
                    glossary_extraction=summary,
                    segments=[
                        {"index": batch_start + i, "source": s.source, "target": s.target}
                        for i, s in enumerate(b)
                    ],
                )
                seg_base += len(b)
                if progress:
                    progress(done, total, label)
                continue

            ctx_text = context.render(self.config.pipeline.rolling_context_segments)
            if not translation_changed:
                # 新译文会使旧审校结果失效；最终 Review 阶段将重新生成。
                chapter.meta["review_issues"] = []
                chapter.meta.pop("review_digest", None)
                translation_changed = True
            res = self._process_batch(b, term_snapshot, ctx_text, style,
                                      book_synopsis, chapter_digest)
            for s, t in zip(b, res.targets):
                s.target = t
            storage.log_event(
                "batch_translated",
                chapter=ci,
                start_index=batch_start,
                count=len(b),
                polished=self.config.pipeline.polish,
                punctuation_normalized=self._punctuation_enabled(),
                backtranslate_sample_count=len(res.bt_samples),
                segments=[
                    {"index": batch_start + i, "source": s.source, "target": t}
                    for i, (s, t) in enumerate(zip(b, res.targets))
                ],
            )
            context.add_targets(res.targets)
            bt_samples.extend(res.bt_samples)
            done += len(b)
            seg_base += len(b)
            # 增量持久化：本批译文 + 累计问题落盘，下次中断从此批之后续跑
            storage.save_chapter(chapter)
            # 译文落盘后再抽取术语，避免中断时术语库领先章节产物。
            self._extract_batch_glossary(ci, batch_start, b)
            glossary_checkpoints.add(glossary_key)
            term_snapshot = self._chapter_term_snapshot(text_segs)
            # 进度回调可能用于触发暂停；必须在批次与术语均落盘后调用，
            # 确保暂停/异常不会丢弃刚完成的 LLM 结果。
            if progress:
                progress(done, total, label)

        # 标点在章级统一处理，直引号状态才能跨批次、跨段保持连续。
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

        # 全章术语抽取入库：保留为兜底，捕捉跨段才能确认的称呼/口癖/固定表达。
        # 最终 Review 会在全书翻译完成后读取此时已经稳定的最终术语库。
        src_text = "\n".join(s.source for s in text_segs)
        tgt_text = "\n".join(s.target or "" for s in text_segs)
        self.extractor.extract_and_store(storage, src_text, tgt_text, ci)
        term_snapshot = self._chapter_term_snapshot(text_segs)
        storage.log_event("chapter_glossary_extracted", chapter=ci)

        # ── 章末整章审校（移出批内关键路径；块内 index 映射回章内段号）──
        # 幂等：续跑重入章末时清掉旧审校项，防重复累积。
        if self.config.pipeline.review:
            review_issues = []
            new_issues = self._review_chapter(text_segs, term_snapshot)
            storage.log_event(
                "chapter_reviewed",
                chapter=ci,
                issue_count=len(new_issues),
                issues=new_issues,
            )
            if self.config.pipeline.autofix_severe:
                self._autofix_severe(text_segs, new_issues, term_snapshot, style,
                                     book_synopsis, chapter_digest,
                                     chapter_index=ci)
            for it in new_issues:
                it["chapter"] = ci
                it.setdefault("fixed", False)
                it["stage"] = "review"
            review_issues.extend(new_issues)

        # 回译抽检
        bt_issues: list[dict] = []
        if bt_samples:
            srcs = [a for a, _ in bt_samples]
            tgts = [b for _, b in bt_samples]
            for it in self.backtrans.check(srcs, tgts):
                it["chapter"] = ci
                bt_issues.append(it)
            storage.log_event(
                "chapter_backtranslation_checked",
                chapter=ci,
                sample_count=len(bt_samples),
                issue_count=len(bt_issues),
                issues=bt_issues,
            )

        # 翻译记忆库（仅作记录/参考，不用于跨位置复用译文）
        for s in text_segs:
            if s.target:
                storage.add_tm(s.source, s.target, ci)

        chapter.meta["backtranslation_issues"] = bt_issues
        storage.save_chapter(chapter)
        storage.set_chapter_status(ci, STATUS_DONE)
        storage.log_event(
            "chapter_done",
            chapter=ci,
            title=chapter.title,
            segment_count=len(text_segs),
            backtranslation_issue_count=len(bt_issues),
        )
        return done

    def _chapter_term_snapshot(self, text_segs) -> list:
        """返回当前章节要注入的术语快照；实时入库后可重新调用刷新。"""
        terms = self.storage.all_terms()
        if self.config.pipeline.glossary_scope != "chapter":
            return terms
        src_text = "\n".join(s.source for s in text_segs)
        hit = {t.source for t in self.storage.terms_in(terms, src_text)}
        return [t for t in terms
                if t.source in hit or (t.type == TYPE_PERSON and t.locked)]

    @staticmethod
    def _chapter_progress_label(title: str, index: int) -> str:
        """进度展示用章节名：优先用书内标题，避免内部序号与“第一章”等标题冲突。"""
        title = (title or "").strip()
        return title or f"章节 {index + 1}"

    # ── 批次术语抽取检查点 ────────────────────────────────────────────────
    @staticmethod
    def _batch_glossary_key(start_index: int, count: int) -> str:
        """返回批次术语抽取检查点键；批次边界变化时不会误命中旧键。"""
        return f"{start_index}:{count}"

    def _completed_batch_glossary_keys(self, chapter: int) -> set[str]:
        """从事件日志恢复本章已完成的批次术语抽取检查点（存储无关）。"""
        completed: set[str] = set()
        for row in self.storage.list_events(
            event_type="batch_glossary_extracted", limit=0
        ):
            if row.get("chapter") != chapter:
                continue
            start = row.get("start_index")
            count = row.get("count")
            if isinstance(start, int) and isinstance(count, int):
                completed.add(self._batch_glossary_key(start, count))
        return completed

    def _extract_batch_glossary(self, chapter: int, start_index: int, batch) -> dict[str, int]:
        """每批译完/续跑跳过后即时抽取术语，供同章后续批次使用。"""
        storage = self.storage
        src_text = "\n".join(s.source for s in batch)
        tgt_text = "\n".join(s.target or "" for s in batch)
        summary = self.extractor.extract_and_store(storage, src_text, tgt_text, chapter)
        storage.log_event(
            "batch_glossary_extracted",
            chapter=chapter,
            start_index=start_index,
            count=len(batch),
            summary=summary,
        )
        return summary

    # ── 全书最终审校 + 严重项定向重译 ────────────────────────────────────────
    _SEVERE_TYPES = ("missing", "mistranslation")
    _REVIEW_SCHEMA_VERSION = 1

    def _review_digest(self, text_segs, terms, *, autofix: bool) -> str:
        """计算一章审校输入摘要，用于识别可安全跳过的重复审校。

        摘要包含源译文、实际注入的最终术语快照、语言和自动修复策略；任一项
        变化都会使旧审校结果失效。提示词或审校协议变化时应递增 schema 版本。
        """
        payload = {
            "version": self._REVIEW_SCHEMA_VERSION,
            "source_lang": self.config.source_lang,
            "target_lang": self.config.target_lang,
            "autofix": autofix,
            "segments": [
                {"source": segment.source, "target": segment.target or ""}
                for segment in text_segs
            ],
            "terms": [
                {
                    "source": term.source,
                    "target": term.target,
                    "reading": term.reading,
                    "type": term.type,
                    "gender": term.gender,
                    "aliases": term.aliases,
                }
                for term in terms
            ],
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _review_book(
        self,
        *,
        progress: Optional[ProgressFn] = None,
        force: bool = False,
        autofix: bool | None = None,
    ) -> list[dict]:
        """使用最终术语库按书序审校全部章节，并逐章持久化结果。

        章节之间按 manifest 顺序处理，单章内部仍由 ``_review_chapter`` 将连续块
        并行送审。检测阶段只读固定译文；一个章节的全部问题合并后，严重项才按
        段号串行修复。已完成且输入摘要未变化的章节会被跳过，支持断点续审。
        """
        storage = self.storage
        manifest = storage.load_manifest()
        pending = [
            chapter["index"]
            for chapter in manifest.get("chapters", [])
            if chapter.get("status") != STATUS_DONE
        ]
        if pending:
            joined = ", ".join(str(index) for index in pending[:10])
            suffix = "…" if len(pending) > 10 else ""
            raise ValueError(
                "全书审校要求所有章节先完成翻译；"
                f"仍待翻译章节：{joined}{suffix}"
            )

        do_autofix = (
            self.config.pipeline.autofix_severe if autofix is None else autofix
        )
        chapters = manifest.get("chapters", [])
        loaded = {
            item["index"]: store.load_chapter(item["index"])
            for item in chapters
        }
        total = sum(len(chapter.text_segments) for chapter in loaded.values())
        done = 0
        all_issues: list[dict] = []
        analysis = store.load_analysis() or {}
        style = self.analyzer.style_brief(analysis)
        book_synopsis = str(analysis.get("book_synopsis", "") or "")

        store.log_event(
            "book_review_started",
            force=force,
            autofix=do_autofix,
            chapters=[item["index"] for item in chapters],
            total_segments=total,
        )
        for item in chapters:
            ci = item["index"]
            chapter = loaded[ci]
            text_segs = chapter.text_segments
            term_snapshot = self._chapter_term_snapshot(glossary, text_segs)
            digest = self._review_digest(
                text_segs, term_snapshot, autofix=do_autofix
            )
            label = f"全书审校：{self._chapter_progress_label(chapter.title, ci)}"

            if (
                not force
                and item.get("review_status") == REVIEW_DONE
                and chapter.meta.get("review_digest") == digest
            ):
                existing = [
                    issue
                    for issue in chapter.meta.get("review_issues", [])
                    if isinstance(issue, dict)
                ]
                all_issues.extend(existing)
                done += len(text_segs)
                store.log_event(
                    "chapter_review_skipped",
                    chapter=ci,
                    reason="unchanged",
                    issue_count=len(existing),
                )
                if progress:
                    progress(done, total, label)
                continue

            store.set_chapter_review_status(ci, REVIEW_RUNNING)
            if progress:
                progress(done, total, label)
            try:
                new_issues = self._review_chapter(text_segs, term_snapshot)
                for issue in new_issues:
                    issue["chapter"] = ci
                    issue.setdefault("fixed", False)
                    issue["stage"] = "review"

                if do_autofix:
                    self._autofix_severe(
                        text_segs,
                        new_issues,
                        term_snapshot,
                        style,
                        book_synopsis,
                        str(chapter.meta.get("source_digest", "") or ""),
                        store=store,
                        chapter_index=ci,
                    )
                    # 翻译阶段已写入旧译文；修复后覆盖相同 source_hash 的 TM。
                    for segment in text_segs:
                        if segment.target:
                            glossary.add_tm(segment.source, segment.target, ci)

                chapter.meta["review_issues"] = new_issues
                chapter.meta["review_digest"] = self._review_digest(
                    text_segs, term_snapshot, autofix=do_autofix
                )
                store.save_chapter(chapter)
                store.set_chapter_review_status(ci, REVIEW_DONE)
            except Exception as error:
                store.set_chapter_review_status(ci, REVIEW_FAILED)
                store.log_event(
                    "chapter_review_failed",
                    chapter=ci,
                    error_type=type(error).__name__,
                    error=str(error),
                )
                raise

            all_issues.extend(new_issues)
            done += len(text_segs)
            store.log_event(
                "chapter_reviewed",
                chapter=ci,
                issue_count=len(new_issues),
                issues=new_issues,
                autofix=do_autofix,
            )
            if progress:
                progress(done, total, label)

        store.log_event(
            "book_review_finished",
            issue_count=len(all_issues),
            autofix=do_autofix,
        )
        return all_issues

    def _review_chapter(self, text_segs, terms) -> list[dict]:
        """把一章切成连续块并行审校，返回映射到章内段号的问题。

        块 = 连续段序列（约 3 倍翻译批大小，减少调用次数与重复注入的输入 token）；
        块内 reviewer 返回的 index 是块内下标，加块首段偏移映射回章内段号；
        越界 index 直接丢弃（模型幻觉防御）。各块只读固定译文和术语快照，
        可并行调用；结果始终按原块顺序合并，保持确定性。
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

        def review_one(job: tuple[int, list]) -> list[dict]:
            """审校一个连续块，并把块内问题索引映射为章内索引。"""
            chunk_base, chunk = job
            srcs = [s.source for s in chunk]
            tgts = [s.target or "" for s in chunk]
            chunk_issues: list[dict] = []
            for it in self.reviewer.review(srcs, tgts, terms):
                idx = it.get("index")
                if isinstance(idx, str):
                    try:
                        idx = int(idx.strip())
                    except ValueError:
                        idx = None
                if isinstance(idx, int) and 0 <= idx < len(chunk):
                    it["index"] = chunk_base + idx
                    chunk_issues.append(it)
                else:
                    warnings.warn(
                        f"忽略无效审校索引 {it.get('index')!r}；"
                        f"当前审校块长度为 {len(chunk)}",
                        RuntimeWarning,
                        stacklevel=2,
                    )
            return chunk_issues

        workers = min(
            max(1, self.config.pipeline.review_concurrency),
            len(jobs),
        )
        if workers == 1:
            results = [review_one(job) for job in jobs]
        else:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                # executor.map 保持输入顺序；并发完成顺序不会改变 issue 顺序。
                results = list(ex.map(review_one, jobs))
        return [issue for chunk_issues in results for issue in chunk_issues]

    @staticmethod
    def _pack_contiguous(segs, budget: int) -> list[list]:
        """按源文字符预算把段保序打包成若干连续块。"""
        chunks: list[list] = []
        cur: list = []
        size = 0
        for s in segs:
            if cur and size + len(s.source) > budget:
                chunks.append(cur)
                cur, size = [], 0
            cur.append(s)
            size += len(s.source)
        if cur:
            chunks.append(cur)
        return chunks

    def _autofix_severe(self, text_segs, issues, terms, style,
                        book_synopsis: str = "", chapter_digest: str = "", *,
                        chapter_index: int | None = None) -> None:
        """对审校严重项（漏译/误译）带审校意见定向重译，每段最多一次。

        采纳条件 = 重译非空且过长度校验：采纳则标点规范化后更新 seg.target 并标 fixed=True；
        不采纳保持 fixed=False 留人工。最终审校重译时原滚动上下文已失效，
        用该段前后各 2 段译文做局部上下文。
        """
        storage = self.storage
        by_seg: dict[int, list[dict]] = {}
        for it in issues:
            if it.get("type") in self._SEVERE_TYPES:
                by_seg.setdefault(it["index"], []).append(it)
        for idx, seg_issues in sorted(by_seg.items()):
            seg = text_segs[idx]
            before = "\n".join(text_segs[j].target or ""
                               for j in range(max(0, idx - 2), idx))
            after = "\n".join(text_segs[j].target or ""
                              for j in range(idx + 1, min(len(text_segs), idx + 3)))
            feedback = "；".join(
                f"{it.get('detail', '')}（建议：{it.get('suggestion', '')}）"
                for it in seg_issues)
            new_t = self.translator.retranslate_with_feedback(
                seg.source, feedback=feedback, glossary_terms=terms, style=style,
                context_before=before, context_after=after,
                book_synopsis=book_synopsis, chapter_digest=chapter_digest)
            if new_t and not checks.length_flags([seg.source], [new_t]):
                if self._punctuation_enabled():
                    new_t = normalize_zh(new_t)
                old_t = seg.target
                seg.target = new_t
                for it in seg_issues:
                    it["fixed"] = True
                storage.log_event(
                    "autofix_applied",
                    chapter=chapter_index,
                    index=idx,
                    source=seg.source,
                    before=old_t,
                    after=new_t,
                    issues=seg_issues,
                )
            else:
                storage.log_event(
                    "autofix_rejected",
                    chapter=chapter_index,
                    index=idx,
                    source=seg.source,
                    before=seg.target,
                    proposed=new_t,
                    issues=seg_issues,
                )

    def _process_batch(self, batch, terms, ctx_text: str, style: str,
                       book_synopsis: str = "", chapter_digest: str = "") -> _BatchResult:
        """单个批次：整批翻译 → 润色。

        每段都在自身上下文里翻译，不跨位置复用译文（避免丢失语境信息）。
        全书概览/本章梗概作为恒定前缀注入，让译者把握全局。
        标点规范化在章末统一执行，以维持跨段引号状态。
        LLM 审校不在翻译批内做；全书完成后由独立 Review 阶段统一执行。
        """
        sources = [s.source for s in batch]
        targets = self.translator.translate_batch(
            sources, glossary_terms=terms, style=style, context=ctx_text,
            book_synopsis=book_synopsis, chapter_digest=chapter_digest)

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
    ALL_STEPS = ("translate", "review", "qa", "report", "assemble")

    def run_review(
        self,
        input_path: str,
        *,
        progress: Optional[ProgressFn] = None,
        force: bool = False,
        autofix: bool | None = None,
    ) -> dict[str, Any]:
        """单独执行最终全书审校，返回状态目录和按书序汇总的问题。"""
        storage = self._locate_existing_store(input_path, progress=progress)
        with self._storage_lock():
            manifest = storage.load_manifest()
            self._apply_language(
                manifest.get("source_lang") or self.config.source_lang
            )
            try:
                issues = self._review_book(
                    progress=progress,
                    force=force,
                    autofix=autofix,
                )
            finally:
                self._flush_usage(scope="review")
        return {"storage": storage, "review_issues": issues}

    def run_steps(self, input_path: str, steps, *,
                  progress: Optional[ProgressFn] = None,
                  out_format: str = "epub", out_path: str | None = None) -> dict[str, Any]:
        """按需执行步骤子集（可单选可全选）。steps ⊆ ALL_STEPS。"""
        from ..agents.consistency import ConsistencyChecker
        from ..assemble.report import build_report
        from ..assemble.writer import assemble, bilingual_out_path

        steps = set(steps)
        run_steps_input = sorted(steps)

        if "translate" in steps:
            storage = self.run(input_path, progress=progress)
        else:
            storage = self.prepare(input_path, progress=progress)
            m = storage.load_manifest()
            self._apply_language(m.get("source_lang") or self.config.source_lang)

        with self._storage_lock():
            storage.log_event("run_steps_started", steps=run_steps_input, input_path=input_path)

            qa_issues: list[dict] = []
            report: dict[str, Any] | None = None
            try:
                if "qa" in steps:
                    if progress:
                        progress(0, 0, "一致性 QA…")
                    qa_issues = ConsistencyChecker(self.client, self.config).check(storage)
                    storage.log_event(
                        "consistency_qa_finished",
                        issue_count=len(qa_issues),
                        issues=qa_issues,
                    )

                self._flush_usage(scope="pipeline")
                if "report" in steps:
                    if progress:
                        progress(0, 0, "生成报告…")
                    report = build_report(storage)
                    report["consistency_issues"] = qa_issues
                    storage.save_report(report)
                    storage.log_event("report_saved")
            finally:
                self._flush_usage(scope="pipeline")

            outputs: list[str] = []
            if "assemble" in steps:
                if progress:
                    progress(0, 0, "回填译文…")
                out_cfg = self.config.output
                do_mono, do_bilingual = out_cfg.mono, out_cfg.bilingual
                if not do_mono and not do_bilingual:
                    do_mono = True  # 兜底：mono/bilingual 都关时至少产一个单语产物
                if do_mono:
                    outputs.append(
                        assemble(
                            storage,
                            input_path,
                            out_path=out_path,
                            out_format=out_format,
                            bilingual=False,
                            about_page=out_cfg.about_page,
                        )
                    )
                if do_bilingual:
                    bi_out_path = bilingual_out_path(out_path) if out_path else None
                    outputs.append(
                        assemble(
                            storage,
                            input_path,
                            out_path=bi_out_path,
                            out_format=out_format,
                            bilingual=True,
                            order=out_cfg.bilingual_order,
                            preserve_source_style=(
                                out_cfg.bilingual_preserve_source_style
                            ),
                            about_page=out_cfg.about_page,
                        )
                    )
                storage.log_event("assembled", outputs=outputs, out_format=out_format)

            storage.log_event(
                "run_steps_finished",
                steps=run_steps_input,
                outputs=outputs,
                qa_issue_count=len(qa_issues),
            )
            return {
                "storage": storage,
                "output": outputs[0] if outputs else None,
                "outputs": outputs,
                "report": report,
                "qa_issues": qa_issues,
            }

    def run_all(self, input_path: str, *, progress: Optional[ProgressFn] = None,
                out_format: str = "epub", out_path: str | None = None,
                do_qa: bool | None = None) -> dict[str, Any]:
        """翻译 → 最终审校 → 一致性 QA → 报告 → 回填，返回结果汇总。"""
        steps = {"translate", "report", "assemble"}
        if self.config.pipeline.review:
            steps.add("review")
        if do_qa if do_qa is not None else self.config.pipeline.consistency_qa:
            steps.add("qa")
        return self.run_steps(input_path, steps, progress=progress,
                              out_format=out_format, out_path=out_path)
