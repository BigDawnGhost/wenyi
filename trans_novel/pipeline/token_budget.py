"""翻译命令启动前的纯本地 Token 预算估算。

这里刻意不复用运行中的 :mod:`eta` 估算器：ETA 依赖真实 LLM usage 样本，
而预算确认必须发生在第一次模型请求之前。本模块只读取源文、已有断点和配置，
用稳定的启发式模型给出“本次新增”用量及不确定区间，不写运行状态。
"""

from __future__ import annotations

import os
import unicodedata
from dataclasses import dataclass
from math import ceil
from pathlib import Path

from ..config import Config
from ..ingest.models import Chapter, Segment
from ..ingest.segmenter import batch_segments, load_document
from .runstore import STATUS_DONE, RunStore, slugify


@dataclass(frozen=True)
class StageTokenEstimate:
    """一个必定执行阶段的预计 Token 用量。"""

    stage: str
    prompt_tokens: int
    completion_tokens: int
    calls: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class TranslationTokenEstimate:
    """供 CLI 展示的不可变翻译前预算。"""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    lower_total_tokens: int
    upper_total_tokens: int
    source_characters: int
    pending_characters: int
    pending_batches: int
    calls: int
    resumed: bool
    basis: str
    stages: tuple[StageTokenEstimate, ...]
    conditional_notes: tuple[str, ...] = ()


@dataclass
class _MutableStage:
    prompt: float = 0.0
    completion: float = 0.0
    calls: float = 0.0

    def add(self, prompt: float, completion: float, calls: float = 1.0) -> None:
        self.prompt += max(0.0, prompt)
        self.completion += max(0.0, completion)
        self.calls += max(0.0, calls)


@dataclass(frozen=True)
class _BudgetInput:
    chapters: tuple[Chapter, ...]
    manifest: dict
    analysis: dict
    store: RunStore | None
    resumed: bool
    basis: str


def estimate_text_tokens(text: str) -> int:
    """在不绑定 provider tokenizer 的前提下估算一段 Unicode 文本的 Token 数。

    中日韩文字通常接近一字一 token；ASCII 单词约四字符一 token；其它字母文字
    使用更保守的 2.5 字符一 token。标点也会占 token，但多个空白只计很小成本。
    """

    total = 0.0
    ascii_run = 0
    other_letter_run = 0

    def flush_runs() -> None:
        nonlocal total, ascii_run, other_letter_run
        if ascii_run:
            total += ceil(ascii_run / 4)
            ascii_run = 0
        if other_letter_run:
            total += ceil(other_letter_run / 2.5)
            other_letter_run = 0

    for char in text:
        codepoint = ord(char)
        if char.isascii() and char.isalnum():
            if other_letter_run:
                flush_runs()
            ascii_run += 1
            continue
        if (
            0x3400 <= codepoint <= 0x9FFF
            or 0x3040 <= codepoint <= 0x30FF
            or 0xAC00 <= codepoint <= 0xD7AF
        ):
            flush_runs()
            total += 1.0
            continue
        category = unicodedata.category(char)
        if category.startswith(("L", "N")):
            if ascii_run:
                flush_runs()
            other_letter_run += 1
            continue
        flush_runs()
        if char.isspace():
            total += 0.03
        else:
            total += 0.35

    flush_runs()
    return max(1, ceil(total)) if text else 0


def _new_pdf_chapters(path: str) -> tuple[tuple[Chapter, ...], str]:
    """不调用 MinerU，仅用 PDF 文本层为首次运行建立预算输入。"""

    from pypdf import PdfReader

    reader = PdfReader(path)
    chapters: list[Chapter] = []
    extracted_any = False
    for index, page in enumerate(reader.pages):
        text = (page.extract_text() or "").strip()
        if text:
            extracted_any = True
        else:
            # 扫描版无法在本地知道 OCR 结果；按小说排版的中位页字符数兜底。
            text = "文" * 1800
        chapters.append(
            Chapter(
                index=index,
                title=f"第 {index + 1} 页",
                segments=[Segment(index=0, source=text)],
            )
        )
    basis = "PDF 文本层" if extracted_any else "PDF 页数粗估（扫描版）"
    return tuple(chapters), basis


