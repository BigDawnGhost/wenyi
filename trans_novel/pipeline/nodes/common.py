"""翻译节点共用的窗口构造、状态恢复与检查辅助。

所有节点共用同一套段落匹配与裁剪口径，避免维护第二套逻辑。
"""

from __future__ import annotations

from trans_novel.epub.slots import normalize_slot_transport, target_slot_transport
from trans_novel.glossary.store import GlossaryStore, terms_matching_text
from trans_novel.ingest.models import Segment
from trans_novel.ingest.segmenter import batch_segments
from trans_novel.pipeline.quality import lint_targets
from trans_novel.pipeline.state import STATUS_DONE, RollingContext, stable_digest
from trans_novel.postprocess.punct import normalize_zh


def chapter_term_snapshot(glossary: GlossaryStore, text_segs, config) -> list:
    """返回当前章节要注入的术语快照；实时入库后可重新调用刷新。"""
    terms = glossary.all_terms()
    if config.pipeline.glossary_scope != "chapter":
        return terms
    src_text = "\n".join(s.source for s in text_segs)
    return terms_matching_text(terms, src_text)


def resume_batches(segments, max_chars: int) -> list[list]:
    """按字符预算分批后，再按“已完成/待翻译”的状态边界拆分（断点续跑不重翻）。"""
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


def count_segments(store, chapter_indices: list[int]) -> int:
    total = 0
    for ci in chapter_indices:
        total += len(store.load_chapter(ci).text_segments)
    return total


def source_context_before(segments: list[Segment], index: int) -> str:
    """只取当前段之前的两段源文，每段最多保留末尾 1200 字符。"""
    paragraphs = []
    for offset in range(max(0, index - 2), index):
        source = segments[offset].source
        if len(source) > 1200:
            source = "[Earlier source truncated]\n" + source[-1200:]
        paragraphs.append(f"[Source paragraph {offset}]\n{source}")
    return "\n\n".join(paragraphs)


def seed_chapter_context(context: RollingContext, store, chapter_index: int) -> None:
    """仅从紧邻的已完成前章重建历史，不信任缓存中可能残留的后文。"""
    state = store.load_state()
    preceding = []
    for chapter in state.chapters:
        if chapter.index == chapter_index:
            break
        preceding.append(chapter.index)
    recent = []
    for index in reversed(preceding):
        progress = state.progress.get(index)
        if progress is None or progress.status != STATUS_DONE:
            break
        committed = []
        for segment in store.load_chapter(index).text_segments:
            if not segment.target or not segment.target.strip():
                break
            committed.append(segment.target)
        recent.extend(reversed(committed[-(context.max_recent_keep - len(recent)) :]))
        if len(recent) >= context.max_recent_keep:
            break
    context.recent_targets = list(reversed(recent))


def normalize_batch(batch, raw_targets, *, punctuation_normalize):
    """按现有标点策略处理译文，不修改滚动历史。"""
    if not punctuation_normalize:
        return raw_targets, False
    for segment in batch:
        if segment.epub_state is None:
            target = segment.target or ""
            if target != segment.source:
                segment.assign_translation(normalize_zh(target))
        else:
            segment.assign_translation(
                normalize_slot_transport(
                    segment.epub_state, target_slot_transport(segment.epub_state)
                )
            )
    return [s.target or "" for s in batch], True


def record_lint(
    sources,
    targets,
    chapter,
    seg_base,
    term_snapshot,
    lint_issues,
    *,
    src_lang,
    issues=None,
    store,
):
    """记录批次检查结果，保持事件与章节问题清单一致。"""
    if issues is None:
        issues = lint_targets(
            sources,
            targets,
            locked_terms=[t for t in term_snapshot if getattr(t, "locked", 0)],
            src_lang=src_lang,
        )
    if not issues:
        return
    payload = [
        {"index": seg_base + it.index, "type": it.type, "detail": it.detail} for it in issues
    ]
    type_counts: dict[str, int] = {}
    for it in issues:
        type_counts[it.type] = type_counts.get(it.type, 0) + 1
        lint_issues.append(
            {
                "chapter": chapter,
                "index": seg_base + it.index,
                "type": it.type,
                "detail": it.detail,
                "stage": "lint",
                "fixed": False,
            }
        )
    if store is not None:
        store.log_event(
            "batch_linted",
            chapter=chapter,
            start_index=seg_base,
            issue_count=len(issues),
            by_type={t: type_counts[t] for t in sorted(type_counts)},
            issues_sha256=stable_digest(payload),
        )
