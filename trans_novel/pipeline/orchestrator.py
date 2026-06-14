"""编排器：驱动全流程，章级状态机 + 断点续跑。

单章流水线（章内批次**串行**，逐批刷新滚动上下文；跨章亦串行传递梗概）：
  每批：渲染上下文（含前一批刚译出的译文）→ 检索术语 → 翻译（对齐保证）→
        廉价校验(空译) + 审校 → 严重项逐段重译 → 润色 → 标点规范化 →
        立即把本批译文并入滚动上下文（供下一批参照，保证连贯）。
  章末（串行）：回译抽检 → 术语抽取入库 → 写 TM → 更新故事梗概 → 落盘标记 done。

run_all：在翻译全书后接 术语 AI 审计统一 → 一致性 QA → 写报告 → 回填出 EPUB，一气呵成。
进度回调 progress(done_segments, total_segments, label) 与 UI 无关，每批完成即触发。
"""

from __future__ import annotations

import os
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..config import Config
from ..glossary.extractor import GlossaryExtractor
from ..glossary.store import GlossaryStore
from ..llm.base import LLMClient, build_client
from ..ingest.segmenter import load_document, batch_segments
from ..postprocess.punct import normalize_zh
from ..agents.analyzer import Analyzer
from ..agents.translator import Translator
from ..agents.reviewer import Reviewer, BackTranslator
from ..agents.polisher import Polisher
from . import checks
from .context import RollingContext
from .runstore import RunStore, slugify, STATUS_DONE

ProgressFn = Callable[[int, int, str], None]
EventFn = Callable[[dict], None]


def _emit(events: Optional[EventFn], ev: dict) -> None:
    if events:
        try:
            events(ev)
        except Exception:
            pass


# 语言名/代码 → ISO 639-1 两字母代码（AI 检测结果归一化）
_LANG_ALIASES = {
    "japanese": "ja", "日语": "ja", "日文": "ja", "jp": "ja", "jpn": "ja",
    "english": "en", "英语": "en", "英文": "en", "eng": "en",
    "russian": "ru", "俄语": "ru", "俄文": "ru", "rus": "ru",
    "chinese": "zh", "中文": "zh", "汉语": "zh", "zh-cn": "zh", "zho": "zh",
    "korean": "ko", "韩语": "ko", "韩文": "ko", "kor": "ko",
    "french": "fr", "法语": "fr", "法文": "fr",
    "german": "de", "德语": "de", "德文": "de",
    "spanish": "es", "西班牙语": "es", "西班牙文": "es",
}