def _load_budget_input(input_path: str, config: Config) -> _BudgetInput:
    """只读加载源文或已有断点；首次 PDF 不触发 MinerU 网络转换。"""

    ext = os.path.splitext(input_path)[1].lower()
    if ext == ".pdf":
        title = Path(input_path).stem
        candidate = RunStore(os.path.join(config.state_dir, slugify(title)), create=False)
        if candidate.exists():
            manifest = candidate.load_manifest()
            chapters = tuple(
                candidate.load_chapter(item["index"])
                for item in manifest.get("chapters", [])
                if isinstance(item.get("index"), int)
            )
            return _BudgetInput(
                chapters=chapters,
                manifest=manifest,
                analysis=candidate.load_analysis() or {},
                store=candidate,
                resumed=True,
                basis="已有断点",
            )
        chapters, basis = _new_pdf_chapters(input_path)
        return _BudgetInput(
            chapters=chapters,
            manifest={
                "source_lang": config.source_lang,
                "meta": {},
                "chapters": [
                    {"index": chapter.index, "status": "pending"} for chapter in chapters
                ],
            },
            analysis={},
            store=None,
            resumed=False,
            basis=basis,
        )

    document = load_document(
        input_path,
        config.source_lang,
        config.target_lang,
        split_segments=config.segment.max_chars_per_segment,
    )
    candidate = RunStore(
        os.path.join(config.state_dir, slugify(document.title)),
        create=False,
    )
    if candidate.exists():
        manifest = candidate.load_manifest()
        chapters = tuple(
            candidate.load_chapter(item["index"])
            for item in manifest.get("chapters", [])
            if isinstance(item.get("index"), int)
        )
        return _BudgetInput(
            chapters=chapters,
            manifest=manifest,
            analysis=candidate.load_analysis() or {},
            store=candidate,
            resumed=True,
            basis="已有断点",
        )
    return _BudgetInput(
        chapters=tuple(document.chapters),
        manifest={
            "source_lang": document.source_lang,
            "meta": document.meta,
            "chapters": [
                {
                    "index": chapter.index,
                    "title": chapter.title,
                    "status": "pending",
                }
                for chapter in document.chapters
            ],
        },
        analysis={},
        store=None,
        resumed=False,
        basis="本地解析源文",
    )


def _stage(stages: dict[str, _MutableStage], name: str) -> _MutableStage:
    return stages.setdefault(name, _MutableStage())


def _target_tokens(source_tokens: int) -> int:
    """估算目标译文长度；跨语种小说翻译通常与源文 token 量同阶。"""

    return max(1, ceil(source_tokens * 0.95))


def _review_chunks(chapter: Chapter, budget: int) -> list[list[Segment]]:
    return batch_segments(chapter.text_segments, budget)


def _manifest_chapter_map(manifest: dict) -> dict[int, dict]:
    return {
        item["index"]: item
        for item in manifest.get("chapters", [])
        if isinstance(item, dict) and isinstance(item.get("index"), int)
    }


