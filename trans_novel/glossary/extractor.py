"""术语抽取 Agent（廉价档）+ 入库（含冲突记录）。

从已经完成的"原文 + 译文"证据窗口抽取应进表的专有名词；
只有原文词和实际译法都能在窗口中定位时才入库。不同译法由
GlossaryStore.upsert_term 记录，等待人工裁决。
"""

from __future__ import annotations

import unicodedata

from ..agents import prompts
from ..agents.base import Agent
from .store import GlossaryStore, GlossaryTerm


def _text(value: object, default: str = "") -> str:
    """把模型返回的标量字段规整为字符串。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return default


def _evidence_text(value: str) -> str:
    """规整证据文本，忽略兼容字符、大小写和排版空白。"""
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def _is_grounded(
    term: GlossaryTerm,
    source_evidence: str,
    target_evidence: str,
) -> bool:
    """只接受能在已规整原译文中分别定位 source/target 的模型候选。"""
    source = _evidence_text(term.source)
    target = _evidence_text(term.target)
    return bool(
        source
        and target
        and source in source_evidence
        and target in target_evidence
    )


class GlossaryExtractor(Agent):
    def extract(self, source_text: str, target_text: str,
                existing: list[GlossaryTerm]) -> list[GlossaryTerm]:
        """从一组原译文中抽取有效术语，并清洗模型返回的字段类型。"""
        system = prompts.render("glossary_extractor_system", src=self.src, tgt=self.tgt)
        user = prompts.render(
            "glossary_extractor_user", src=self.src, tgt=self.tgt,
            glossary=prompts.render_glossary(existing),
            source=source_text, target=target_text,
        )
        raw = self._ask_json(system, user, tier="fast", key="terms", default=[])
        terms: list[GlossaryTerm] = []
        for d in self.dict_items(raw):
            source = _text(d.get("source"))
            target = _text(d.get("target"))
            if not source or not target:
                continue
            raw_aliases = d.get("aliases")
            aliases = raw_aliases if isinstance(raw_aliases, list) else []
            gender = _text(d.get("gender"))
            terms.append(GlossaryTerm(
                source=source,
                target=target,
                reading=_text(d.get("reading")),
                type=_text(d.get("type"), "术语"),
                gender="" if gender == "未知" else gender,
                aliases=[alias for a in aliases if (alias := _text(a))],
                note=_text(d.get("note")),
            ))
        return terms

    def extract_and_store(self, store: GlossaryStore, source_text: str,
                          target_text: str, chapter: int) -> dict[str, int]:
        """抽取有原译文证据的术语并入库，返回各类处理数量。"""
        # 只把当前窗口实际出现的旧词条发给模型。全量术语会随书长线性增长，
        # 既浪费 token，也会诱导模型复述与本窗口无关的条目。
        existing = store.terms_in_text(source_text)
        terms = self.extract(source_text, target_text, existing)
        source_evidence = _evidence_text(source_text)
        target_evidence = _evidence_text(target_text)
        summary = {
            "inserted": 0,
            "conflict": 0,
            "unchanged": 0,
            "rejected": 0,
            "aliases_rejected": 0,
        }
        for t in terms:
            if not _is_grounded(t, source_evidence, target_evidence):
                summary["rejected"] += 1
                continue
            grounded_aliases = [
                alias
                for alias in t.aliases
                if (normalized := _evidence_text(alias))
                and normalized in source_evidence
            ]
            summary["aliases_rejected"] += len(t.aliases) - len(grounded_aliases)
            t.aliases = grounded_aliases
            t.first_chapter = chapter
            result = store.upsert_term(t, chapter=chapter)
            summary[result] = summary.get(result, 0) + 1
        return summary
