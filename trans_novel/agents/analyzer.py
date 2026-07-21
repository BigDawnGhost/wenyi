"""全局分析 Agent（强档）。

通读样章，产出风格指南、角色圣经（含性别/语气）、初始术语候选，
并把角色/术语种入术语库，作为全书翻译的统一基准。
"""

from __future__ import annotations

from typing import Any

from .. import languages
from ..glossary.store import GlossaryStore, GlossaryTerm
from . import prompts
from .base import Agent


def _text(value: Any, default: str = "") -> str:
    """把模型字段规整为文本；嵌套对象等非标量值直接回退。"""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return default


class Analyzer(Agent):
    def analyze(self, sample_text: str) -> dict[str, Any]:
        """分析样本文本，并返回经过类型清洗的风格、角色和术语信息。"""
        system = prompts.render(
            "analyzer_system",
            src=self.src,
            tgt=self.tgt,
            honorific_strategy=self.config.honorific_strategy,
        )
        user = prompts.render("analyzer_user", src=self.src, tgt=self.tgt,
                              sample=sample_text)
        # 不传 default：分析失败照常抛出，由调用方决定（prepare 阶段失败应显式暴露）
        data = self._ask_json(system, user, tier="strong")
        if not isinstance(data, dict):
            data = {}
        for key in (
            "genre",
            "tone",
            "style_guide",
            "narration",
            "pacing",
            "register",
            "dialogue_style",
            "rhetoric",
        ):
            data[key] = _text(data.get(key))
        characters = self.dict_items(data.get("characters"))
        for character in characters:
            character["type"] = _text(
                character.get("type"), languages.default_person_type(self.tgt)
            )
            character["gender"] = _text(character.get("gender"))
            character["note"] = _text(character.get("note"))
        terms = self.dict_items(data.get("terms"))
        for term in terms:
            term["type"] = _text(
                term.get("type"), languages.default_term_type(self.tgt)
            )
            term["note"] = _text(term.get("note"))
        data["characters"] = characters
        data["terms"] = terms
        return data

    def seed_glossary(self, store: GlossaryStore, analysis: dict[str, Any]) -> int:
        """把分析得到的角色/术语种入术语库，返回写入条目数。"""
        count = 0
        for ch in self.dict_items(analysis.get("characters")):
            source = _text(ch.get("source"))
            target = _text(ch.get("target"))
            if not source or not target:
                continue
            store.upsert_term(
                GlossaryTerm(
                    source=source,
                    target=target,
                    reading=_text(ch.get("reading")),
                    type=_text(
                        ch.get("type"), languages.default_person_type(self.tgt)
                    ),
                    gender=_text(ch.get("gender")),
                    note=_text(ch.get("note")),
                    first_chapter=0,
                ),
                chapter=0,
            )
            count += 1
        for tm in self.dict_items(analysis.get("terms")):
            source = _text(tm.get("source"))
            target = _text(tm.get("target"))
            if not source or not target:
                continue
            store.upsert_term(
                GlossaryTerm(
                    source=source,
                    target=target,
                    reading=_text(tm.get("reading")),
                    type=_text(
                        tm.get("type"), languages.default_term_type(self.tgt)
                    ),
                    note=_text(tm.get("note")),
                    first_chapter=0,
                ),
                chapter=0,
            )
            count += 1
        return count

    def style_brief(self, analysis: dict[str, Any]) -> str:
        """把分析结果浓缩成给译者注入的风格/角色简报。"""
        chars = self.dict_items(analysis.get("characters"))
        return languages.style_brief(analysis, chars, target=self.tgt)