def _normalize_lang(code: str) -> str:
    c = (code or "").strip().lower()
    if not c:
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
        self.analyzer = Analyzer(self.client, config)
        self.translator = Translator(self.client, config)
        self.reviewer = Reviewer(self.client, config)
        self.backtrans = BackTranslator(self.client, config)
        self.polisher = Polisher(self.client, config)
        self.extractor = GlossaryExtractor(self.client, config)

    # ── 语言解析 ────────────────────────────────────────────────────────────
    def _apply_language(self, lang: str) -> None:
        """把解析出的源语言应用到 config 与各 agent（auto 检测后调用）。"""
        resolved = lang or self.config.source_lang
        self.config.source_lang = resolved
        for ag in (self.analyzer, self.translator, self.reviewer,
                   self.backtrans, self.polisher, self.extractor):
            ag.src = resolved

    # ── 准备 / 续跑入口 ──────────────────────────────────────────────────
    def prepare(self, input_path: str) -> RunStore:
        # 超长段按句拆分（max_chars_per_segment），续段标 cont 供回填并回
        doc = load_document(input_path, self.config.source_lang, self.config.target_lang,
                            split_segments=self.config.segment.max_chars_per_segment)
        run_dir = os.path.join(self.config.state_dir, slugify(doc.title))
        store = RunStore(run_dir)
        if store.exists():
            return store  # 已有进度 → 直接续跑，不重置（语言在 run() 里按 manifest 应用）

        # 新建：auto 时用 AI 检测主要语言（失败回退启发式结果）
        if self.config.source_lang in ("auto", "", None):
            doc.source_lang = self._detect_language_ai(doc) or doc.source_lang
        self._apply_language(doc.source_lang)

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

    def _detect_language_ai(self, doc) -> str:
        """用 LLM 检测正文主要语言，返回 ISO 代码（如 ja/en/ru）。失败返回空串。"""
        sample = self._sample_text(doc)[:1500]
        if not sample.strip():
            return ""
        system = (
            "你是语言识别器。判断给定文本的主要自然语言，"
            '仅输出 JSON：{"language":"<ISO 639-1 两字母代码，如 ja/en/ru/ko/fr/de/zh>"}。'
        )
        try:
            data = self.client.complete_json(
                [{"role": "system", "content": system},
                 {"role": "user", "content": sample}], tier="cheap")
            code = (data.get("language") if isinstance(data, dict) else "") or ""
            return _normalize_lang(str(code))
        except Exception:
            return ""

    @staticmethod
    def _sample_text(doc) -> str:
        for ch in doc.chapters:
            text = "\n".join(s.source for s in ch.text_segments)
            if len(text) > 200:
                return text[:6000]
        joined = "\n".join(
            s.source for ch in doc.chapters[:2] for s in ch.text_segments
        )
        return joined[:6000]

    def run(self, input_path: str, *, only_chapter: int | None = None,
            progress: Optional[ProgressFn] = None,
            events: Optional[EventFn] = None) -> RunStore:
        store = self.prepare(input_path)
        manifest = store.load_manifest()
        self._apply_language(manifest.get("source_lang") or self.config.source_lang)
        glossary = GlossaryStore(store.glossary_path)
        context = RollingContext.from_dict(store.load_context() or {})
        style = self.analyzer.style_brief(store.load_analysis() or {})

        if only_chapter is not None:
            targets = [only_chapter]
        else:
            targets = store.pending_chapters()

        total = self._count_segments(store, targets)
        done = 0
        _emit(events, {"type": "step", "step": "translate", "status": "start",
                       "total_segments": total, "chapters": len(targets)})
        try:
            for ci in targets:
                done = self._translate_chapter(
                    ci, store, glossary, context, style,
                    progress=progress, events=events, done=done, total=total)
                store.save_context(context.to_dict())
                _emit(events, {"type": "chapter_done", "chapter": ci})
            # 全书译完后翻译书名与各章标题（供目录/文件名使用，借术语表保持专名一致）
            if not store.pending_chapters():
                self._translate_titles(store, glossary, events=events)
        finally:
            glossary.close()
        if progress and total:
            progress(total, total, "翻译完成")
        _emit(events, {"type": "step", "step": "translate", "status": "done"})
        return store

    @staticmethod
    def _count_segments(store: RunStore, chapter_indices: list[int]) -> int:
        total = 0
        for ci in chapter_indices:
            total += len(store.load_chapter(ci).text_segments)
        return total

    # ── 书名 / 章节标题翻译（目录与输出文件名用）──────────────────────────────
    def _translate_titles(self, store: RunStore, glossary: GlossaryStore, *,
                          events: Optional[EventFn] = None) -> None:
        """把书名 + 各章标题整体翻成中文，写回 manifest（幂等：已全部译过则跳过）。

        借术语表保证专名一致；一次调用翻译全部标题，互为上下文更连贯。
        """
        from ..agents import prompts

        m = store.load_manifest()
        chapters = m.get("chapters", [])
        if (m.get("title_translated")
                and all(c.get("title_translated") for c in chapters)):
            return  # 已译，断点续跑不重复调用

        # 标题压成单行，避免内嵌换行破坏 numbered 对齐
        def _flat(s: str) -> str:
            return " ".join((s or "").split())
        titles = [_flat(m.get("title", ""))] + [_flat(c.get("title", "")) for c in chapters]
        if not any(t.strip() for t in titles):
            return
        _emit(events, {"type": "step", "step": "titles", "status": "start"})
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
                 {"role": "user", "content": user}], tier="strong")
        except Exception:
            return
        out = data.get("titles") if isinstance(data, dict) else data
        if not isinstance(out, list) or len(out) != len(titles):
            return
        out = [str(t).strip() for t in out]
        m["title_translated"] = out[0] or m.get("title")
        for c, t in zip(chapters, out[1:]):
            c["title_translated"] = t or c.get("title")
        store.save_manifest(m)
        _emit(events, {"type": "step", "step": "titles", "status": "done",
                       "title": m["title_translated"]})

    # ── 单章 ──────────────────────────────────────────────────────────────
    def _translate_chapter(self, ci: int, store: RunStore,
                           glossary: GlossaryStore, context: RollingContext,
                           style: str, *, progress: Optional[ProgressFn] = None,
                           events: Optional[EventFn] = None,
                           done: int = 0, total: int = 0) -> int:
        chapter = store.load_chapter(ci)
        text_segs = chapter.text_segments
        if not text_segs:
            store.set_chapter_status(ci, STATUS_DONE)
            return done

        batches = batch_segments(text_segs, self.config.segment.max_chars_per_batch)
        label = f"第{ci}章 {chapter.title}"
        # 章内术语表不变：取一次快照，逐批在内存里过滤，免去逐批查库
        term_snapshot = glossary.all_terms()

        # 逐批串行：每批渲染最新上下文 → 处理 → 立即把译文并入上下文供下一批参照。
        # 不再并发，换取章内跨批的代词/术语/语气连贯。
        # 断点续跑（段/批级）：上次中断前已译完并落盘的批次，整批跳过、不重翻，只重建上下文。
        review_issues: list[dict] = list(chapter.meta.get("review_issues", []))
        bt_samples: list[tuple[str, str]] = []
        for i, b in enumerate(batches):
            if all(s.target and s.target.strip() for s in b):
                # 该批上次已在原位、原上下文中译完 → 复用，重建滚动上下文后跳过
                context.add_targets([s.target for s in b])
                done += len(b)
                if progress:
                    progress(done, total, label)
                _emit(events, {
                    "type": "batch", "chapter": ci, "batch": i,
                    "title": chapter.title, "done": done, "total": total,
                    "pairs": [{"source": s.source, "target": s.target} for s in b],
                    "issues": [], "resumed": True,
                })
                continue

            ctx_text = context.render(self.config.pipeline.rolling_context_segments)
            terms = glossary.terms_in(term_snapshot, "\n".join(s.source for s in b))
            res = self._process_batch(b, terms, ctx_text, style)
            for s, t in zip(b, res.targets):
                s.target = t
            context.add_targets(res.targets)
            for it in res.issues:
                it["chapter"] = ci
            review_issues.extend(res.issues)
            bt_samples.extend(res.bt_samples)
            done += len(b)
            if progress:
                progress(done, total, label)
            # 增量持久化：本批译文 + 累计问题落盘，下次中断从此批之后续跑
            chapter.meta["review_issues"] = review_issues
            store.save_chapter(chapter)
            # 批次级实时：原句↔译句对照 + 该批审校建议（含 fixed 标记）
            _emit(events, {
                "type": "batch", "chapter": ci, "batch": i,
                "title": chapter.title, "done": done, "total": total,
                "pairs": [{"source": s.source, "target": t}
                          for s, t in zip(b, res.targets)],
                "issues": res.issues,
            })

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

        # 翻译记忆库（仅作记录/参考，不用于跨位置复用译文）
        for s in text_segs:
            if s.target:
                glossary.add_tm(s.source, s.target, ci)

        # 更新故事梗概（跨章串行传递）
        context.update_summary(self.client, tgt_text)

        chapter.meta["review_issues"] = review_issues
        chapter.meta["backtranslation_issues"] = bt_issues
        store.save_chapter(chapter)
        store.set_chapter_status(ci, STATUS_DONE)
        return done

    _LEN_DETAIL = {
        "empty": "译文为空（疑似漏译）",
        "too_short": "译文明显偏短（疑似漏译）",
        "too_long": "译文明显偏长（疑似增译/失控）",
    }

    def _process_batch(self, batch, terms, ctx_text: str, style: str) -> _BatchResult:
        """单个批次：整批翻译 → 审校/长度校验（仅上报）→ 标点规范化。

        每段都在自身上下文里翻译，不跨位置复用译文（避免丢失语境信息）。
        问题一律 fixed=False，交人工介入。
        """
        sources = [s.source for s in batch]
        targets = self.translator.translate_batch(
            sources, glossary_terms=terms, style=style, context=ctx_text)

        issues: list[dict] = []
        if self.config.pipeline.review:
            issues = self.reviewer.review(sources, targets, terms)
        # 无成本长度校验：空译/过短/过长也作为待人工项上报
        for f in checks.length_flags(sources, targets):
            issues.append({
                "index": f.index, "type": f.reason,
                "detail": self._LEN_DETAIL.get(f.reason, f.reason),
                "suggestion": "",
            })
        for it in issues:
            it["fixed"] = False

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
    ALL_STEPS = ("translate", "audit", "qa", "report", "assemble")

    def run_steps(self, input_path: str, steps, *,
                  progress: Optional[ProgressFn] = None,
                  events: Optional[EventFn] = None,
                  out_format: str = "epub", out_path: str | None = None) -> dict[str, Any]:
        """按需执行步骤子集（可单选可全选）。steps ⊆ ALL_STEPS。"""
        from ..agents.glossary_auditor import GlossaryAuditor
        from ..agents.consistency import ConsistencyChecker
        from ..assemble.writer import assemble
        from ..assemble.report import build_report

        steps = set(steps)

        if "translate" in steps:
            store = self.run(input_path, progress=progress, events=events)
        else:
            store = self.prepare(input_path)
            m = store.load_manifest()
            self._apply_language(m.get("source_lang") or self.config.source_lang)

        glossary = GlossaryStore(store.glossary_path)
        audit_applied: list[dict] = []
        qa_issues: list[dict] = []
        report: dict[str, Any] | None = None
        try:
            if "audit" in steps:
                _emit(events, {"type": "step", "step": "audit", "status": "start"})
                audit_applied = GlossaryAuditor(self.client, self.config).audit(store, glossary)
                _emit(events, {"type": "audit", "unifications": audit_applied})
                _emit(events, {"type": "step", "step": "audit", "status": "done"})

            if "qa" in steps:
                _emit(events, {"type": "step", "step": "qa", "status": "start"})
                qa_issues = ConsistencyChecker(self.client, self.config).check(store, glossary)
                _emit(events, {"type": "qa", "issues": qa_issues})
                _emit(events, {"type": "step", "step": "qa", "status": "done"})

            if "report" in steps:
                _emit(events, {"type": "step", "step": "report", "status": "start"})
                report = build_report(store, glossary)
                report["consistency_issues"] = qa_issues
                report["glossary_unifications"] = audit_applied
                store.save_report(report)
                _emit(events, {"type": "step", "step": "report", "status": "done"})
        finally:
            glossary.close()

        out = None
        if "assemble" in steps:
            _emit(events, {"type": "step", "step": "assemble", "status": "start"})
            out = assemble(store, input_path, out_path=out_path, out_format=out_format)
            _emit(events, {"type": "step", "step": "assemble", "status": "done", "output": out})

        result = {"store": store, "output": out, "report": report,
                  "qa_issues": qa_issues, "audit": audit_applied}
        _emit(events, {"type": "done",
                       "output": out,
                       "summary": (report or {}).get("summary", {}),
                       "audit": len(audit_applied), "qa": len(qa_issues)})
        return result

    def run_all(self, input_path: str, *, progress: Optional[ProgressFn] = None,
                events: Optional[EventFn] = None,
                out_format: str = "epub", out_path: str | None = None,
                do_audit: bool | None = None, do_qa: bool | None = None) -> dict[str, Any]:
        """翻译 → 术语审计统一 → 一致性 QA → 报告 → 回填 EPUB，返回结果汇总。"""
        steps = {"translate", "report", "assemble"}
        if do_audit if do_audit is not None else self.config.glossary_audit:
            steps.add("audit")
        if do_qa if do_qa is not None else self.config.pipeline.consistency_qa:
            steps.add("qa")
        return self.run_steps(input_path, steps, progress=progress, events=events,
                              out_format=out_format, out_path=out_path)
