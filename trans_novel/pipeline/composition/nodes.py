"""Construction of built-in workflow nodes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from trans_novel.config import Config
from trans_novel.llm.base import LLMClient
from trans_novel.pipeline.composition.context import RunContext
from trans_novel.pipeline.contracts import BatchCommitHook, ExecutionGoal
from trans_novel.pipeline.nodes import (
    AnalyzeNode,
    AssembleNode,
    DeterministicQANode,
    MineTermsNode,
    NameTermsNode,
    PolishNode,
    PrepareNode,
    RepairNode,
    ReportNode,
    TitlesNode,
    TranslateNode,
)
from trans_novel.pipeline.state import (
    NODE_ANALYZE,
    NODE_ASSEMBLE,
    NODE_DETERMINISTIC_QA,
    NODE_MINE_TERMS,
    NODE_NAME_TERMS,
    NODE_POLISH,
    NODE_PREPARE,
    NODE_REPAIR,
    NODE_REPORT,
    NODE_TITLES,
    NODE_TRANSLATE,
)


def build_node_factory(
    client: LLMClient,
    config: Config,
    shared: RunContext,
    goal: ExecutionGoal,
    batch_commit_hook: BatchCommitHook | None = None,
):
    def _style() -> str:
        return shared.style_brief()

    builders: dict[str, Callable[[RunContext, int | None], Any]] = {
        NODE_PREPARE: lambda shared, ci: PrepareNode(client=client, config=config, doc=shared.doc),
        NODE_ANALYZE: lambda shared, ci: AnalyzeNode(
            analyzer=shared.agents.analyzer,
            config=config,
            doc=shared.doc,
            glossary=shared.glossary(),
            frozen_book=shared.frozen_book(),
        ),
        NODE_MINE_TERMS: lambda shared, ci: MineTermsNode(
            miner=shared.agents.miner, config=config, frozen_book=shared.frozen_book()
        ),
        NODE_NAME_TERMS: lambda shared, ci: NameTermsNode(
            namer=shared.agents.namer,
            analyzer=shared.agents.analyzer,
            glossary=shared.glossary(),
            config=config,
            frozen_book=shared.frozen_book(),
        ),
        NODE_TRANSLATE: lambda shared, ci: TranslateNode(
            translator=shared.agents.translator,
            extractor=shared.agents.extractor,
            polisher=shared.agents.polisher,
            glossary=shared.glossary(),
            config=config,
            style_brief=_style(),
            rolling_context=shared.rolling_context(),
            frozen_book=shared.frozen_book(),
            frozen_preparation=shared.frozen_preparation,
            batch_commit_hook=batch_commit_hook,
        ),
        NODE_POLISH: lambda shared, ci: PolishNode(
            polisher=shared.agents.polisher,
            extractor=shared.agents.extractor,
            glossary=shared.glossary(),
            config=config,
            style_brief=_style(),
            frozen_book=shared.frozen_book(),
            frozen_preparation=shared.frozen_preparation,
        ),
        NODE_TITLES: lambda shared, ci: TitlesNode(
            client=client,
            config=config,
            src=shared.agents.src,
            tgt=shared.agents.tgt,
            glossary=shared.glossary(),
        ),
        NODE_DETERMINISTIC_QA: lambda shared, ci: DeterministicQANode(glossary=shared.glossary()),
        NODE_REPAIR: lambda shared, ci: RepairNode(
            translator=shared.agents.translator,
            glossary=shared.glossary(),
            style_brief=_style(),
            config=config,
        ),
        NODE_REPORT: lambda shared, ci: ReportNode(glossary=shared.glossary()),
        NODE_ASSEMBLE: lambda shared, ci: AssembleNode(
            config=config,
            out_format=goal.out_format,
            out_path=goal.out_path,
        ),
    }

    def factory(node_id: str, ci: int | None):
        builder = builders.get(node_id)
        if builder is None:
            raise KeyError(f"未注册节点构造器: {node_id}")
        return builder(shared, ci)

    return factory


__all__ = ["build_node_factory"]
