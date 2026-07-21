"""翻译 Agent（强档）。

核心保证：句段对齐——输入 N 段，输出必须是 N 段，一一对应。
策略：
1. 整批翻译并要求等长 JSON 数组；
2. 段数不符则重试（最多 align_retry_limit 次）；
3. 仍不符则逐段单独翻译兜底，从结构上保证 1:1，杜绝整段漏译。
"""

from __future__ import annotations

from ..agents import prompts
from ..agents.base import Agent
from ..glossary.store import GlossaryTerm
from ..locales import message as ui_message


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
            "translator_system", src=self.src, tgt=self.tgt,
            honorific_strategy=self.config.honorific_strategy,
        )
        empty = prompts.empty(tgt=self.tgt)
        user = prompts.render(
            "translator_user", src=self.src, tgt=self.tgt,
            style=style or empty,
            book_synopsis=book_synopsis or empty,
            glossary=prompts.render_glossary(glossary_terms, tgt=self.tgt),
            chapter_digest=chapter_digest or empty,
            context=context or empty,
            n=n, n_minus_1=n - 1,
            numbered_source=prompts.numbered(sources),
        )
        # 不传 default：调用失败照常抛出，由 translate_batch 的重试/兜底逻辑处理
        items = self._ask_json(system, user, tier="strong", key="translations")
        if not isinstance(items, list):
            raise AlignmentError(ui_message("error.translation_array_missing"))
        if len(items) != n:
            raise AlignmentError(
                ui_message(
                    "error.translation_count_mismatch",
                    expected=n,
                    actual=len(items),
                )
            )
        if any(not isinstance(item, str) or not item.strip() for item in items):
            raise AlignmentError(ui_message("error.translation_item_invalid"))
        return items

    def _translate_one(self, source, glossary_terms, style, context,
                       book_synopsis, chapter_digest) -> str:
        """借用批量协议翻译单段，作为批量对齐失败后的最终兜底。"""
        out = self._call_batch([source], glossary_terms, style, context,
                               book_synopsis, chapter_digest)
        return out[0]

    def retranslate_with_feedback(
        self,
        source: str,
        *,
        feedback: str,
        glossary_terms: list[GlossaryTerm] | None = None,
        style: str = "",
        context_before: str = "",
        context_after: str = "",
        book_synopsis: str = "",
        chapter_digest: str = "",
    ) -> str:
        """带审校意见定向重译单段（章末 autofix 用）。失败返回空串，由调用方决定弃用。

        复用 translator_system（与主翻译共享稳定前缀，命中缓存）；
        user 用 translator_fix_user：前缀块与主翻译一致，上下文换成前文+后文译文，附审校意见。
        """
        system = prompts.render(
            "translator_system", src=self.src, tgt=self.tgt,
            honorific_strategy=self.config.honorific_strategy,
        )
        empty = prompts.empty(tgt=self.tgt)
        user = prompts.render(
            "translator_fix_user", src=self.src, tgt=self.tgt,
            style=style or empty,
            book_synopsis=book_synopsis or empty,
            glossary=prompts.render_glossary(glossary_terms or [], tgt=self.tgt),
            chapter_digest=chapter_digest or empty,
            context_before=context_before or empty,
            context_after=context_after or empty,
            feedback=feedback or empty,
            source=source,
        )
        items = self._ask_json(system, user, tier="strong",
                               key="translations", default=None)
        if isinstance(items, list) and items:
            return str(items[0]).strip()
        return ""

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
            except Exception:
                pass

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
                raise AlignmentError(
                    ui_message("error.translation_fallback_failed", index=index)
                ) from error
        return targets
