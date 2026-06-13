"""术语抽取 Agent（廉价档）+ 入库（含冲突裁决）。

每翻完一章，从"原文 + 译文"里抽取应进表的专有名词，
依据实际译法入库；冲突裁决由 GlossaryStore.upsert_term 完成。
"""

from __future__ import annotations

from ..config import Config
from ..llm.base import LLMClient
from ..agents import prompts
from .store import GlossaryStore, GlossaryTerm


class GlossaryExtractor:
    def __init__(self, client: LLMClient, config: Config):
        self.client = client
        self.config = config
        self.src = config.source_lang
        self.tgt = config.target_lang

    def extract(self, source_text: str, target_text: str,
                existing: list[GlossaryTerm]) -> list[GlossaryTerm]:
        system = prompts.render("glossary_extractor_system", src=self.src, tgt=self.tgt)
        user = prompts.render(
            "glossary_extractor_user", src=self.src, tgt=self.tgt,
            glossary=prompts.render_glossary(existing),
            source=source_text, target=target_text,
        )
        try:
            data = self.client.complete_json(
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                tier="cheap",
            )
        except Exception:
            return []
        raw = data.get("terms", []) if isinstance(data, dict) else (data or [])
        terms: list[GlossaryTerm] = []
        for d in raw:
            if not isinstance(d, dict) or not d.get("source") or not d.get("target"):
                continue
            terms.append(GlossaryTerm(
                source=str(d["source"]).strip(),
                target=str(d["target"]).strip(),
                reading=str(d.get("reading", "")).strip(),
                type=d.get("type", "术语"),
                gender=d.get("gender", "") if d.get("gender") not in ("未知", None) else "",
                aliases=[a for a in d.get("aliases", []) if a],
                note=d.get("note", ""),
                confidence="medium",
            ))
        return terms

    def extract_and_store(self, store: GlossaryStore, source_text: str,
                          target_text: str, chapter: int) -> dict[str, int]:
        existing = store.all_terms()
        terms = self.extract(source_text, target_text, existing)
        summary = {"inserted": 0, "updated": 0, "conflict": 0, "unchanged": 0}
        for t in terms:
            t.first_chapter = chapter
            result = store.upsert_term(t, chapter=chapter)
            summary[result] = summary.get(result, 0) + 1
        return summary
