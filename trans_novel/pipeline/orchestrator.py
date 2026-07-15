"""编排器：驱动全流程，章级状态机 + 断点续跑。

单章流水线（章内批次**串行**，逐批刷新滚动上下文与术语快照；跨章亦串行传递梗概）：
  每批：渲染上下文（含前一批刚译出的译文）→ 翻译（对齐保证）→ 润色（可选）→
        标点规范化 → 术语/称呼/固定表达实时抽取入库 → 立即供下一批参照。
  章末：全章术语兜底抽取 → 整章分块并行审校 →
        严重项串行定向重译（autofix_severe，过长度校验才采纳）→ 回译抽检 → 写 TM → 落盘标记 done。
翻译前先预扫源文建立全书理解（逐章梗概+全书概览，fast 档并行），作恒定前缀注入每章翻译。

run_all：在翻译全书后接 术语 AI 审计统一 → 一致性 QA → 写报告 → 回填出 EPUB，一气呵成。
进度回调 progress(done_segments, total_segments, label) 与 UI 无关，每批完成即触发。
"""

from __future__ import annotations

import os
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..config import Config
from ..glossary.extractor import GlossaryExtractor
from ..glossary.store import GlossaryStore
from ..llm.base import LLMClient
from ..llm.factory import build_client
from ..llm.usage import merge_usage_summaries, usage_delta
from ..ingest.segmenter import load_document, batch_segments
from ..postprocess.punct import normalize_zh
from ..agents.analyzer import Analyzer
from ..agents.synopsis import Synopsizer
from ..agents.translator import Translator
from ..agents.reviewer import Reviewer, BackTranslator
from ..agents.polisher import Polisher
from . import checks
from .context import RollingContext
from .runstore import RunStore, slugify, STATUS_DONE

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
    c = (code or "").strip().lower()
    if not c or c in {"auto", "unknown", "und", "uncertain", "mixed", "多语言", "未知"}:
        return ""
    if c in _LANG_ALIASES:
        return _LANG_ALIASES[c]
    return c[:2] if c[:2].isalpha() else ""


@dataclass
class _BatchResult:
    targets: list[str]
    issues: list[dict] = field(default_factory=list)
    bt_samples: list[tuple[str, str]] = field(default_factory=list)


