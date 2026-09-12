"""润色 Agent：按检查点批量校验模型响应，并保留逐项回退原因。"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any

from trans_novel.agents import langprofile, prompts
from trans_novel.agents.base import Agent, WorkflowProtocolError, retry_protocol
from trans_novel.glossary.store import GlossaryTerm


@dataclass
class PolishResult:
    texts: list[str]
    fallback_reasons: dict[int, str]
    proposals: dict[int, str] = field(default_factory=dict)
    locked_terms: tuple[GlossaryTerm, ...] = ()


class _PolishProtocolError(WorkflowProtocolError):
    def __init__(self, result: PolishResult):
        super().__init__("polish_protocol_invalid")
        self.result = result


def _batch_fallback(
    targets: list[str],
    eligible: set[int],
    locked_terms: tuple[GlossaryTerm, ...],
) -> PolishResult:
    return PolishResult(
        texts=targets.copy(),
        fallback_reasons=dict.fromkeys(eligible, "polish_batch_invalid"),
        locked_terms=locked_terms,
    )


def _parse_response(
    data: Any,
    targets: list[str],
    eligible: set[int],
    locked_terms: tuple[GlossaryTerm, ...],
) -> PolishResult:
    if not isinstance(data, dict) or not isinstance(data.get("polished"), list):
        return _batch_fallback(targets, eligible, locked_terms)

    by_id: dict[int, Any] = {}
    for item in data["polished"]:
        if not isinstance(item, dict):
            return _batch_fallback(targets, eligible, locked_terms)
        item_id = item.get("id")
        if type(item_id) is not int or item_id not in eligible or item_id in by_id:
            return _batch_fallback(targets, eligible, locked_terms)
        by_id[item_id] = item.get("text")

    texts = targets.copy()
    reasons: dict[int, str] = {}
    proposals: dict[int, str] = {}
    for index in eligible:
        if index not in by_id:
            reasons[index] = "polish_item_missing"
        elif not isinstance(by_id[index], str) or not by_id[index].strip():
            reasons[index] = "polish_item_invalid"
        else:
            proposals[index] = by_id[index]
            texts[index] = by_id[index].strip()
    return PolishResult(
        texts=texts,
        fallback_reasons=reasons,
        proposals=proposals,
        locked_terms=locked_terms,
    )


class Polisher(Agent):
    def polish(
        self,
        targets: list[str],
        sources: list[str],
        *,
        glossary_terms: list[GlossaryTerm] | None = None,
        style: str = "",
        source_context: str = "",
        chapter_title: str = "",
        strict: bool = False,
    ) -> PolishResult:
        if len(targets) != len(sources):
            raise ValueError("polish source/target count mismatch")
        term_snapshot = tuple(copy.deepcopy(glossary_terms or ()))

        eligible = {
            index for index, source in enumerate(sources) if langprofile.needs_translation(source)
        }
        texts = [
            target if index in eligible else sources[index] for index, target in enumerate(targets)
        ]
        if not eligible:
            return PolishResult(
                texts=texts,
                fallback_reasons={},
                locked_terms=term_snapshot,
            )

        system = prompts.render("polisher_system", src=self.src, tgt=self.tgt)
        items = [
            {"id": index, "source": sources[index], "target": targets[index]}
            for index in sorted(eligible)
        ]
        user = prompts.render(
            "polisher_user",
            src=self.src,
            tgt=self.tgt,
            glossary=prompts.render_glossary(list(term_snapshot)),
            style=style or "（无）",
            source_context=source_context or "（无）",
            chapter_title=chapter_title or "（无）",
            polish_items=json.dumps(items, ensure_ascii=False),
        )

        def ask() -> PolishResult:
            try:
                data = self._ask_json(
                    system,
                    user,
                    agent="editor",
                    operation="polish.batch",
                    strict=True,
                )
            except WorkflowProtocolError as exc:
                raise _PolishProtocolError(_batch_fallback(texts, eligible, term_snapshot)) from exc
            result = _parse_response(data, texts, eligible, term_snapshot)
            if result.fallback_reasons:
                raise _PolishProtocolError(result)
            return result

        try:
            return (
                retry_protocol(ask, retries=self.config.pipeline.protocol_retry_limit)
                if strict
                else ask()
            )
        except _PolishProtocolError as exc:
            return exc.result
