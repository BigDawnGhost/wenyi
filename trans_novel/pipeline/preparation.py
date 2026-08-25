"""准备服务：状态定位、输入解析、语言检测、初始化事务、风格分析与全书理解预扫。

负责：
  * 状态目录定位、输入解析、PDF 转换缓存、源文件哈希校验、语言检测；
  * 初始化事务（begin_initialization → chapters/analysis/glossary/context →
    最后原子写入 initialized manifest → finish_initialization）；
  * 风格分析、初始术语、RollingContext 与全书理解预扫（逐章梗概 + 全书概览）。
样本选择属于准备阶段；语言归一化由同级的 language.py 提供，供本服务与 Runtime
共同复用。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Any

from ..glossary.store import GlossaryStore
from ..ingest.epub_reader import peek_epub_title
from ..ingest.segmenter import load_document
from .context import RollingContext
from .language import normalize_lang
from .runstore import RunStore, slugify

if TYPE_CHECKING:
    from .runtime import PipelineRuntime

ProgressFn = Callable[[int, int, str], None]


class PreparationService:
    """准备阶段的领域服务：定位/解析/初始化/全书理解。"""

    def __init__(self, runtime: PipelineRuntime):
        self._runtime = runtime

    # ── 定位与续跑 ────────────────────────────────────────────────────────
    def locate_existing(
        self,
        input_path: str,
        *,
        progress: ProgressFn | None = None,
    ) -> RunStore:
        """定位输入文件对应的既有状态，不创建或初始化新的翻译任务。

        PDF 的状态目录直接取自文件名，因此可在调用 MinerU 前完成检查；EPUB 只读
        OPF 取书名，不走全本 ingest（全本解析会对每个物理资源做一次 annotate，专为
        定位目录多付这个成本没有必要，而且后面回填导出时还会重新 annotate 一遍）。其它
        格式仍需本地解析书名来得到与 prepare 相同的状态目录。
        """
        ext = os.path.splitext(input_path)[1].lower()
        if ext == ".pdf":
            title = os.path.splitext(os.path.basename(input_path))[0]
        elif ext == ".epub":
            if progress:
                progress(0, 0, "查找翻译进度…")
            title = peek_epub_title(input_path)
        else:
            if progress:
                progress(0, 0, "查找翻译进度…")
            doc = load_document(
                input_path,
                self._runtime.config.source_lang,
                self._runtime.config.target_lang,
                split_segments=self._runtime.config.segment.max_chars_per_segment,
            )
            title = doc.title

        store = RunStore(
            os.path.join(self._runtime.config.state_dir, slugify(title)),
            create=False,
        )
        if not store.exists():
            raise ValueError("尚无翻译进度。请先运行 translate。")
        self._runtime.ensure_store_source(store, input_path)
        self._runtime.bind_llm_events(store)
        self._runtime.attach_metrics_store(store)
        return store

    def prepare(
        self,
        input_path: str,
        *,
        progress: ProgressFn | None = None,
    ) -> RunStore:
        """解析输入并定位状态目录；首次运行时在书级锁内完成初始化。

        PDF 的状态目录可直接由文件名确定，因此续跑时先检查 manifest，
        避免重新调用外部转换服务；首次转换产生的 HTML 缓存在该状态目录中。
        """
        if os.path.splitext(input_path)[1].lower() == ".pdf":
            # PDF 的书名固定取文件名，首次解析前即可确定状态目录。
            pdf_title = os.path.splitext(os.path.basename(input_path))[0]
            run_dir = os.path.join(self._runtime.config.state_dir, slugify(pdf_title))
            store = RunStore(run_dir)
            self._runtime.bind_llm_events(store)
            self._runtime.attach_metrics_store(store)
            with store.lock():
                if store.exists():
                    self._runtime.ensure_store_source(store, input_path)
                    store.log_event(
                        "run_resumed",
                        input_path=input_path,
                        run_dir=store.run_dir,
                    )
                    return store
                if progress:
                    progress(0, 0, "解析文档…")
                source_hash = self._runtime.initial_source_sha256(input_path)
                # 转换失败也要留下同源初始化标记，确保重试保留失败运行账本。
                store.begin_initialization(source_hash)
                doc = load_document(
                    input_path,
                    self._runtime.config.source_lang,
                    self._runtime.config.target_lang,
                    split_segments=self._runtime.config.segment.max_chars_per_segment,
                    cache_dir=store.source_dir,
                    source_hash=source_hash,
                )
                if self._runtime.source_sha256(input_path) != source_hash:
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
        source_hash = self._runtime.initial_source_sha256(input_path)
        # 超长段按句拆分（max_chars_per_segment），续段标 cont 供回填并回
        doc = load_document(
            input_path,
            self._runtime.config.source_lang,
            self._runtime.config.target_lang,
            split_segments=self._runtime.config.segment.max_chars_per_segment,
        )
        if self._runtime.source_sha256(input_path) != source_hash:
            raise ValueError("源文件在解析期间发生变化；请确认文件稳定后重试。")
        run_dir = os.path.join(self._runtime.config.state_dir, slugify(doc.title))
        store = RunStore(run_dir)
        self._runtime.bind_llm_events(store)
        self._runtime.attach_metrics_store(store)
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
            self._runtime.ensure_store_source(store, input_path)
            store.log_event("run_resumed", input_path=input_path, run_dir=store.run_dir)
            return store  # 已有进度 → 直接续跑，不重置（语言在 run() 里按 manifest 应用）

        initialization_hash = source_hash or self._runtime.source_sha256(input_path)
        store.begin_initialization(initialization_hash)

        # 新建：auto 时只使用模型检测主要语言；失败则要求用户显式指定。
        if self._runtime.config.source_lang in ("auto", "", None):
            if progress:
                progress(0, 0, "识别语言…")
            detected = self.detect_language_ai(doc)
            if not detected:
                store.log_event("language_detection_failed", source_lang=doc.source_lang)
                raise RuntimeError(
                    "自动识别源语言失败：请检查模型配置，或在 config.yaml 的 "
                    "language.source 指定 ISO 639-1 语言代码（如 ja/en/ko/ru/fr/de/es）。"
                )
            doc.source_lang = detected
            store.log_event("language_detected", source_lang=doc.source_lang)
        self._runtime.apply_language(doc.source_lang)

        manifest = store.stage_document(
            doc,
            source_hash=initialization_hash,
        )
        glossary = GlossaryStore(store.glossary_path)
        try:
            if progress:
                progress(0, 0, "分析全书风格…")
            sample = self.sample_text(doc)
            analysis = self._runtime.analyzer.analyze(sample) if sample else {}
            if analysis:
                self._runtime.analyzer.seed_glossary(glossary, analysis)
            store.save_analysis(analysis)
            store.log_event("analysis_saved", has_analysis=bool(analysis))
            store.save_context(
                RollingContext(
                    max_recent_keep=max(
                        40,
                        self._runtime.config.pipeline.rolling_context_segments,
                    )
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
                    "review": self._runtime.config.pipeline.review,
                    "polish": self._runtime.config.pipeline.polish,
                    "book_understanding": self._runtime.config.pipeline.book_understanding,
                    "review_concurrency": self._runtime.config.pipeline.review_concurrency,
                    "review_output_retries": (self._runtime.config.pipeline.review_output_retries),
                },
            )
        finally:
            glossary.close()
        return store

    def activate(self, store: RunStore) -> dict[str, Any]:
        """恢复 manifest 语言并同步给全部 Agent，返回该 manifest。"""
        manifest = store.load_manifest()
        self._runtime.apply_manifest_languages(manifest)
        return manifest

    def detect_language_ai(self, doc) -> str:
        """用模型检测正文主要语言，返回 ISO 代码（如 ja/en/ru）。失败返回空串。"""
        # labeled=False：纯源文样本，防多点采样的中文标签污染语言检测
        sample = self.sample_text(doc, labeled=False)[:1500]
        if not sample.strip():
            return ""
        system = (
            "你是语言识别器。判断给定文本的主要自然语言，"
            '仅输出 JSON：{"language":"<ISO 639-1 两字母代码，如 ja/en/ru/ko/fr/de/zh>"}。'
            "无法判断时 language 置为空字符串。"
        )
        try:
            data = self._runtime.client.complete_json(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": sample},
                ],
                tier="cheap",
                stage="language_detect",
            )
            code = (data.get("language") if isinstance(data, dict) else "") or ""
            return normalize_lang(str(code))
        except Exception:  # noqa: BLE001 - provider errors mean detection failed
            return ""

    @staticmethod
    def sample_text(doc, *, labeled: bool = True) -> str:
        """取风格分析样章。labeled=True 时多点采样（开头/中部/结尾各一段，带中文标注），
        让分析覆盖全书风格全貌；labeled=False 返回单段纯源文（语言检测用，不能混入中文标签）。"""
        texts = ["\n".join(s.source for s in ch.text_segments) for ch in doc.chapters]
        texts = [t for t in texts if len(t) > 200]
        if not texts:  # 兜底：全书都是短章
            joined = "\n".join(s.source for ch in doc.chapters[:2] for s in ch.text_segments)
            return joined[:6000]
        if not labeled:
            return texts[0][:6000]
        picks = [
            (0, "开头样章"),
            (len(texts) // 2, "中部样章"),
            (len(texts) - 1, "结尾样章"),
        ]
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

    # ── 全书理解预扫（源文逐章梗概 + 全书概览）────────────────────────────
    def ensure_understanding(
        self,
        store: RunStore,
        progress: ProgressFn | None = None,
    ) -> str:
        """翻译前预扫源文：逐章梗概存入 chapter.meta，归并出全书概览存入 analysis。

        幂等、可续跑：已有梗概/概览则跳过。返回全书概览（注入各章翻译 prompt）。
        关闭 book_understanding 时直接返回空串。
        """
        if not self._runtime.config.pipeline.book_understanding:
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
                workers=max(1, self._runtime.config.pipeline.prescan_concurrency),
            )
            workers = max(1, self._runtime.config.pipeline.prescan_concurrency)
            if progress:
                progress(0, len(todo), "预扫章节梗概")
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {
                    ex.submit(self._runtime.synopsizer.digest_chapter, src): ci for ci, src in todo
                }
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
            synopsis = self._runtime.synopsizer.book_synopsis(
                digests,
                self._runtime.analyzer.style_brief(analysis),
            )
            analysis["book_synopsis"] = synopsis
            store.save_analysis(analysis)
            store.log_event("book_synopsis_saved", synopsis=synopsis)
        return synopsis
