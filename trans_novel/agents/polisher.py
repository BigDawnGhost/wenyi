"""润色 Agent（强档）。

在审校通过的直译稿上做中文文学性二次加工：不增删信息、保持段数不变。
对齐失败（段数不符）时保守地返回原译文，绝不因润色而引入漏译。
"""

from __future__ import annotations

from ..config import Config
from ..glossary.store import GlossaryTerm
from ..llm.base import LLMClient
from . import prompts


class Polisher:
    def __init__(self, client: LLMClient, config: Config):
        self.client = client
        self.config = config
        self.src = config.source_lang
        self.tgt = config.target_lang

    def polish(self, targets: list[str], *, glossary_terms: list[GlossaryTerm] | None = None,
               style: str = "") -> list[str]:
        if not targets:
            return []
        glossary_terms = glossary_terms or []
        n = len(targets)
        system = prompts.render("polisher_system", src=self.src, tgt=self.tgt, n=n)
        user = prompts.render(
            "polisher_user", src=self.src, tgt=self.tgt,
            glossary=prompts.render_glossary(glossary_terms),
            style=style or "（无）", n=n,
            numbered_target=prompts.numbered(targets),
        )
        try:
            data = self.client.complete_json(
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                tier="strong",
            )
        except Exception:
            return list(targets)
        items = data.get("polished") if isinstance(data, dict) else data
        if isinstance(items, list) and len(items) == n:
            return [str(x) for x in items]
        return list(targets)  # 段数不符 → 保守保留原译
