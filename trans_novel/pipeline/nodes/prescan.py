"""Best-effort terminology mining and naming nodes."""

from __future__ import annotations

from trans_novel.agents.base import WorkflowProtocolError
from trans_novel.agents.term_miner import TermMiner
from trans_novel.config import Config
from trans_novel.glossary.store import TYPE_PERSON, GlossaryStore
from trans_novel.pipeline.contracts import NodeOutcome, NodeRequest
from trans_novel.pipeline.planning.fingerprints import (
    analyst_model_profile,
    fast_model_profile,
    frozen_input_fingerprint,
    mine_terms_input_fingerprint,
    name_terms_input_fingerprint,
)
from trans_novel.pipeline.state import (
    NODE_MINE_TERMS,
    NODE_NAME_TERMS,
    SCOPE_BOOK,
)


def _import_frozen_glossary(glossary: GlossaryStore, frozen_book) -> None:
    for term in frozen_book.glossary:
        existing = glossary.get_term(term.source)
        if existing is not None and existing != term:
            raise ValueError(f"frozen glossary conflict: {term.source}")
        if existing is None:
            glossary.upsert_term(term)


class MineTermsNode:
    node_id = NODE_MINE_TERMS
    scope = SCOPE_BOOK

    def __init__(self, *, miner: TermMiner, config: Config, frozen_book=None):
        self.miner, self.config, self.frozen_book = miner, config, frozen_book

    def execute(self, request: NodeRequest) -> NodeOutcome:
        store = request.store
        if self.frozen_book is not None:
            fp = frozen_input_fingerprint(
                request.shared.frozen_preparation.preparation_sha256,
                self.node_id,
                self.frozen_book.book_id,
                [t.source for t in self.frozen_book.glossary],
            )
            return NodeOutcome(fingerprint=fp, artifacts={"candidates": []})
        state = store.load_state()
        src_chapters = [
            (c.index, "\n".join(s.source for s in store.load_chapter(c.index).text_segments))
            for c in state.chapters
            if c.processing is None or c.processing.action != "preserve"
        ]
        on_progress = (
            (lambda i, n: request.progress(i, n, "查找专有名词…")) if request.progress else None
        )
        candidates = self.miner.mine(
            src_chapters,
            concurrency=max(1, self.config.pipeline.prescan_concurrency),
            on_progress=on_progress,
        )
        fp = mine_terms_input_fingerprint(
            [text for _, text in src_chapters],
            self.miner.src,
            self.config.pipeline.prescan_concurrency,
            fast_model_profile(self.config),
        )
        return NodeOutcome(fingerprint=fp, artifacts={"candidates": candidates})


class NameTermsNode:
    node_id = NODE_NAME_TERMS
    scope = SCOPE_BOOK

    def __init__(
        self, *, namer, analyzer, glossary: GlossaryStore, config: Config, frozen_book=None
    ):
        self.namer, self.analyzer, self.glossary = namer, analyzer, glossary
        self.config, self.frozen_book = config, frozen_book

    def execute(self, request: NodeRequest) -> NodeOutcome:
        store = request.store
        if self.frozen_book is not None:
            _import_frozen_glossary(self.glossary, self.frozen_book)
            state = store.load_state()
            state.analysis_flags.term_mining_done = True
            store.save_state(state)
            fp = frozen_input_fingerprint(
                request.shared.frozen_preparation.preparation_sha256,
                self.node_id,
                self.frozen_book.book_id,
                [t.source for t in self.frozen_book.glossary],
            )
            return NodeOutcome(fingerprint=fp, artifacts={"named_terms": []})
        candidates = (request.artifacts.get("mine_terms") or {}).get("candidates")
        if candidates is None:
            raise WorkflowProtocolError("missing_mine_candidates")
        state = store.load_state()
        mine_node = state.nodes.get(NODE_MINE_TERMS)
        mine_fingerprint = mine_node.input_fingerprint if mine_node else ""
        if not mine_fingerprint:
            raise WorkflowProtocolError("missing_mine_fingerprint")
        style = self.analyzer.style_brief(store.load_analysis() or {})
        named = self.namer.name_terms(
            candidates,
            style,
            existing=self.glossary.all_terms(),
            concurrency=max(1, self.config.pipeline.prescan_concurrency),
            on_progress=(lambda i, n: request.progress(i, n, "统一译名…"))
            if request.progress
            else None,
        )
        for term in named:
            self.glossary.upsert_term(term, chapter=0)
            if term.type == TYPE_PERSON:
                self.glossary.confirm_locked(term.source, term.target)
        state.analysis_flags.term_mining_done = True
        store.save_state(state)
        fp = name_terms_input_fingerprint(
            mine_fingerprint,
            style,
            self.config.pipeline.prescan_concurrency,
            analyst_model_profile(self.config),
        )
        return NodeOutcome(fingerprint=fp, artifacts={"named_terms": named})


__all__ = [
    "MineTermsNode",
    "NameTermsNode",
    "name_terms_input_fingerprint",
]
