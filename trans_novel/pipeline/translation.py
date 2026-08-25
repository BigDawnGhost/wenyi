"""翻译服务：批次续跑、章/批翻译、润色、滚动上下文、术语快照与抽取、标题翻译。

服务接收已经生成的全书概览，在调用方持有书级锁时执行正文翻译。保持章内及跨章串行，
不增加并行化。每批精确顺序：

    翻译 → 批次译文落盘 → 注释定位并落盘 → 更新上下文 → batch 事件
    → 术语抽取/checkpoint → 更新历史索引 → 下一批

章末仍执行剩余标点规范化、全章术语兜底，并用 save_chapter_with_status
原子发布正文和 done。已有译文但缺失 glossary checkpoint 时只补抽术语，不重新翻译
或覆盖译文。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ..glossary.extractor import TranslatedSegmentEvidence
from ..glossary.store import GlossaryStore
from ..ingest.epub_reader import strip_ruby_markers
from ..ingest.models import Segment
from ..ingest.segmenter import batch_segments
from ..postprocess.punct import normalize_zh_segments
from .context import RollingContext
from .docx_styles import DocxStyleService
from .runstore import STATUS_DONE, RunStore

if TYPE_CHECKING:
    from .annotations import AnnotationService
    from .runtime import PipelineRuntime

ProgressFn = Callable[[int, int, str], None]


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


class TranslationService:
    """正文翻译、术语抽取与标题/目录翻译的领域服务。"""

    def __init__(self, runtime: PipelineRuntime, annotations: AnnotationService):
        self._runtime = runtime
        self._annotations = annotations
        self._docx_styles = DocxStyleService(runtime)

    def run(
        self,
        store: RunStore,
        *,
        book_synopsis: str,
        only_chapter: int | None = None,
        progress: ProgressFn | None = None,
    ) -> RunStore:
        """恢复语言和上下文，依次翻译章节并持续保存用量与进度。

        语言恢复、only_chapter 校验与全书概览生成由调用方（编排器）完成，
        本方法在书级锁内只执行正文翻译与标题翻译。
        """
        manifest = store.load_manifest()
        glossary = GlossaryStore(store.glossary_path)
        context = RollingContext.from_dict(
            store.load_context() or {},
            min_recent_keep=max(40, self._runtime.config.pipeline.rolling_context_segments),
        )
        style = self._runtime.analyzer.style_brief(store.load_analysis() or {})

        if only_chapter is not None:
            targets = [only_chapter]
            progress_chapters = targets
        else:
            targets = store.pending_chapters()
            progress_chapters = [chapter["index"] for chapter in manifest.get("chapters", [])]

        total, done = self.progress_counts(store, progress_chapters)
        translation_history, source_corpus = self.load_translation_inputs(store)
        annotation_context_registry = store.load_annotation_contexts()
        store.log_event(
            "translate_run_started",
            only_chapter=only_chapter,
            chapters=targets,
            total_segments=total,
        )
        try:
            with self._runtime.metric_stage("translate"):
                for ci in targets:
                    done = self.translate_chapter(
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
                    self._runtime.flush_usage(store, scope="chapter")
                # 全书译完后翻译各章标题和目录项（书名保持原文，借术语表保持专名一致）
                if not store.pending_chapters():
                    self.translate_titles(store, glossary, progress=progress)
        finally:
            glossary.close()
            self._runtime.flush_usage(store, scope="translate")
        if progress and total:
            progress(total, total, "翻译完成")
        store.log_event("translate_run_finished", total_segments=total)
        return store

    @staticmethod
    def load_translation_inputs(
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
    def update_translation_history(
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

    def progress_counts(self, store: RunStore, chapter_indices: list[int]) -> tuple[int, int]:
        """按全书批次检查点计算进度，续跑从已有译文数量开始显示。

        只有整批译文齐全时才计入 done；不完整批次会整体重跑，提前计入其中
        个别已有段会导致完成数重复累加。
        """
        total = 0
        done = 0
        for ci in chapter_indices:
            segments = store.load_chapter(ci).text_segments
            total += len(segments)
            for batch in _resume_batches(
                segments, self._runtime.config.segment.max_chars_per_batch
            ):
                if all(segment.target and segment.target.strip() for segment in batch):
                    done += len(batch)
        return total, done

    def translate_chapter(
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
        annotation_contexts = self._annotations.annotation_contexts_for_segments(
            text_segs,
            annotation_context_registry,
        )

        batches = _resume_batches(text_segs, self._runtime.config.segment.max_chars_per_batch)
        label = self.chapter_progress_label(chapter.title, ci)
        # prepare() 的最后一个标签通常是“解析文档…”。续跑首批可能先恢复术语，
        # 若不在章首刷新，整个模型请求期间都会错误地显示成仍在解析源文件。
        if progress:
            progress(done, total, label)
        glossary_checkpoints = store.completed_batch_glossary_keys(ci)
        # 章首读一次术语快照供后续真译注入 prompt。glossary_scope=chapter 时按本章
        # 源文裁剪。之后仅在「术语库可能已变」且「下一批真要翻译」时惰性刷新：
        # 正常 skip（译文与术语 checkpoint 都在）不抽、不刷；缺 checkpoint 的已译
        # 批仍补抽并标记 stale，保证中途续跑不漏抽取、又不在纯 skip 上白刷整表。
        term_snapshot = self.chapter_term_snapshot(glossary, text_segs)
        term_snapshot_stale = False

        # 逐批串行：每批渲染最新上下文 → 处理 → 立即把译文并入上下文供下一批参照。
        # 不再并发，换取章内跨批的代词/术语/语气连贯。
        # 断点续跑（段/批级）：上次中断前已译完并落盘的批次，整批跳过、不重翻，只重建上下文。
        seg_base = 0  # 当前批首段的章内段号（issue 批内下标 → 章内段号）
        for b in batches:
            batch_start = seg_base
            glossary_key = store.batch_glossary_key(batch_start, len(b))
            existing_targets = [s.target for s in b if s.target and s.target.strip()]
            if len(existing_targets) == len(b):
                # 该批上次已在原位、原上下文中译完 → 复用，重建滚动上下文后跳过
                self._annotations.align_annotations_after_batch(
                    ci,
                    chapter,
                    batch_start,
                    len(b),
                    store,
                )
                self._docx_styles.align_styles_after_batch(
                    ci,
                    chapter,
                    batch_start,
                    len(b),
                    store,
                )
                context.add_targets([s.target or "" for s in b])
                self.sync_context_chapter_prefix(
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
                    # 译文在、术语 checkpoint 不在（旧状态/中断在抽取前）：补抽入库。
                    summary = self.extract_batch_glossary(
                        glossary,
                        store,
                        ci,
                        batch_start,
                        b,
                        translation_history,
                        source_corpus,
                    )
                    glossary_checkpoints.add(glossary_key)
                    term_snapshot_stale = True
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

            if term_snapshot_stale:
                term_snapshot = self.chapter_term_snapshot(glossary, text_segs)
                term_snapshot_stale = False

            ctx_text = context.render(self._runtime.config.pipeline.rolling_context_segments)
            targets = self.process_batch(
                b,
                term_snapshot,
                ctx_text,
                style,
                book_synopsis,
                chapter_digest,
                annotation_contexts=annotation_contexts[batch_start : batch_start + len(b)],
            )
            for s, t in zip(b, targets):
                s.target = t
            # 增量持久化译文，下次中断从此批之后续跑。
            store.save_chapter(chapter)
            # 只处理当前批次触及的注释逻辑段。多个注释段严格按原文顺序
            # 一段一次调用；若当前批只有超长段的前半部分，则等最后一个
            # cont 续段译完后再合并定位。
            self._annotations.align_annotations_after_batch(
                ci,
                chapter,
                batch_start,
                len(b),
                store,
            )
            self._docx_styles.align_styles_after_batch(
                ci,
                chapter,
                batch_start,
                len(b),
                store,
            )
            context.add_targets([s.target or "" for s in b])
            self.sync_context_chapter_prefix(
                context,
                text_segs,
                batch_start + len(b),
            )
            store.log_event(
                "batch_translated",
                chapter=ci,
                start_index=batch_start,
                count=len(b),
                polished=self._runtime.config.pipeline.polish,
                punctuation_normalized=self._runtime.punctuation_enabled(),
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
            seg_base += len(b)
            if progress:
                progress(done, total, label)
            # 译文落盘后再抽取术语，避免中断时术语库领先章节产物。
            self.extract_batch_glossary(
                glossary,
                store,
                ci,
                batch_start,
                b,
                translation_history,
                source_corpus,
            )
            self.update_translation_history(translation_history, ci, batch_start, b)
            glossary_checkpoints.add(glossary_key)
            # 库可能已变；延迟到下一批真译前再刷，末批之后无需再刷。
            term_snapshot_stale = True

        # 不含注释的段落在章末统一完成标点规范化。含注释逻辑段已在其
        # 最后一个续段译完时用同一函数定稿；此处重复处理是幂等的。
        if self._runtime.punctuation_enabled():
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
            self.update_translation_history(translation_history, ci, 0, text_segs)

        # 全章术语抽取入库：保留为兜底，捕捉跨段才能确认的称呼/口癖/固定表达。
        # 最终 Review 会在全书翻译完成后读取此时已经稳定的最终术语库。
        src_text = "\n".join(s.source for s in text_segs)
        tgt_text = "\n".join(s.target or "" for s in text_segs)
        chapter_glossary_summary = self._runtime.extractor.extract_and_store(
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

        store.save_chapter_with_status(chapter, STATUS_DONE)
        store.log_event(
            "chapter_done",
            chapter=ci,
            title=chapter.title,
            segment_count=len(text_segs),
        )
        return done

    def chapter_term_snapshot(self, glossary: GlossaryStore, text_segs) -> list:
        """返回当前章节要注入的术语快照；实时入库后可重新调用刷新。"""
        terms = glossary.all_terms()
        if self._runtime.config.pipeline.glossary_scope != "chapter":
            return terms
        src_text = "\n".join(s.source for s in text_segs)
        hit = {t.source for t in GlossaryStore.terms_in(terms, src_text)}
        return [t for t in terms if t.source in hit]

    @staticmethod
    def chapter_progress_label(title: str, index: int) -> str:
        """进度展示用章节名：优先用书内标题，避免内部序号与“第一章”等标题冲突。"""
        title = (title or "").strip()
        return title or f"章节 {index + 1}"

    def extract_batch_glossary(
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
        summary = self._runtime.extractor.extract_and_store(
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

    @staticmethod
    def sync_context_chapter_prefix(
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

    def translate_titles(
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
                src=self._runtime.config.source_lang,
                tgt=self._runtime.config.target_lang,
                n=len(titles),
            )
            user = prompts.render(
                "title_translator_user",
                src=self._runtime.config.source_lang,
                tgt=self._runtime.config.target_lang,
                glossary=glossary_text,
                n=len(titles),
                numbered_titles=prompts.numbered(titles),
            )
            try:
                data = self._runtime.client.complete_json(
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

    def process_batch(
        self,
        batch,
        terms,
        ctx_text: str,
        style: str,
        book_synopsis: str = "",
        chapter_digest: str = "",
        annotation_contexts: list[list[dict[str, str]]] | None = None,
    ) -> list[str]:
        """单个批次：整批翻译 → 润色。

        每段都在自身上下文里翻译，不跨位置复用译文（避免丢失语境信息）。
        全书概览/本章梗概作为恒定前缀注入，让译者把握全局。
        标点规范化在章末统一执行，以维持跨段引号状态。
        LLM 审校不在翻译批内做；全书完成后由独立 Review 阶段统一执行。
        """
        sources = [s.source for s in batch]
        targets = self._runtime.translator.translate_batch(
            sources,
            glossary_terms=terms,
            style=style,
            context=ctx_text,
            book_synopsis=book_synopsis,
            chapter_digest=chapter_digest,
            annotation_contexts=annotation_contexts,
        )
        # 模型偶发把源文注音标记〘假名〙抄进译文时剥掉。
        targets = [strip_ruby_markers(target) for target in targets]

        if self._runtime.config.pipeline.polish:
            polished = self._runtime.polisher.polish(targets, glossary_terms=terms, style=style)
            if len(polished) == len(targets):
                targets = polished

        return targets
