"""编排器：驱动全流程，章级状态机 + 断点续跑。

单章流水线（每个批次内）：
  检索相关术语 → 翻译（对齐保证）→ 廉价校验(空译/长度) + 审校 →
  严重项逐段重译 → 润色 → 回写上下文
章末：回译抽检（抽样）→ 术语抽取入库 → 更新故事梗概 → 写 TM → 落盘标记 done。
"""

from __future__ import annotations

import os
import random

from ..config import Config
from ..glossary.extractor import GlossaryExtractor
from ..glossary.store import GlossaryStore
from ..llm.base import LLMClient, build_client
from ..ingest.models import Chapter
from ..ingest.segmenter import load_document, batch_segments
from ..agents.analyzer import Analyzer
from ..agents.translator import Translator
from ..agents.reviewer import Reviewer, BackTranslator
from ..agents.polisher import Polisher
from . import checks
from .context import RollingContext
from .runstore import RunStore, slugify, STATUS_DONE


class Orchestrator:
    def __init__(self, config: Config, client: LLMClient | None = None):
        self.config = config
        self.client = client or build_client(config)
        self.analyzer = Analyzer(self.client, config)
        self.translator = Translator(self.client, config)
        self.reviewer = Reviewer(self.client, config)
        self.backtrans = BackTranslator(self.client, config)
        self.polisher = Polisher(self.client, config)
        self.extractor = GlossaryExtractor(self.client, config)

    # ── 准备 / 续跑入口 ──────────────────────────────────────────────────
    def prepare(self, input_path: str) -> RunStore:
        doc = load_document(input_path, self.config.source_lang, self.config.target_lang)
        run_dir = os.path.join(self.config.state_dir, slugify(doc.title))
        store = RunStore(run_dir)
        if store.exists():
            return store  # 已有进度 → 直接续跑，不重置

        store.init_from_document(doc)
        glossary = GlossaryStore(store.glossary_path)
        sample = self._sample_text(doc)
        analysis = self.analyzer.analyze(sample) if sample else {}
        if analysis:
            self.analyzer.seed_glossary(glossary, analysis)
        store.save_analysis(analysis)
        glossary.close()
        store.save_context(RollingContext().to_dict())
        return store

    @staticmethod
    def _sample_text(doc) -> str:
        for ch in doc.chapters:
            text = "\n".join(s.source for s in ch.text_segments)
            if len(text) > 200:
                return text[:6000]
        # 兜底：拼接前几章
        joined = "\n".join(
            s.source for ch in doc.chapters[:2] for s in ch.text_segments
        )
        return joined[:6000]

    def run(self, input_path: str, *, only_chapter: int | None = None) -> RunStore:
        store = self.prepare(input_path)
        glossary = GlossaryStore(store.glossary_path)
        context = RollingContext.from_dict(store.load_context() or {})
        style = self.analyzer.style_brief(store.load_analysis() or {})

        if only_chapter is not None:
            targets = [only_chapter]
        else:
            targets = store.pending_chapters()

        try:
            for ci in targets:
                self._translate_chapter(ci, store, glossary, context, style)
                store.save_context(context.to_dict())
        finally:
            glossary.close()
        return store

    # ── 单章 ──────────────────────────────────────────────────────────────
    def _translate_chapter(self, ci: int, store: RunStore,
                           glossary: GlossaryStore, context: RollingContext,
                           style: str) -> None:
        chapter = store.load_chapter(ci)
        text_segs = chapter.text_segments
        if not text_segs:
            store.set_chapter_status(ci, STATUS_DONE)
            return

        batches = batch_segments(text_segs, self.config.segment.max_chars_per_batch)
        review_issues: list[dict] = []
        bt_samples: list = []  # (source, target) 抽样

        for batch in batches:
            sources = [s.source for s in batch]
            terms = glossary.terms_in_text("\n".join(sources))
            ctx_text = context.render(self.config.pipeline.rolling_context_segments)

            targets = self.translator.translate_batch(
                sources, glossary_terms=terms, style=style, context=ctx_text)
            for s, t in zip(batch, targets):
                s.target = t

            # 廉价校验 + 审校 → 严重项逐段重译
            severe: set[int] = {f.index for f in checks.length_flags(sources, targets)
                                if f.reason == "empty"}
            if self.config.pipeline.review:
                issues = self.reviewer.review(sources, targets, terms)
                for it in issues:
                    it["chapter"] = ci
                review_issues.extend(issues)
                severe |= Reviewer.severe_indices(issues)
            for idx in sorted(severe):
                if 0 <= idx < len(batch):
                    fixed = self.translator.translate_batch(
                        [batch[idx].source], glossary_terms=terms,
                        style=style, context=ctx_text)
                    if fixed:
                        batch[idx].target = fixed[0]
                        targets[idx] = fixed[0]

            # 润色
            if self.config.pipeline.polish:
                polished = self.polisher.polish(
                    [s.target or "" for s in batch], glossary_terms=terms, style=style)
                for s, p in zip(batch, polished):
                    s.target = p
                targets = polished

            context.add_targets(targets)

            # 回译抽检采样
            rate = self.config.pipeline.backtranslate_sample
            if rate > 0:
                for s in batch:
                    if random.random() < rate:
                        bt_samples.append((s.source, s.target or ""))

        # 回译抽检
        bt_issues: list[dict] = []
        if bt_samples:
            srcs = [a for a, _ in bt_samples]
            tgts = [b for _, b in bt_samples]
            for it in self.backtrans.check(srcs, tgts):
                it["chapter"] = ci
                bt_issues.append(it)

        # 术语抽取入库
        src_text = "\n".join(s.source for s in text_segs)
        tgt_text = "\n".join(s.target or "" for s in text_segs)
        self.extractor.extract_and_store(glossary, src_text, tgt_text, ci)

        # 翻译记忆库
        for s in text_segs:
            if s.target:
                glossary.add_tm(s.source, s.target, ci)

        # 更新故事梗概
        context.update_summary(self.client, tgt_text)

        # 记录本章问题供报告
        chapter.meta["review_issues"] = review_issues
        chapter.meta["backtranslation_issues"] = bt_issues
        store.save_chapter(chapter)
        store.set_chapter_status(ci, STATUS_DONE)