def estimate_translation_tokens(
    input_path: str,
    config: Config,
    *,
    only_chapter: int | None = None,
    include_review: bool = False,
    include_qa: bool = False,
) -> TranslationTokenEstimate:
    """估算 ``translate`` 命令从当前断点起会新增的 Token 用量。

    估算覆盖配置决定的必经调用；模型重试、翻译对齐恢复和审校发现问题后才触发的
    Agent/Fix/盲审不冒充确定成本，而是通过更宽的上界和提示明确表达。
    """

    source = _load_budget_input(input_path, config)
    chapter_by_index = {chapter.index: chapter for chapter in source.chapters}
    if only_chapter is not None and only_chapter not in chapter_by_index:
        available = sorted(chapter_by_index)
        valid_range = f"0–{available[-1]}" if available else "无可翻译章节"
        raise ValueError(f"章节编号 {only_chapter} 不存在；可用范围：{valid_range}")

    manifest_chapters = _manifest_chapter_map(source.manifest)
    if only_chapter is not None:
        target_indices = [only_chapter]
    elif source.resumed:
        target_indices = [
            index
            for index in sorted(chapter_by_index)
            if manifest_chapters.get(index, {}).get("status") != STATUS_DONE
        ]
    else:
        target_indices = sorted(chapter_by_index)

    stages: dict[str, _MutableStage] = {}
    all_source_text = "\n".join(
        segment.source for chapter in source.chapters for segment in chapter.text_segments
    )
    source_characters = sum(
        len(segment.source) for chapter in source.chapters for segment in chapter.text_segments
    )

    # 新任务初始化：语言识别与风格分析都发生在正文翻译之前。
    if not source.resumed:
        if config.source_lang in ("auto", "", None):
            sample = all_source_text[:1500]
            _stage(stages, "语言识别").add(estimate_text_tokens(sample) + 180, 20)
        style_sample_tokens = estimate_text_tokens(all_source_text[:8400])
        if style_sample_tokens:
            _stage(stages, "风格与初始术语").add(style_sample_tokens + 350, 800)

    # 全书预扫是正文翻译的必经准备；已有逐章梗概和概览均按断点跳过。
    if config.pipeline.book_understanding:
        digest_outputs = 0
        for chapter in source.chapters:
            if chapter.meta.get("source_digest"):
                continue
            chapter_text = "\n".join(segment.source for segment in chapter.text_segments)[:8000]
            chapter_tokens = estimate_text_tokens(chapter_text)
            completion = min(600, max(120, ceil(chapter_tokens * 0.08)))
            _stage(stages, "全书预扫").add(chapter_tokens + 250, completion)
            digest_outputs += completion
        if not source.analysis.get("book_synopsis") and source.chapters:
            # 超长书的 map-reduce 可能多于一次调用；按每 12k 摘要字符一组外推。
            digest_tokens = digest_outputs or max(120, len(source.chapters) * 180)
            groups = max(1, ceil(digest_tokens / 6000))
            synopsis_calls = groups + (1 if groups > 1 else 0)
            synopsis_prompt = digest_tokens + synopsis_calls * 350
            synopsis_completion = synopsis_calls * 700
            _stage(stages, "全书预扫").add(
                synopsis_prompt,
                synopsis_completion,
                synopsis_calls,
            )

    pending_characters = 0
    pending_batches = 0
    predicted_targets: dict[tuple[int, int], int] = {}
    for chapter in source.chapters:
        for segment in chapter.text_segments:
            actual = (segment.target or "").strip()
            predicted_targets[(chapter.index, segment.index)] = (
                estimate_text_tokens(actual)
                if actual
                else _target_tokens(estimate_text_tokens(segment.source))
            )

    for chapter_index in target_indices:
        chapter = chapter_by_index[chapter_index]
        batches = batch_segments(chapter.text_segments, config.segment.max_chars_per_batch)
        batch_start = 0
        glossary_done = (
            source.store.completed_batch_glossary_keys(chapter_index)
            if source.store is not None
            else set()
        )
        for batch in batches:
            batch_source = "\n".join(segment.source for segment in batch)
            source_tokens = estimate_text_tokens(batch_source)
            target_tokens = sum(
                predicted_targets[(chapter.index, segment.index)] for segment in batch
            )
            translated = all(segment.target and segment.target.strip() for segment in batch)
            glossary_key = RunStore.batch_glossary_key(batch_start, len(batch))
            if not translated:
                batch_chars = sum(len(segment.source) for segment in batch)
                pending_characters += batch_chars
                pending_batches += 1
                context_tokens = min(900, ceil(source_tokens * 0.55))
                _stage(stages, "正文翻译").add(
                    source_tokens + context_tokens + 650,
                    target_tokens,
                )
                if config.pipeline.polish:
                    _stage(stages, "文学润色").add(target_tokens + 400, target_tokens)
                _stage(stages, "术语抽取").add(
                    source_tokens + target_tokens + 350,
                    min(500, max(80, ceil(source_tokens * 0.10))),
                )
            elif glossary_key not in glossary_done:
                _stage(stages, "术语抽取").add(
                    source_tokens + target_tokens + 350,
                    min(500, max(80, ceil(source_tokens * 0.10))),
                )

            if config.pipeline.annotation_alignment:
                for segment in batch:
                    metadata = segment.meta.get("epub_annotations")
                    if not isinstance(metadata, dict) or not metadata.get("items"):
                        continue
                    already_aligned = bool(metadata.get("target_digest") and metadata.get("placements"))
                    if translated and already_aligned:
                        continue
                    unit_source = estimate_text_tokens(segment.source)
                    unit_target = predicted_targets[(chapter.index, segment.index)]
                    _stage(stages, "EPUB 注释定位").add(
                        unit_source + unit_target + 300,
                        min(350, max(80, ceil((unit_source + unit_target) * 0.08))),
                    )
            batch_start += len(batch)

        if chapter.text_segments:
            chapter_source = sum(estimate_text_tokens(s.source) for s in chapter.text_segments)
            chapter_target = sum(
                predicted_targets[(chapter.index, segment.index)]
                for segment in chapter.text_segments
            )
            _stage(stages, "术语抽取").add(
                chapter_source + chapter_target + 400,
                min(750, max(120, ceil(chapter_source * 0.08))),
            )

            sample_rate = config.pipeline.backtranslate_sample
            if sample_rate > 0:
                sample_fraction = min(1.0, max(0.0, sample_rate))
                sample_source = chapter_source * sample_fraction
                sample_target = chapter_target * sample_fraction
                _stage(stages, "回译抽检").add(
                    sample_target + 250,
                    sample_source,
                )
                _stage(stages, "回译抽检").add(
                    sample_source * 2 + 250,
                    min(450, max(80, sample_source * 0.08)),
                )

    # 正文完成后未复用的章标题/目录标题会分批翻译；成本很小但仍计入。
    if only_chapter is None:
        pending_titles = [
            str(manifest_chapters.get(chapter.index, {}).get("title") or chapter.title).strip()
            for chapter in source.chapters
            if not manifest_chapters.get(chapter.index, {}).get("title_translated")
            and str(manifest_chapters.get(chapter.index, {}).get("title") or chapter.title).strip()
        ]
        for offset in range(0, len(pending_titles), 50):
            title_tokens = estimate_text_tokens("\n".join(pending_titles[offset : offset + 50]))
            _stage(stages, "标题翻译").add(title_tokens + 220, max(20, title_tokens))

    if include_review and only_chapter is None:
        review_budget = config.segment.max_chars_per_batch * 3
        for chapter in source.chapters:
            for chunk in _review_chunks(chapter, review_budget):
                source_tokens = sum(estimate_text_tokens(segment.source) for segment in chunk)
                target_tokens = sum(
                    predicted_targets[(chapter.index, segment.index)] for segment in chunk
                )
                _stage(stages, "全书审校 R1").add(
                    source_tokens + target_tokens + 550,
                    min(1200, max(120, ceil(source_tokens * 0.08))),
                )

    if include_qa and only_chapter is None and source.chapters:
        qa_prompt = 400 + sum(
            min(
                350,
                sum(predicted_targets[(chapter.index, segment.index)] for segment in chapter.text_segments),
            )
            for chapter in source.chapters
        )
        _stage(stages, "一致性 QA").add(qa_prompt, min(1000, max(150, qa_prompt * 0.08)))

    stage_results = tuple(
        StageTokenEstimate(
            stage=name,
            prompt_tokens=ceil(value.prompt),
            completion_tokens=ceil(value.completion),
            calls=ceil(value.calls),
        )
        for name, value in stages.items()
        if value.prompt > 0 or value.completion > 0
    )
    prompt_tokens = sum(item.prompt_tokens for item in stage_results)
    completion_tokens = sum(item.completion_tokens for item in stage_results)
    total_tokens = prompt_tokens + completion_tokens

    notes = ["模型重试、翻译对齐恢复不会预先计入，发生时会增加实际用量"]
    upper_factor = 1.40
    if include_review and only_chapter is None and (
        config.pipeline.review_agent_loop or config.pipeline.review_fix_loop
    ):
        notes.append("审校取证、冲突仲裁、影子修订和盲审仅在触发时追加")
        upper_factor = 1.70
    if source.basis.startswith("PDF 页数"):
        notes.append("扫描版 PDF 无文本层，预算按页数推算，误差会更大")
        upper_factor = max(upper_factor, 1.85)

    return TranslationTokenEstimate(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        lower_total_tokens=max(0, int(total_tokens * 0.70)),
        upper_total_tokens=max(total_tokens, ceil(total_tokens * upper_factor)),
        source_characters=source_characters,
        pending_characters=pending_characters,
        pending_batches=pending_batches,
        calls=sum(item.calls for item in stage_results),
        resumed=source.resumed,
        basis=source.basis,
        stages=stage_results,
        conditional_notes=tuple(notes),
    )
