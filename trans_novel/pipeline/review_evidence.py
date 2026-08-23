"""Review Agent Loop 使用的只读全书证据索引。"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from threading import Lock
from typing import Any

from ..glossary.store import GlossaryTerm, source_matches_text, term_match_sources
from ..ingest.models import Chapter


def _normalized(value: str) -> str:
    """统一兼容字符、宽度和大小写，供术语/建议键比较。"""
    return unicodedata.normalize("NFKC", value).casefold().strip()


def _clip(value: Any, limit: int) -> str:
    """把术语字段限制在证据消息可控的长度内。"""
    return str(value or "")[:limit]


def _glossary_ref(source: str) -> str:
    """为术语条目生成不泄露任意字符到 ID 的稳定引用。"""
    digest = sha256(source.encode("utf-8")).hexdigest()[:16]
    return f"glossary:{digest}"


@dataclass(frozen=True)
class SegmentRef:
    """全书中的一个可审校段落及其稳定位置。"""

    global_ordinal: int
    chapter: int
    text_index: int
    segment_index: int
    source: str
    target: str
    baseline_target: str
    target_origin: str
    chapter_title: str

    @property
    def ref(self) -> str:
        """返回可在调试输出和 Agent 证据中引用的稳定 ID。"""
        return f"ch{self.chapter}:text{self.text_index}:seg{self.segment_index}"

    def compact(self) -> dict[str, Any]:
        """序列化为证据载荷。"""
        limit = 4000
        payload = {
            "ref": self.ref,
            "chapter": self.chapter,
            "text_index": self.text_index,
            "segment_index": self.segment_index,
            "chapter_title": self.chapter_title,
            "source": self.source[:limit],
            "target": self.target[:limit],
            "target_origin": self.target_origin,
            "source_truncated": len(self.source) > limit,
            "target_truncated": len(self.target) > limit,
        }
        if self.target_origin == "shadow_override":
            payload.update(
                {
                    "baseline_target": self.baseline_target[:limit],
                    "baseline_target_truncated": len(self.baseline_target) > limit,
                }
            )
        return payload


class BookEvidenceIndex:
    """以查询驱动方式提供跨章上下文和第 N 次术语出现证据。"""

    def __init__(
        self,
        chapters: list[Chapter],
        terms: list[GlossaryTerm],
        analysis: dict[str, Any],
        *,
        target_overrides: Mapping[tuple[int, int], str] | None = None,
    ):
        """构建全书只读证据索引。

        ``target_overrides`` 以 ``(chapter.index, text_index)`` 为键，为指定
        段落提供仅在本索引中生效的影子译文。未覆盖的位置仍读取章节中的正式
        译文，因此默认调用方式及持久化数据均不受影响。
        """
        flattened: list[SegmentRef] = []
        by_location: dict[tuple[int, int], int] = {}
        chapter_digests: dict[int, str] = {}
        overrides = target_overrides if target_overrides is not None else {}
        for chapter in chapters:
            chapter_digests[chapter.index] = str(chapter.meta.get("source_digest", "") or "")
            for text_index, segment in enumerate(chapter.text_segments):
                position = len(flattened)
                location = (chapter.index, text_index)
                baseline_target = segment.target or ""
                has_override = location in overrides
                flattened.append(
                    SegmentRef(
                        global_ordinal=position,
                        chapter=chapter.index,
                        text_index=text_index,
                        segment_index=segment.index,
                        source=segment.source,
                        target=overrides.get(location, baseline_target),
                        baseline_target=baseline_target,
                        target_origin=("shadow_override" if has_override else "formal"),
                        chapter_title=chapter.title,
                    )
                )
                by_location[location] = position

        self.segments = tuple(flattened)
        self._by_location = by_location
        self.terms = tuple(terms)
        self.analysis = dict(analysis)
        self.chapter_digests = chapter_digests
        self._exact_source_lookup: dict[str, GlossaryTerm] = {}
        self._source_lookup: dict[str, list[GlossaryTerm]] = {}
        self._alias_lookup: dict[str, list[GlossaryTerm]] = {}
        for term in self.terms:
            self._exact_source_lookup[term.source] = term
            normalized_source = _normalized(term.source)
            if normalized_source:
                self._source_lookup.setdefault(normalized_source, []).append(term)
            for alias in term.aliases:
                normalized_alias = _normalized(alias)
                if normalized_alias:
                    self._alias_lookup.setdefault(normalized_alias, []).append(term)
        self._occurrence_cache: dict[str, tuple[SegmentRef, ...]] = {}
        self._cache_lock = Lock()

    def segment_ref(self, chapter: int, text_index: int) -> SegmentRef | None:
        """按章号和 text_segments 下标返回段落引用。"""
        position = self._by_location.get((chapter, text_index))
        return self.segments[position] if position is not None else None

    def canonical_term(self, query: str) -> tuple[GlossaryTerm | None, list[str]]:
        """把 source/alias 解析为规范术语；歧义时返回所有候选 source。"""
        stripped = query.strip()
        exact = self._exact_source_lookup.get(stripped)
        if exact is not None:
            return exact, []
        normalized = _normalized(stripped)
        source_matches = self._source_lookup.get(normalized, [])
        unique_sources = {term.source: term for term in source_matches}
        if len(unique_sources) == 1:
            return next(iter(unique_sources.values())), []
        if len(unique_sources) > 1:
            return None, sorted(unique_sources)

        matches = self._alias_lookup.get(normalized, [])
        unique = {term.source: term for term in matches}
        if len(unique) == 1:
            return next(iter(unique.values())), []
        if len(unique) > 1:
            return None, sorted(unique)
        return None, []

    def _occurrences(self, query: str) -> tuple[str, tuple[SegmentRef, ...], list[str]]:
        """懒扫描并缓存一个规范术语或字面短语的命中段落。"""
        term, ambiguous = self.canonical_term(query)
        if ambiguous:
            return "", (), ambiguous
        canonical = term.source if term is not None else query.strip()
        if not canonical:
            return "", (), []
        cache_key = (
            f"term:{term.source}" if term is not None else f"literal:{_normalized(canonical)}"
        )
        with self._cache_lock:
            cached = self._occurrence_cache.get(cache_key)
        if cached is not None:
            return canonical, cached, []

        keys = term_match_sources(term) if term is not None else [canonical]
        found = tuple(
            segment
            for segment in self.segments
            if any(source_matches_text(key, segment.source) for key in keys)
        )
        with self._cache_lock:
            existing = self._occurrence_cache.setdefault(cache_key, found)
        return canonical, existing, []

    @staticmethod
    def _selector_positions(selectors: list[Any], total: int) -> tuple[list[int], list[Any]]:
        """把 1-based/first/middle/last 选择器转为去重后的零基位置。"""
        positions: list[int] = []
        invalid: list[Any] = []
        for selector in selectors[:8]:
            position: int | None = None
            if isinstance(selector, int) and not isinstance(selector, bool):
                position = selector - 1
            elif selector == "first":
                position = 0
            elif selector == "middle":
                position = (total - 1) // 2 if total else None
            elif selector == "last":
                position = total - 1 if total else None
            else:
                invalid.append(selector)
            if position is None or not 0 <= position < total:
                if selector not in invalid:
                    invalid.append(selector)
                continue
            if position not in positions:
                positions.append(position)
        return positions, invalid

    def _context(self, position: int, before: int, after: int) -> list[dict[str, Any]]:
        """按全书连续顺序返回跨章上下文。"""
        start = max(0, position - before)
        end = min(len(self.segments), position + after + 1)
        return [segment.compact() for segment in self.segments[start:end]]

    def term_occurrences(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """选择性返回一个术语在全书中的第 N 个命中段落。"""
        query = arguments.get("term")
        if not isinstance(query, str) or not query.strip() or len(query) > 128:
            return {"ok": False, "error": "invalid_term"}
        selectors = arguments.get("selectors", ["first", "middle", "last"])
        if not isinstance(selectors, list) or not selectors or len(selectors) > 8:
            return {"ok": False, "error": "invalid_selectors"}
        radius = arguments.get("context_radius", 0)
        if isinstance(radius, bool) or not isinstance(radius, int) or not 0 <= radius <= 2:
            return {"ok": False, "error": "invalid_context_radius"}

        term, _ = self.canonical_term(query)
        canonical, occurrences, ambiguous = self._occurrences(query)
        if ambiguous:
            return {
                "ok": False,
                "error": "ambiguous_term",
                "candidates": ambiguous,
            }
        positions, invalid = self._selector_positions(selectors, len(occurrences))
        selected = []
        for position in positions:
            segment = occurrences[position]
            selected.append(
                {
                    "ordinal": position + 1,
                    **segment.compact(),
                    "context": self._context(segment.global_ordinal, radius, radius),
                }
            )
        return {
            "ok": True,
            "canonical_term": canonical,
            "glossary_term": self._term_evidence(term) if term is not None else None,
            "total_matches": len(occurrences),
            "selected": selected,
            "invalid_selectors": invalid,
        }

    @staticmethod
    def _term_evidence(term: GlossaryTerm) -> dict[str, Any]:
        """把单个术语压缩为可引用的只读证据。"""
        return {
            "ref": _glossary_ref(term.source),
            "source": _clip(term.source, 256),
            "target": _clip(term.target, 256),
            "reading": _clip(term.reading, 256),
            "type": _clip(term.type, 64),
            "gender": _clip(term.gender, 64),
            "aliases": [_clip(alias, 256) for alias in term.aliases[:16]],
            "first_chapter": term.first_chapter,
            "note": _clip(term.note, 2000),
        }

    def glossary_term(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """按 source 或 alias 返回一个规范术语条目，不允许枚举全表。"""
        query = arguments.get("term")
        if not isinstance(query, str) or not query.strip() or len(query) > 128:
            return {"ok": False, "error": "invalid_term"}
        term, ambiguous = self.canonical_term(query)
        if ambiguous:
            return {
                "ok": False,
                "error": "ambiguous_term",
                "candidates": ambiguous,
            }
        if term is None:
            return {"ok": False, "error": "term_not_found"}
        return {
            "ok": True,
            "term": self._term_evidence(term),
        }

    def segment_context(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """返回指定段落附近的跨章原译文。"""
        chapter = arguments.get("chapter")
        text_index = arguments.get("index")
        before = arguments.get("before", 2)
        after = arguments.get("after", 2)
        if (
            isinstance(chapter, bool)
            or not isinstance(chapter, int)
            or isinstance(text_index, bool)
            or not isinstance(text_index, int)
            or isinstance(before, bool)
            or not isinstance(before, int)
            or isinstance(after, bool)
            or not isinstance(after, int)
        ):
            return {"ok": False, "error": "invalid_segment_context_arguments"}
        if not 0 <= before <= 6 or not 0 <= after <= 6:
            return {"ok": False, "error": "context_limit_exceeded"}
        position = self._by_location.get((chapter, text_index))
        if position is None:
            return {"ok": False, "error": "segment_not_found"}
        return {
            "ok": True,
            "center": self.segments[position].ref,
            "segments": self._context(position, before, after),
        }

    def book_context(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """按 section 返回有限的书级或章节级分析信息。"""
        section = arguments.get("section")
        if section == "style_guide":
            value = self.analysis.get("style_guide", "")
        elif section == "book_synopsis":
            value = self.analysis.get("book_synopsis", "")
        elif section == "chapter_digest":
            chapter = arguments.get("chapter")
            if isinstance(chapter, bool) or not isinstance(chapter, int):
                return {"ok": False, "error": "invalid_chapter"}
            if chapter not in self.chapter_digests:
                return {"ok": False, "error": "chapter_not_found"}
            value = self.chapter_digests.get(chapter, "")
        else:
            return {"ok": False, "error": "invalid_book_context_section"}
        text = str(value or "")
        limit = 6000
        return {
            "ok": True,
            "ref": (
                f"book:chapter_digest:ch{arguments['chapter']}"
                if section == "chapter_digest"
                else f"book:{section}"
            ),
            "section": section,
            "value": text[:limit],
            "truncated": len(text) > limit,
        }

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        """验证并执行一个 JSON 证据请求，始终返回结构化结果。"""
        request_id = request.get("request_id")
        tool = request.get("tool")
        arguments = request.get("arguments", {})
        if not isinstance(request_id, str) or not request_id.strip():
            return {"request_id": "", "ok": False, "error": "invalid_request_id"}
        if not isinstance(arguments, dict):
            return {"request_id": request_id, "ok": False, "error": "invalid_arguments"}
        if not isinstance(tool, str):
            return {
                "request_id": request_id,
                "tool": tool,
                "ok": False,
                "error": "unknown_tool",
            }
        handlers = {
            "glossary_term": self.glossary_term,
            "term_occurrences": self.term_occurrences,
            "segment_context": self.segment_context,
            "book_context": self.book_context,
        }
        handler = handlers.get(tool)
        if handler is None:
            return {
                "request_id": request_id,
                "tool": tool,
                "ok": False,
                "error": "unknown_tool",
            }
        result = {"request_id": request_id, "tool": tool, **handler(arguments)}
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > 64_000:
            return {
                "request_id": request_id,
                "tool": tool,
                "ok": False,
                "error": "evidence_result_too_large",
                "hint": "减少 selectors、context_radius、before 或 after 后重试。",
            }
        return result

    @staticmethod
    def evidence_refs(value: Any) -> set[str]:
        """递归收集证据载荷中的稳定 ref。"""
        refs: set[str] = set()
        if isinstance(value, dict):
            ref = value.get("ref")
            if isinstance(ref, str):
                refs.add(ref)
            for child in value.values():
                refs.update(BookEvidenceIndex.evidence_refs(child))
        elif isinstance(value, list):
            for child in value:
                refs.update(BookEvidenceIndex.evidence_refs(child))
        return refs
