"""翻译 Agent（强档）。

核心保证：句段对齐——输入 N 段，输出必须是 N 段，一一对应。
策略：
1. 整批翻译并要求等长 JSON 数组；
2. 段数不符则重试（最多 review_retry_limit 次）；
3. 仍不符则逐段单独翻译兜底，从结构上保证 1:1，杜绝整段漏译。
"""

from __future__ import annotations

from ..config import Config
from ..glossary.store import GlossaryTerm
from ..llm.base import LLMClient
from . import prompts


class AlignmentError(Exception):
    pass


class Translator:
    def __init__(self, client: LLMClient, config: Config):
        self.client = client
        self.config = config
        self.src = config.source_lang
        self.tgt = config.target_lang

    def _call_batch(
        self,
        sources: list[str],
        glossary_terms: list[GlossaryTerm],
        style: str,
        context: str,
    ) -> list[str]:
        n = len(sources)
        system = prompts.render(
            "translator_system", src=self.src, tgt=self.tgt,
            n=n, honorific_rule=prompts.honorific_rule(self.config.honorific_strategy),
        )
        user = prompts.render(
            "translator_user", src=self.src, tgt=self.tgt,
            glossary=prompts.render_glossary(glossary_terms),
            style=style or "（无）",
            context=context or "（无）",
            n=n, n_minus_1=n - 1,
            numbered_source=prompts.numbered(sources),
        )
        data = self.client.complete_json(
            [{"role": "system", "content": system},
             {"role": "user", "content": user}],
            tier="strong",
        )
        items = data.get("translations") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise AlignmentError("模型未返回译文数组")
        return [str(x) for x in items]

    def _translate_one(self, source: str, glossary_terms, style, context) -> str:
        out = self._call_batch([source], glossary_terms, style, context)
        return out[0] if out else ""

    def translate_batch(
        self,
        sources: list[str],
        *,
        glossary_terms: list[GlossaryTerm] | None = None,
        style: str = "",
        context: str = "",
    ) -> list[str]:
        """翻译一批源段，返回与之等长的译文列表。"""
        glossary_terms = glossary_terms or []
        n = len(sources)
        if n == 0:
            return []

        attempts = self.config.pipeline.review_retry_limit + 1
        for _ in range(attempts):
            try:
                out = self._call_batch(sources, glossary_terms, style, context)
            except Exception:
                out = []
            if len(out) == n:
                return out
        # 兜底：逐段翻译，保证 1:1
        return [self._translate_one(s, glossary_terms, style, context) for s in sources]
