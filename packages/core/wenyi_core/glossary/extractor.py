"""术语抽取 Agent（廉价档）+ 入库（含冲突记录）。

每翻完一章，从"原文 + 译文"里抽取应进表的专有名词，
依据实际译法入库；不同译法由 GlossaryStore.upsert_term 记录，等待人工裁决。
"""

from __future__ import annotations

from ..agents import prompts
from ..agents.base import Agent
from ..storage import Storage
from .store import GlossaryTerm


def _text(value: object, default: str = "") -> str:
    """把模型返回的标量字段规整为字符串。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return default


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

    def extract_and_store(self, storage: Storage, source_text: str,
                          target_text: str, chapter: int) -> dict[str, int]:
        """抽取术语并写入数据库，返回新增、冲突和未变化数量。"""
        existing = storage.all_terms()
        terms = self.extract(source_text, target_text, existing)
        summary = {"inserted": 0, "conflict": 0, "unchanged": 0}
        for t in terms:
            t.first_chapter = chapter
            result = storage.upsert_term(t, chapter=chapter)
            summary[result] = summary.get(result, 0) + 1
        return summary