class Orchestrator:
    def __init__(self, config: Config, client: LLMClient | None = None):
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

    # ── 语言解析 ────────────────────────────────────────────────────────────
    def _apply_language(self, lang: str) -> None:
        """把解析出的源语言应用到 config 与各 agent（auto 检测后调用）。"""
        resolved = lang or self.config.source_lang
        self.config.source_lang = resolved
        for ag in (self.analyzer, self.synopsizer, self.translator, self.reviewer,
                   self.backtrans, self.polisher, self.extractor):
            ag.src = resolved

    # ── 准备 / 续跑入口 ──────────────────────────────────────────────────
    def prepare(self, input_path: str, *,
                progress: Optional[ProgressFn] = None) -> RunStore:
        if progress:
            progress(0, 0, "解析文档…")
        # 超长段按句拆分（max_chars_per_segment），续段标 cont 供回填并回
        doc = load_document(input_path, self.config.source_lang, self.config.target_lang,
                            split_segments=self.config.segment.max_chars_per_segment)
        run_dir = os.path.join(self.config.state_dir, slugify(doc.title))
        store = RunStore(run_dir)
        if store.exists():
            store.log_event("run_resumed", input_path=input_path, run_dir=store.run_dir)
            return store  # 已有进度 → 直接续跑，不重置（语言在 run() 里按 manifest 应用）

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

        store.init_from_document(doc)
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
                "autofix_severe": self.config.pipeline.autofix_severe,
                "polish": self.config.pipeline.polish,
                "backtranslate_sample": self.config.pipeline.backtranslate_sample,
                "consistency_qa": self.config.pipeline.consistency_qa,
                "book_understanding": self.config.pipeline.book_understanding,
                "review_concurrency": self.config.pipeline.review_concurrency,
            },
        )
        glossary = GlossaryStore(store.glossary_path)
        if progress:
            progress(0, 0, "分析全书风格…")
        sample = self._sample_text(doc)
        analysis = self.analyzer.analyze(sample) if sample else {}
        if analysis:
            self.analyzer.seed_glossary(glossary, analysis)
        store.save_analysis(analysis)
        store.log_event("analysis_saved", has_analysis=bool(analysis))
        glossary.close()
        store.save_context(RollingContext().to_dict())
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
            progress: Optional[ProgressFn] = None) -> RunStore:
        store = self.prepare(input_path, progress=progress)
        manifest = store.load_manifest()
        self._apply_language(manifest.get("source_lang") or self.config.source_lang)
        glossary = GlossaryStore(store.glossary_path)
        context = RollingContext.from_dict(store.load_context() or {})
        style = self.analyzer.style_brief(store.load_analysis() or {})
        # 翻译前预扫源文，建立全书理解（幂等、可续跑）；全书概览注入每章翻译
        book_synopsis = self._build_understanding(store, progress=progress)

        if only_chapter is not None:
            targets = [only_chapter]
            progress_chapters = targets
        else:
            targets = store.pending_chapters()
            progress_chapters = [
                chapter["index"] for chapter in manifest.get("chapters", [])
            ]

        total, done = self._progress_counts(store, progress_chapters)
        store.log_event(
            "translate_run_started",
            only_chapter=only_chapter,
            chapters=targets,
            total_segments=total,
        )
        try:
            for ci in targets:
                done = self._translate_chapter(
                    ci, store, glossary, context, style, book_synopsis,
                    progress=progress, done=done, total=total)
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

    def _progress_counts(self, store: RunStore,
                         chapter_indices: list[int]) -> tuple[int, int]:
        """按全书批次检查点计算进度，续跑从已有译文数量开始显示。

        只有整批译文齐全时才计入 done；不完整批次会整体重跑，提前计入其中
        个别已有段会导致完成数重复累加。
        """
        total = 0
        done = 0
        for ci in chapter_indices:
            segments = store.load_chapter(ci).text_segments
            total += len(segments)
            for batch in batch_segments(
                segments, self.config.segment.max_chars_per_batch
            ):
                if all(segment.target and segment.target.strip()
                       for segment in batch):
                    done += len(batch)
        return total, done

    # ── 全书理解预扫（源文逐章梗概 + 全书概览）────────────────────────────────
    def _build_understanding(self, store: RunStore,
                             progress: Optional[ProgressFn] = None) -> str:
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
        loaded = {c.get("index", i): store.load_chapter(c.get("index", i))
                  for i, c in enumerate(chapters)}
        todo = [(ci, "\n".join(s.source for s in ch.text_segments))
                for ci, ch in loaded.items() if not ch.meta.get("source_digest")]
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
                futs = {ex.submit(self.synopsizer.digest_chapter, src): ci
                        for ci, src in todo}
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
        digests = [loaded[c.get("index", i)].meta.get("source_digest", "") or ""
                   for i, c in enumerate(chapters)]

        analysis = store.load_analysis() or {}
        synopsis = analysis.get("book_synopsis", "")
        if not synopsis and any(d.strip() for d in digests):
            if progress:
                progress(0, 0, "生成全书概览…")
            synopsis = self.synopsizer.book_synopsis(
                digests, self.analyzer.style_brief(analysis))
            analysis["book_synopsis"] = synopsis
            store.save_analysis(analysis)
            store.log_event("book_synopsis_saved", synopsis=synopsis)
        return synopsis

    # ── 章节标题 / 目录项翻译（书名保持原文）──────────────────────────────
    def _translate_titles(self, store: RunStore, glossary: GlossaryStore,
                          progress: Optional[ProgressFn] = None) -> None:
        """把各章标题和额外目录项翻成中文，写回 manifest（幂等：已全部译过则跳过）。

        书名保持原文，不写 title_translated；借术语表保证章节标题里的专名一致。
        """
        from ..agents import prompts

        m = store.load_manifest()
        chapters = m.get("chapters", [])

        # 标题压成单行，避免内嵌换行破坏 numbered 对齐
        def _flat(s: object) -> str:
            return " ".join(str(s or "").split())

        raw_meta = m.get("meta")
        meta = raw_meta if isinstance(raw_meta, dict) else {}
        chapter_hrefs = {c.get("href") for c in chapters if c.get("href")}
        raw_toc_entries = meta.get("toc_entries", [])
        toc_entry_items = raw_toc_entries if isinstance(raw_toc_entries, list) else []
        toc_entries = [
            e for e in toc_entry_items
            if isinstance(e, dict) and e.get("href") not in chapter_hrefs and _flat(e.get("title", ""))
        ]

        titled_chapters = [c for c in chapters if _flat(c.get("title", ""))]
        m.pop("title_translated", None)
        if (all(c.get("title_translated") for c in titled_chapters)
                and all(e.get("title_translated") for e in toc_entries)):
            store.save_manifest(m)
            store.log_event("titles_skipped", reason="already_translated")
            return  # 已译，断点续跑不重复调用

        titles = (
            [_flat(c.get("title", "")) for c in titled_chapters]
            + [_flat(e.get("title", "")) for e in toc_entries]
        )
        if not any(t.strip() for t in titles):
            return
        if progress:
            progress(0, 0, "翻译章节标题…")
        system = prompts.render("title_translator_system",
                                src=self.config.source_lang, tgt=self.config.target_lang,
                                n=len(titles))
        user = prompts.render("title_translator_user",
                              src=self.config.source_lang, tgt=self.config.target_lang,
                              glossary=prompts.render_glossary(glossary.all_terms()),
                              n=len(titles), numbered_titles=prompts.numbered(titles))
        try:
            data = self.client.complete_json(
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}], tier="strong",
                stage="title_translate")
        except Exception:
            return
        out = data.get("titles") if isinstance(data, dict) else data
        if not isinstance(out, list) or len(out) != len(titles):
            store.log_event(
                "titles_translation_rejected",
                reason="count_mismatch",
                expected=len(titles),
                actual=len(out) if isinstance(out, list) else None,
            )
            return
        out = [str(t).strip() for t in out]
        chapter_out = out[:len(titled_chapters)]
        toc_out = out[len(titled_chapters):]
        for c, t in zip(titled_chapters, chapter_out):
            c["title_translated"] = t or c.get("title")
        for e, t in zip(toc_entries, toc_out):
            e["title_translated"] = t or e.get("title")
        store.save_manifest(m)
        store.log_event(
            "titles_translated",
            titles=[
                {"index": i - 1, "source": src, "target": tgt}
                for i, (src, tgt) in enumerate(zip(titles, out))
            ],
        )

    # ── 单章 ──────────────────────────────────────────────────────────────
    def _translate_chapter(self, ci: int, store: RunStore,
                           glossary: GlossaryStore, context: RollingContext,
                           style: str, book_synopsis: str = "", *,
                           progress: Optional[ProgressFn] = None,
                           done: int = 0, total: int = 0) -> int:
        chapter = store.load_chapter(ci)
        text_segs = chapter.text_segments
        if not text_segs:
            store.set_chapter_status(ci, STATUS_DONE)
            store.log_event("chapter_skipped", chapter=ci, reason="empty")
            return done
        chapter_digest = chapter.meta.get("source_digest", "")

        batches = batch_segments(text_segs, self.config.segment.max_chars_per_batch)
        label = self._chapter_progress_label(chapter.title, ci)
        # prepare() 的最后一个标签通常是“解析文档…”。续跑首批可能先恢复术语，
        # 若不在章首刷新，整个模型请求期间都会错误地显示成仍在解析源文件。
        if progress:
            progress(done, total, label)
        glossary_checkpoints = store.completed_batch_glossary_keys(ci)
        # 章内术语快照会在每个批次术语抽取后刷新，让新确认的称呼/口癖/固定表达
        # 立即影响后续批次。glossary_scope=chapter 时仍按本章源文裁剪，避免全量表过大。
        term_snapshot = self._chapter_term_snapshot(glossary, text_segs)

        # 逐批串行：每批渲染最新上下文 → 处理 → 立即把译文并入上下文供下一批参照。
        # 不再并发，换取章内跨批的代词/术语/语气连贯。
        # 断点续跑（段/批级）：上次中断前已译完并落盘的批次，整批跳过、不重翻，只重建上下文。
        review_issues: list[dict] = [
            i for i in chapter.meta.get("review_issues", [])
            if i.get("stage") != "length"
        ]
        bt_samples: list[tuple[str, str]] = []
        seg_base = 0   # 当前批首段的章内段号（issue 批内下标 → 章内段号）
        for b in batches:
            batch_start = seg_base
            glossary_key = store.batch_glossary_key(batch_start, len(b))
            existing_targets = [s.target for s in b if s.target and s.target.strip()]
            if len(existing_targets) == len(b):
                # 该批上次已在原位、原上下文中译完 → 复用，重建滚动上下文后跳过
                context.add_targets(existing_targets)
                if glossary_key in glossary_checkpoints:
                    summary = {"inserted": 0, "conflict": 0, "unchanged": 0,
                               "updated": 0, "skipped": 1}
                else:
                    summary = self._extract_batch_glossary(
                        glossary, store, ci, batch_start, b
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
                if progress:
                    progress(done, total, label)
                continue

            ctx_text = context.render(self.config.pipeline.rolling_context_segments)
            res = self._process_batch(b, term_snapshot, ctx_text, style,
                                      book_synopsis, chapter_digest)
            for s, t in zip(b, res.targets):
                s.target = t
            store.log_event(
                "batch_translated",
                chapter=ci,
                start_index=batch_start,
                count=len(b),
                polished=self.config.pipeline.polish,
                punctuation_normalized=self.config.punctuation_normalize,
                issues=res.issues,
                backtranslate_sample_count=len(res.bt_samples),
                segments=[
                    {"index": batch_start + i, "source": s.source, "target": t}
                    for i, (s, t) in enumerate(zip(b, res.targets))
                ],
            )
            context.add_targets(res.targets)
            for it in res.issues:
                it["chapter"] = ci
                it["index"] += batch_start   # 批内下标 → 章内段号
            review_issues.extend(res.issues)
            bt_samples.extend(res.bt_samples)
            done += len(b)
            seg_base += len(b)
            if progress:
                progress(done, total, label)
            # 增量持久化：本批译文 + 累计问题落盘，下次中断从此批之后续跑
            chapter.meta["review_issues"] = review_issues
            store.save_chapter(chapter)
            # 译文落盘后再抽取术语，避免中断时术语库领先章节产物。
            self._extract_batch_glossary(glossary, store, ci, batch_start, b)
            glossary_checkpoints.add(glossary_key)
            term_snapshot = self._chapter_term_snapshot(glossary, text_segs)

        # 全章术语抽取入库：保留为兜底，捕捉跨段才能确认的称呼/口癖/固定表达。
        # 放在 review 前，让本章审校也能使用兜底抽出的术语。
        src_text = "\n".join(s.source for s in text_segs)
        tgt_text = "\n".join(s.target or "" for s in text_segs)
        self.extractor.extract_and_store(glossary, src_text, tgt_text, ci)
        term_snapshot = self._chapter_term_snapshot(glossary, text_segs)
        store.log_event("chapter_glossary_extracted", chapter=ci)

        # ── 章末整章审校（移出批内关键路径；块内 index 映射回章内段号）──
        # 幂等：续跑重入章末时清掉旧审校项，防重复累积。
        if self.config.pipeline.review:
            review_issues = []
            new_issues = self._review_chapter(text_segs, term_snapshot)
            store.log_event(
                "chapter_reviewed",
                chapter=ci,
                issue_count=len(new_issues),
                issues=new_issues,
            )
            if self.config.pipeline.autofix_severe:
                self._autofix_severe(text_segs, new_issues, term_snapshot, style,
                                     book_synopsis, chapter_digest,
                                     store=store, chapter_index=ci)
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
            store.log_event(
                "chapter_backtranslation_checked",
                chapter=ci,
                sample_count=len(bt_samples),
                issue_count=len(bt_issues),
                issues=bt_issues,
            )

        # 翻译记忆库（仅作记录/参考，不用于跨位置复用译文）
        for s in text_segs:
            if s.target:
                glossary.add_tm(s.source, s.target, ci)

        chapter.meta["review_issues"] = review_issues
        chapter.meta["backtranslation_issues"] = bt_issues
        store.save_chapter(chapter)
        store.set_chapter_status(ci, STATUS_DONE)
        store.log_event(
            "chapter_done",
            chapter=ci,
            title=chapter.title,
            segment_count=len(text_segs),
            review_issue_count=len(review_issues),
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

    def _extract_batch_glossary(self, glossary: GlossaryStore, store: RunStore,
                                chapter: int, start_index: int, batch) -> dict[str, int]:
        """每批译完/续跑跳过后即时抽取术语，供同章后续批次使用。"""
        src_text = "\n".join(s.source for s in batch)
        tgt_text = "\n".join(s.target or "" for s in batch)
        summary = self.extractor.extract_and_store(glossary, src_text, tgt_text, chapter)
        store.log_event(
            "batch_glossary_extracted",
            chapter=chapter,
            start_index=start_index,
            count=len(batch),
            summary=summary,
        )
        return summary

    # ── 章末审校 + 严重项定向重译 ────────────────────────────────────────────
    _SEVERE_TYPES = ("missing", "mistranslation")

    def _review_chapter(self, text_segs, terms) -> list[dict]:
        """整章分块并行审校（章末统一做，不在批内阻塞翻译主路径）。

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
            chunk_base, chunk = job
            srcs = [s.source for s in chunk]
            tgts = [s.target or "" for s in chunk]
            chunk_issues: list[dict] = []
            for it in self.reviewer.review(srcs, tgts, terms):
                idx = it.get("index")
                if isinstance(idx, int) and 0 <= idx < len(chunk):
                    it["index"] = chunk_base + idx
                    chunk_issues.append(it)
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
                        store: RunStore | None = None,
                        chapter_index: int | None = None) -> None:
        """对审校严重项（漏译/误译）带审校意见定向重译，每段最多一次。

        采纳条件 = 重译非空且过长度校验：采纳则标点规范化后更新 seg.target 并标 fixed=True；
        不采纳保持 fixed=False 留人工。章末重译时原滚动上下文已失效，用该段前后各 2 段译文做局部上下文。
        """
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
                if self.config.punctuation_normalize:
                    new_t = normalize_zh(new_t)
                old_t = seg.target
                seg.target = new_t
                for it in seg_issues:
                    it["fixed"] = True
                if store is not None:
                    store.log_event(
                        "autofix_applied",
                        chapter=chapter_index,
                        index=idx,
                        source=seg.source,
                        before=old_t,
                        after=new_t,
                        issues=seg_issues,
                    )
            elif store is not None:
                store.log_event(
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
        """单个批次：整批翻译 → 润色 → 标点规范化。

        每段都在自身上下文里翻译，不跨位置复用译文（避免丢失语境信息）。
        全书概览/本章梗概作为恒定前缀注入，让译者把握全局。
        LLM 审校不在批内做（移至章末统一做，见 _review_chapter，不阻塞翻译主路径）。
        """
        sources = [s.source for s in batch]
        targets = self.translator.translate_batch(
            sources, glossary_terms=terms, style=style, context=ctx_text,
            book_synopsis=book_synopsis, chapter_digest=chapter_digest)

        issues: list[dict] = []

        if self.config.pipeline.polish:
            polished = self.polisher.polish(targets, glossary_terms=terms, style=style)
            if len(polished) == len(targets):
                targets = polished

        if self.config.punctuation_normalize:
            targets = [normalize_zh(t) if t else t for t in targets]

        bt_samples: list[tuple[str, str]] = []
        rate = self.config.pipeline.backtranslate_sample
        if rate > 0:
            for s, t in zip(sources, targets):
                if random.random() < rate:
                    bt_samples.append((s, t or ""))

        return _BatchResult(targets=targets, issues=issues, bt_samples=bt_samples)

    # ── 可选步骤 / 连续全流程 ────────────────────────────────────────────────
    ALL_STEPS = ("translate", "qa", "report", "assemble")

    def run_steps(self, input_path: str, steps, *,
                  progress: Optional[ProgressFn] = None,
                  out_format: str = "epub", out_path: str | None = None) -> dict[str, Any]:
        """按需执行步骤子集（可单选可全选）。steps ⊆ ALL_STEPS。"""
        from ..agents.consistency import ConsistencyChecker
        from ..assemble.writer import assemble, bilingual_out_path
        from ..assemble.report import build_report

        steps = set(steps)
        run_steps_input = sorted(steps)

        if "translate" in steps:
            store = self.run(input_path, progress=progress)
        else:
            store = self.prepare(input_path, progress=progress)
            m = store.load_manifest()
            self._apply_language(m.get("source_lang") or self.config.source_lang)
        store.log_event("run_steps_started", steps=run_steps_input, input_path=input_path)

        glossary = GlossaryStore(store.glossary_path)
        qa_issues: list[dict] = []
        report: dict[str, Any] | None = None
        try:
            if "qa" in steps:
                if progress:
                    progress(0, 0, "一致性 QA…")
                qa_issues = ConsistencyChecker(self.client, self.config).check(store, glossary)
                store.log_event(
                    "consistency_qa_finished",
                    issue_count=len(qa_issues),
                    issues=qa_issues,
                )

            self._flush_usage(store, scope="pipeline")
            if "report" in steps:
                if progress:
                    progress(0, 0, "生成报告…")
                report = build_report(store, glossary)
                report["consistency_issues"] = qa_issues
                store.save_report(report)
                store.log_event("report_saved", path=store.report_path)
        finally:
            glossary.close()
            self._flush_usage(store, scope="pipeline")

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
                        store,
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
                        store,
                        input_path,
                        out_path=bi_out_path,
                        out_format=out_format,
                        bilingual=True,
                        order=out_cfg.bilingual_order,
                        about_page=out_cfg.about_page,
                    )
                )
            store.log_event("assembled", outputs=outputs, out_format=out_format)

        store.log_event(
            "run_steps_finished",
            steps=run_steps_input,
            outputs=outputs,
            qa_issue_count=len(qa_issues),
        )
        return {
            "store": store,
            "output": outputs[0] if outputs else None,
            "outputs": outputs,
            "report": report,
            "qa_issues": qa_issues,
        }

    def run_all(self, input_path: str, *, progress: Optional[ProgressFn] = None,
                out_format: str = "epub", out_path: str | None = None,
                do_qa: bool | None = None) -> dict[str, Any]:
        """翻译 → 一致性 QA → 报告 → 回填 EPUB，返回结果汇总。"""
        steps = {"translate", "report", "assemble"}
        if do_qa if do_qa is not None else self.config.pipeline.consistency_qa:
            steps.add("qa")
        return self.run_steps(input_path, steps, progress=progress,
                              out_format=out_format, out_path=out_path)
