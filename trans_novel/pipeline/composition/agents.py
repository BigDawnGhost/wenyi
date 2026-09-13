"""Production agent bundle constructed for a resolved language pair."""

from __future__ import annotations

from trans_novel.agents.analyzer import Analyzer
from trans_novel.agents.chapter_classifier import ChapterClassifier
from trans_novel.agents.glossary_extractor import GlossaryExtractor
from trans_novel.agents.layout_analyzer import LayoutAnalyzer
from trans_novel.agents.namer import CastNamer
from trans_novel.agents.polisher import Polisher
from trans_novel.agents.term_miner import TermMiner
from trans_novel.agents.translator import Translator
from trans_novel.config import Config
from trans_novel.llm.base import LLMClient


class AgentBundle:
    """一组按已解析语言对构造的生产 Agent。"""

    def __init__(self, *, client: LLMClient, config: Config, src: str, tgt: str):
        self.client = client
        self.config = config
        self.src = src
        self.tgt = tgt
        self.analyzer = Analyzer(client, config, src=src, tgt=tgt)
        self.classifier = ChapterClassifier(client, config, src=src, tgt=tgt)
        self.layout_analyzer = LayoutAnalyzer(client, config, src=src, tgt=tgt)
        self.translator = Translator(client, config, src=src, tgt=tgt)
        self.polisher = Polisher(client, config, src=src, tgt=tgt)
        self.extractor = GlossaryExtractor(client, config, src=src, tgt=tgt)
        self.miner = TermMiner(client, config, src=src, tgt=tgt)
        self.namer = CastNamer(client, config, src=src, tgt=tgt)


__all__ = ["AgentBundle"]
