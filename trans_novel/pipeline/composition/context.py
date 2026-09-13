"""Shared per-run coordination state."""

from __future__ import annotations

import threading
from typing import Any

from trans_novel.config import Config
from trans_novel.glossary.store import GlossaryStore
from trans_novel.pipeline.composition.agents import AgentBundle
from trans_novel.pipeline.state import RollingContext


class RunContext:
    """一轮运行的共享**协调**状态：解析后的文档、惰性解析的 Agent、术语库连接、
    滚动上下文、进度计数、润色 future 与并行层锁。

    具体节点不再通过本类型获取 Agent/语言/术语库等具体依赖（那些走构造注入）；
    runner 只按不透明对象传递本类型，节点仅消费协调状态（计数/锁/在途 future）。
    """

    def __init__(
        self,
        *,
        store,
        config: Config,
        doc,
        agent_builder,
        frozen_preparation=None,
        output=None,
        output_digest: str | None = None,
        theme=None,
        theme_bundle=None,
        layout_inventory=None,
        layout_profile=None,
        output_format: str = "epub",
        output_relevant: bool = False,
        identity_languages: tuple[str, str] = ("", ""),
    ):
        self.store = store
        self.config = config
        self.doc = doc
        self._agent_builder = agent_builder
        self.frozen_preparation = frozen_preparation
        self.output = output if output is not None else config.output
        self.output_ready = False
        self.output_digest = output_digest
        self.theme = theme
        self.theme_bundle = theme_bundle
        self.layout_inventory = layout_inventory
        self.layout_profile = layout_profile
        self.output_format = output_format
        self.output_relevant = output_relevant
        self.identity_languages = identity_languages
        self.resolved_source_lang: str | None = None  # prepare 解析后写入
        self._agents: AgentBundle | None = None
        self._context: RollingContext | None = None
        self._glossary: GlossaryStore | None = None
        self._style_brief: str | None = None
        self._frozen_book = None
        self.segments_done = 0
        self.segments_total = 0
        # 并行层（digest ∥ mine_terms）节点在 worker 线程写 RunStore：锁串行化
        # 读改写，避免并发 save_state 互相覆盖（仓库既有约定：RunStore 读写主线程，
        # 并行场景退化为共享锁保护）。
        self.store_lock = threading.Lock()
        # translate 提交的润色 future（批起点 → Future），polish 节点排干时取用。
        self.polish_futures: dict[tuple[int, int], Any] = {}

    def frozen_book(self):
        if self.frozen_preparation is None:
            return None
        if self._frozen_book is None:
            book_id = ""
            source_sha256 = ""
            if self.store.exists():
                state = self.store.load_state()
                book_id = str(
                    state.meta.get("benchmark_book_id") or state.meta.get("book_id") or state.title
                )
                source_sha256 = str(state.meta.get("source_sha256") or "")
                if not source_sha256:
                    source_sha256 = state.identity.source_bytes_sha256
            if not book_id and self.doc is not None:
                book_id = str(
                    self.doc.meta.get("benchmark_book_id")
                    or self.doc.meta.get("book_id")
                    or self.doc.title
                )
            if not source_sha256 and self.doc is not None:
                source_sha256 = str(self.doc.meta.get("source_sha256") or "")
            if not source_sha256 and self.doc is not None:
                from trans_novel.pipeline.state import source_bytes_hash

                source_sha256 = source_bytes_hash(self.doc.source_path)
            self._frozen_book = self.frozen_preparation.book_for(
                book_id=book_id,
                source_sha256=source_sha256,
            )
        return self._frozen_book

    def frozen_chapter_index(self, chapter_index: int) -> int:
        """Resolve a synthetic chapter to its single original chapter."""
        if self.frozen_preparation is None:
            return chapter_index
        chapter = next(
            (item for item in getattr(self.doc, "chapters", []) if item.index == chapter_index),
            None,
        )
        chapter_meta = getattr(chapter, "meta", {})
        chapter_value = (
            chapter_meta.get("original_chapter_index") if isinstance(chapter_meta, dict) else None
        )
        document_meta = getattr(self.doc, "meta", {})
        mapping = (
            document_meta.get("continuous_chapter_mapping", {})
            if isinstance(document_meta, dict)
            else {}
        )
        mapped_value = mapping.get(str(chapter_index)) if isinstance(mapping, dict) else None
        if (
            chapter_value is not None
            and mapped_value is not None
            and str(chapter_value) != str(mapped_value)
        ):
            raise ValueError(f"ambiguous frozen chapter mapping: {chapter_index}")
        value = chapter_value if chapter_value is not None else mapped_value
        if value is None:
            return chapter_index
        try:
            resolved = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"invalid frozen chapter mapping: {chapter_index}") from error
        if resolved < 0:
            raise ValueError(f"invalid frozen chapter mapping: {chapter_index}")
        return resolved

    @property
    def agents(self) -> AgentBundle:
        if self._agents is None:
            src = self.resolved_source_lang or self.config.source_lang
            tgt = self.config.target_lang
            if self.store.exists():
                state = self.store.load_state()
                src = (
                    self.resolved_source_lang
                    or state.identity.source_lang
                    or state.source_lang
                    or src
                )
                tgt = state.identity.target_lang or state.target_lang or tgt
            self._agents = self._agent_builder(src, tgt)
        return self._agents

    def style_brief(self) -> str:
        if self._style_brief is None:
            if self.frozen_preparation is not None:
                book = self.frozen_book()
                self._style_brief = book.style_brief
            else:
                self._style_brief = self.agents.analyzer.style_brief(
                    self.store.load_analysis() or {}
                )
        return self._style_brief

    def rolling_context(self) -> RollingContext:
        if self._context is None:
            self._context = RollingContext.from_dict(
                self.store.load_context() or {},
                min_recent_keep=max(40, self.config.pipeline.rolling_context_segments),
            )
        return self._context

    def glossary(self) -> GlossaryStore:
        if self._glossary is None:
            self._glossary = GlossaryStore(self.store.glossary_path)
        return self._glossary

    def close(self) -> None:
        if self._glossary is not None:
            self._glossary.close()
            self._glossary = None


__all__ = ["RunContext"]
