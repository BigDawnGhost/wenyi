"""旧版流水线的章节标题与目录标题翻译协调逻辑。

这个模块只服务于旧 ``RunStore`` 任务。它保留旧 Orchestrator 的落盘、事件、
进度和续跑语义，但不依赖新版 workflow/graph，也不调用正文 ``Translator``。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..glossary.store import GlossaryStore
from .runstore import RunStore

ProgressFn = Callable[[int, int, str], None]
CompleteJsonFn = Callable[..., Any]


def translate_legacy_titles(
    store: RunStore,
    glossary: GlossaryStore,
    *,
    complete_json: CompleteJsonFn,
    source_lang: str,
    target_lang: str,
    progress: ProgressFn | None = None,
) -> None:
    """翻译旧任务的逻辑章标题和 NCX/NAV 目录节点并写回 manifest。

    目录节点若已定位到正文 heading Segment，直接复用完整译文，
    使正文与目录严格一致；其它标题再分批调用标题翻译器。每批立即
    落盘，续跑只处理尚未完成的项。书名始终保持原文。
    """
    from ..agents import prompts

    # 读取旧 manifest，并只接受旧格式中有效的字典型目录项。
    m = store.load_manifest()
    chapters = m.get("chapters", [])

    # 标题压成单行，避免内嵌换行破坏 numbered 对齐。
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

    # 扫描每章可翻译文本，构造完整 heading 的源文与译文索引。
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

    # 优先把正文 heading 的完整译文复用到对应目录项。
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

    # 目录译名先同步给其对应的逻辑章节。
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

    # 仅把尚无译名且不能复用正文译文的记录加入模型翻译队列。
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

    # heading 复用结果必须先落盘；全量已完成时记录跳过事件并短路。
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

    # 每个批次都保持“调用、校验、写回、同步、保存、事件、进度”的旧顺序。
    completed = 0
    glossary_text = prompts.render_glossary(glossary.all_terms())
    for batch_index, batch in enumerate(batches):
        titles = [str(item["source"]) for item in batch]
        system = prompts.render(
            "title_translator_system",
            src=source_lang,
            tgt=target_lang,
            n=len(titles),
        )
        user = prompts.render(
            "title_translator_user",
            src=source_lang,
            tgt=target_lang,
            glossary=glossary_text,
            n=len(titles),
            numbered_titles=prompts.numbered(titles),
        )
        try:
            data = complete_json(
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
                {"source": source, "target": target} for source, target in zip(titles, translated)
            ],
        )
        completed += len(batch)
        if progress:
            progress(completed, len(pending), "翻译章节标题")


__all__ = ["translate_legacy_titles"]
