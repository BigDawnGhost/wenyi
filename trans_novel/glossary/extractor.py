"""术语抽取 Agent（廉价档）+ 入库（含冲突记录）。

每翻完一章，从"原文 + 译文"里抽取应进表的专有名词，
依据实际译法入库；不同译法由 GlossaryStore.upsert_term 记录，等待人工裁决。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, replace

from ..agents import prompts
from ..agents.base import Agent
from .store import GlossaryStore, GlossaryTerm, source_matches_text


def _text(value: object, default: str = "") -> str:
    """把模型返回的标量字段规整为字符串。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return default


@dataclass(frozen=True)
class TranslatedSegmentEvidence:
    """一个已翻译段落及其书内位置，供新术语回查首次译法。"""

    chapter: int
    segment: int
    source: str
    target: str


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

    @staticmethod
    def _first_occurrences(
        terms: list[GlossaryTerm],
        store: GlossaryStore,
        history: Iterable[TranslatedSegmentEvidence],
        before: tuple[int, int],
    ) -> dict[str, TranslatedSegmentEvidence]:
        """找到尚未入库术语在指定位置之前的首个已译段落。"""
        pending = {
            term.source
            for term in terms
            if store.get_term(term.source) is None
        }
        if not pending:
            return {}

        first: dict[str, TranslatedSegmentEvidence] = {}
        ordered_history = sorted(
            history, key=lambda item: (item.chapter, item.segment)
        )
        for evidence in ordered_history:
            if (evidence.chapter, evidence.segment) >= before:
                continue
            for source in pending:
                if source in first:
                    continue
                if source_matches_text(source, evidence.source):
                    first[source] = evidence
            if len(first) == len(pending):
                break
        return first

    def _align_with_first_occurrences(
        self,
        terms: list[GlossaryTerm],
        occurrences: dict[str, TranslatedSegmentEvidence],
    ) -> tuple[list[GlossaryTerm], int, int]:
        """用首次译文校准候选译名；无法可靠判定的历史命中项暂不入库。"""
        if not occurrences:
            return terms, 0, 0

        candidates = []
        for term in terms:
            evidence = occurrences.get(term.source)
            if evidence is None:
                continue
            candidates.append({
                "source": term.source,
                "proposed_target": term.target,
                "first_occurrence": {
                    "chapter": evidence.chapter,
                    "segment": evidence.segment,
                    "source": evidence.source,
                    "target": evidence.target,
                },
            })

        system = prompts.render(
            "glossary_history_system", src=self.src, tgt=self.tgt
        )
        user = prompts.render(
            "glossary_history_user",
            src=self.src,
            tgt=self.tgt,
            candidates_json=json.dumps(candidates, ensure_ascii=False, indent=2),
        )
        raw = self._ask_json(
            system, user, tier="fast", key="terms", default=[]
        )
        resolved = {
            source: target
            for item in self.dict_items(raw)
            if (source := _text(item.get("source"))) in occurrences
            and (target := _text(item.get("target")))
        }

        aligned: list[GlossaryTerm] = []
        unresolved = 0
        for term in terms:
            if term.source not in occurrences:
                aligned.append(term)
                continue
            target = resolved.get(term.source)
            if not target:
                unresolved += 1
                continue
            aligned.append(replace(term, target=target))
        return aligned, len(resolved), unresolved

    def extract_and_store(
        self,
        store: GlossaryStore,
        source_text: str,
        target_text: str,
        chapter: int,
        *,
        history: Iterable[TranslatedSegmentEvidence] = (),
        before: tuple[int, int] | None = None,
    ) -> dict[str, int]:
        """抽取术语并入库；新术语优先沿用其首次历史译法。

        ``history`` 仅包含已有译文证据。若新术语在 ``before`` 位置之前出现，
        会先用首次出现的原译文校准 target；历史证据无法判定时暂不入库，
        避免把后出的候选译名锁定并污染后文。
        """
        existing = store.all_terms()
        terms = self.extract(source_text, target_text, existing)
        occurrences = (
            self._first_occurrences(terms, store, history, before)
            if before is not None
            else {}
        )
        terms, aligned, unresolved = self._align_with_first_occurrences(
            terms, occurrences
        )
        summary = {
            "inserted": 0,
            "conflict": 0,
            "unchanged": 0,
            "history_matched": len(occurrences),
            "history_aligned": aligned,
            "history_unresolved": unresolved,
        }
        for t in terms:
            evidence = occurrences.get(t.source)
            t.first_chapter = evidence.chapter if evidence is not None else chapter
            result = store.upsert_term(t, chapter=chapter)
            summary[result] = summary.get(result, 0) + 1
        return summary
