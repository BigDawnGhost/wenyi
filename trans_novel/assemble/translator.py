"""翻译 Agent（强档）。

核心保证：句段对齐——输入 N 段，输出必须是 N 段，一一对应。
策略：
1. 整批翻译并要求等长 JSON 数组；
2. 段数不符则重试（最多 align_retry_limit 次）；
3. 仍不符则逐段单独翻译兜底，从结构上保证 1:1，杜绝整段漏译。
"""

from __future__ import annotations

from ..agents import langprofile, prompts
from ..agents.base import Agent
from ..glossary.store import GlossaryTerm
from ..llm.json_parser import JsonParseError


class AlignmentError(Exception):
    pass


class Translator(Agent):
    def _call_batch(
        self,
        sources: list[str],
        glossary_terms: list[GlossaryTerm],
        style: str,
        context: str,
        book_synopsis: str = "",
        chapter_digest: str = "",
    ) -> list[str]:
        """调用一次批量翻译，并严格校验输出类型、数量和非空性。"""
        n = len(sources)
        system = prompts.render(
            "translator_system",
            src=self.src,
            tgt=self.tgt,
            lang_guidance=langprofile.translate_guidance(self.src, self.config.honorific_strategy),
        )
        user = prompts.render(
            "translator_user",
            src=self.src,
            tgt=self.tgt,
            style=style or "（无）",
            book_synopsis=book_synopsis or "（无）",
            glossary=prompts.render_glossary(glossary_terms),
            chapter_digest=chapter_digest or "（无）",
            context=context or "（无）",
            n=n,
            n_minus_1=n - 1,
            numbered_source=prompts.numbered(sources),
        )
        # Provider 瞬时错误只由传输层重试；这里仅把成功响应中的 JSON
        # 协议错误归入对齐恢复，避免 401/403/5xx 被业务层再次放大。
        try:
            items = self._ask_json(system, user, tier="strong", key="translations")
        except JsonParseError as error:
            raise AlignmentError("模型返回的译文 JSON 无法解析") from error
        if not isinstance(items, list):
            raise AlignmentError("模型未返回译文数组")
        if len(items) != n:
            raise AlignmentError(f"译文数量不匹配：期望 {n} 段，实际 {len(items)} 段")
        if any(not isinstance(item, str) or not item.strip() for item in items):
            raise AlignmentError("模型返回了空译文或非字符串译文")
        return items

    def _translate_one(
        self, source, glossary_terms, style, context, book_synopsis, chapter_digest
    ) -> str:
        """借用批量协议翻译单段，作为批量对齐失败后的最终兜底。"""
        out = self._call_batch(
            [source], glossary_terms, style, context, book_synopsis, chapter_digest
        )
        return out[0]

    def translate_batch(
        self,
        sources: list[str],
        *,
        glossary_terms: list[GlossaryTerm] | None = None,
        style: str = "",
        context: str = "",
        book_synopsis: str = "",
        chapter_digest: str = "",
    ) -> list[str]:
        """翻译一批源段，返回与之等长的译文列表。"""
        glossary_terms = glossary_terms or []
        n = len(sources)
        if n == 0:
            return []

        attempts = self.config.pipeline.align_retry_limit + 1
        for _ in range(attempts):
            try:
                return self._call_batch(
                    sources,
                    glossary_terms,
                    style,
                    context,
                    book_synopsis,
                    chapter_digest,
                )
            except AlignmentError:
                # 只恢复模型输出协议/对齐错误；传输错误已由 provider 统一处理。
                continue

        # 兜底：逐段翻译。任一段仍失败时显式中断，保留已落盘
        # 批次供续跑；不能用空字符串占位，否则章节会被错误标记为已完成。
        targets: list[str] = []
        for index, source in enumerate(sources):
            try:
                targets.append(
                    self._translate_one(
                        source,
                        glossary_terms,
                        style,
                        context,
                        book_synopsis,
                        chapter_digest,
                    )
                )
            except Exception as error:
                raise AlignmentError(f"逐段兜底翻译在第 {index} 段失败") from error
        return targets
