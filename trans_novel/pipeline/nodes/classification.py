"""全章分类覆盖、缓存与持久化。"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from trans_novel.agents.chapter_classifier import ChapterClassifier, ChapterObservation
from trans_novel.ingest.models import (
    CHAPTER_SEMANTICS_VERSION,
    Chapter,
    ChapterProcessing,
    Document,
    chapter_source_digest,
)

_CACHE_SCHEMA_VERSION = 1
_MAX_SOURCE_CHARS = 16_000
_CACHE_NAME = "chapter_classification.json"


class _CachedChunk(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    index: int
    source_sha256: str = Field(min_length=1)
    result: ChapterObservation


class _CachedChapter(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    chapter_id: int
    source_sha256: str = Field(min_length=1)
    chunks: list[_CachedChunk] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ordered_unique_chunks(self) -> _CachedChapter:
        indices = [chunk.index for chunk in self.chunks]
        if indices != list(range(len(indices))):
            raise ValueError("cached chunk indices must be consecutive")
        if any(chunk.result.chapter_id != self.chapter_id for chunk in self.chunks):
            raise ValueError("cached observation chapter ID mismatch")
        return self


class _ClassificationCache(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1]
    strategy_version: Literal["chapter_semantics_v1"]
    source_fingerprint: str = Field(min_length=1)
    configured_analyst_models: list[str] = Field(min_length=1)
    chapters: dict[str, _CachedChapter] = Field(default_factory=dict)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _source_fingerprint(chapters: list[Chapter]) -> str:
    payload = [[chapter.index, chapter_source_digest(chapter)] for chapter in chapters]
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return _sha256(canonical)


def _source_chunks(chapter: Chapter) -> list[str]:
    chunks: list[str] = []
    current = ""
    for segment_index, segment in enumerate(chapter.segments):
        paragraph = ("\n\n" if segment_index else "") + segment.source
        if len(current) + len(paragraph) <= _MAX_SOURCE_CHARS:
            current += paragraph
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(paragraph) > _MAX_SOURCE_CHARS:
            chunks.append(paragraph[:_MAX_SOURCE_CHARS])
            paragraph = paragraph[_MAX_SOURCE_CHARS:]
        current = paragraph
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk.strip()]


def _load_cache(path: str, chapters: list[Chapter]) -> _ClassificationCache | None:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as stream:
            cache = _ClassificationCache.model_validate(json.load(stream))
    except (OSError, ValueError, TypeError) as error:
        raise ValueError("章节分类缓存损坏；请创建新的状态目录重新翻译。") from error
    if cache.source_fingerprint != _source_fingerprint(chapters):
        raise ValueError("章节分类源证据已变化；请创建新的状态目录重新翻译。")
    expected = {str(chapter.index): chapter_source_digest(chapter) for chapter in chapters}
    if any(
        key not in expected
        or cached.chapter_id != int(key)
        or cached.source_sha256 != expected[key]
        for key, cached in cache.chapters.items()
    ):
        raise ValueError("章节分类缓存覆盖冲突；请创建新的状态目录重新翻译。")
    return cache


def _new_cache(classifier: ChapterClassifier, chapters: list[Chapter]) -> _ClassificationCache:
    return _ClassificationCache(
        schema_version=_CACHE_SCHEMA_VERSION,
        strategy_version=CHAPTER_SEMANTICS_VERSION,
        source_fingerprint=_source_fingerprint(chapters),
        configured_analyst_models=list(classifier.config.llm.models.analyst),
    )


def _complete_processing(
    chapter: Chapter, observations: list[ChapterObservation]
) -> ChapterProcessing:
    kinds = [observation.kind for observation in observations]
    if not kinds:
        action, review, reason = "translate", False, "empty chapter"
    elif all(kind == "reference_only" for kind in kinds):
        action, review = "preserve", False
        reason = "; ".join(dict.fromkeys(item.reason for item in observations))
    elif all(kind == "translatable" for kind in kinds):
        action, review = "translate", False
        reason = "; ".join(dict.fromkeys(item.reason for item in observations))
    else:
        action, review = "translate", True
        reason = "; ".join(dict.fromkeys(item.reason for item in observations))
    return ChapterProcessing(
        action=action,
        review_required=review,
        reason=reason,
        source_sha256=chapter_source_digest(chapter),
        strategy_version=CHAPTER_SEMANTICS_VERSION,
    )


def _manifest_chapter(manifest: dict[str, Any], chapter_id: int) -> dict[str, Any]:
    chapters = manifest.get("chapters")
    if not isinstance(chapters, list):
        raise ValueError("运行清单缺少章节索引")
    matches = [
        item for item in chapters if isinstance(item, dict) and item.get("index") == chapter_id
    ]
    if len(matches) != 1:
        raise ValueError(f"运行清单章节索引冲突: {chapter_id}")
    return matches[0]


def _reuse_complete(chapter: Chapter, stored: Chapter, indexed: Any) -> bool:
    chapter_decision = stored.processing
    index_decision = indexed.processing
    if chapter_decision is None and index_decision is None:
        return False
    if chapter_decision is None or index_decision is None or chapter_decision != index_decision:
        raise ValueError(f"章节分类持久化决策冲突: {chapter.index}")
    if chapter_decision.source_sha256 != chapter_source_digest(stored):
        raise ValueError(f"章节分类源摘要与已保存章节不一致: {chapter.index}")
    if chapter_decision.source_sha256 != chapter_source_digest(chapter):
        raise ValueError("章节分类源证据已变化；请创建新的状态目录重新翻译。")
    chapter.set_processing(chapter_decision)
    return True


def ensure_chapter_classification(
    doc: Document,
    store,
    classifier: ChapterClassifier,
    staged_manifest: dict[str, Any] | None = None,
    progress=None,
) -> None:
    """确保每章都有一份可持久恢复的全源文语义决策。"""
    unfinished = list(doc.chapters)
    state = store.load_state() if store.exists() else None
    if state is not None:
        indexed = {chapter.index: chapter for chapter in state.chapters}
        if set(indexed) != {chapter.index for chapter in doc.chapters}:
            raise ValueError("章节分类覆盖与运行清单不一致")
        unfinished = [
            chapter
            for chapter in doc.chapters
            if not _reuse_complete(
                chapter, store.load_chapter(chapter.index), indexed[chapter.index]
            )
        ]
    if not unfinished:
        return

    cache_path = store.path_for(_CACHE_NAME)
    cache = _load_cache(cache_path, doc.chapters) or _new_cache(classifier, doc.chapters)
    manifest = staged_manifest if staged_manifest is not None else store.load_manifest()
    for ordinal, chapter in enumerate(unfinished, 1):
        if progress:
            progress(ordinal, len(unfinished), f"分析章节语义 {ordinal}/{len(unfinished)}…")
        source_hash = chapter_source_digest(chapter)
        cached = cache.chapters.get(str(chapter.index))
        if cached is None:
            cached = _CachedChapter(chapter_id=chapter.index, source_sha256=source_hash)
            cache.chapters[str(chapter.index)] = cached
        elif cached.chapter_id != chapter.index or cached.source_sha256 != source_hash:
            raise ValueError("章节分类源证据已变化；请创建新的状态目录重新翻译。")

        chunks = _source_chunks(chapter)
        raw_hints = chapter.meta.get("semantic_hints", [])
        hints = (
            sorted({hint for hint in raw_hints if isinstance(hint, str)})
            if isinstance(raw_hints, list)
            else []
        )
        if len(cached.chunks) > len(chunks):
            raise ValueError(f"章节分类缓存覆盖冲突: {chapter.index}")
        for chunk_index, source in enumerate(chunks):
            digest = _sha256(source)
            if chunk_index < len(cached.chunks):
                if cached.chunks[chunk_index].source_sha256 != digest:
                    raise ValueError(f"章节分类缓存分块冲突: {chapter.index}:{chunk_index}")
                continue
            observation = classifier.classify(
                chapter_id=chapter.index,
                title=chapter.title[:1000],
                context=f"Consecutive source chunk {chunk_index + 1} of {len(chunks)}.",
                source=source,
                hints=hints,
            )
            cached.chunks.append(
                _CachedChunk(index=chunk_index, source_sha256=digest, result=observation)
            )
            store.write_json(cache_path, cache.model_dump(mode="json"))

        processing = _complete_processing(chapter, [item.result for item in cached.chunks])
        chapter.set_processing(processing)
        store.save_chapter(chapter)
        _manifest_chapter(manifest, chapter.index)["processing"] = processing.model_dump(
            mode="json"
        )
        if staged_manifest is None:
            store.save_manifest(manifest)


__all__ = ["ensure_chapter_classification"]
