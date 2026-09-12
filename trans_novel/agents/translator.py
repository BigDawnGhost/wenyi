"""翻译 Agent。

balanced/quality 每次调用只发送一个待译段，并接收纯译文，从调用边界保证源段与译文
一一对应。economy 保留批量翻译；批量协议失败时仍逐段兜底。

模型路由按功能 Agent 选择：正文走 translator（operation=translate.batch 或
translate.single）；operation 只作用量/调试归因，不参与路由。
"""

from dataclasses import dataclass

from trans_novel.agents import langprofile, prompts
from trans_novel.agents.base import Agent, WorkflowProtocolError, retry_protocol
from trans_novel.glossary.store import GlossaryTerm, terms_matching_text
from trans_novel.ingest.models import KIND_HEADING
from trans_novel.llm.errors import JSONParseError


@dataclass(frozen=True, slots=True)
class TranslationBatchResult:
    translations: tuple[str, ...]
    request_count: int


class AlignmentError(WorkflowProtocolError):
    """句段对齐失败：协议错误的子类，reason 是稳定标识，message 是可读的中文说明。"""


class Translator(Agent):
    def _call_batch(
        self,
        sources: list[str],
        glossary_terms: list[GlossaryTerm],
        style: str,
        context: str,
        *,
        agent: str,
        operation: str = "translate.batch",
        source_context: str = "",
        chapter_title: str = "",
    ) -> list[str]:
        n = len(sources)
        system = prompts.render(
            "translator_system",
            src=self.src,
            tgt=self.tgt,
            n=n,
            lang_guidance=langprofile.translate_guidance(self.src, self.config.honorific_strategy),
        )
        user = prompts.render(
            "translator_user",
            src=self.src,
            tgt=self.tgt,
            style=style or "（无）",
            glossary=prompts.render_glossary(glossary_terms),
            context=context or "（无）",
            source_context=source_context or "（无）",
            chapter_title=chapter_title or "（无）",
            n=n,
            n_minus_1=n - 1,
            numbered_source=prompts.numbered(sources),
        )
        items = self._ask_json(system, user, key="translations", agent=agent, operation=operation)
        if not isinstance(items, list) or len(items) != n:
            raise AlignmentError(
                "translation_count_mismatch",
                f"译文数量不匹配：期望 {n} 段，实际 {len(items) if isinstance(items, list) else '非数组'}",
            )
        if any(not isinstance(item, str) or not item.strip() for item in items):
            raise AlignmentError("translation_item_invalid", "模型返回了空译文或非字符串译文")
        return items

    def _translate_one(
        self,
        source,
        glossary_terms,
        style,
        context,
        *,
        agent: str,
        operation: str = "translate.batch",
        kind: str | None = None,
        source_context: str = "",
        chapter_title: str = "",
    ) -> str:
        if kind == KIND_HEADING:
            system = prompts.render("translator_heading_system", src=self.src)
            glossary_terms = terms_matching_text(glossary_terms, source)
            user = prompts.render(
                "translator_heading_user",
                src=self.src,
                source=source,
                glossary=prompts.render_glossary(glossary_terms),
            )
        else:
            system = prompts.render("translator_single_system", src=self.src)
            user = prompts.render(
                "translator_single_user",
                src=self.src,
                source=source,
                glossary=prompts.render_glossary(glossary_terms),
                style=style or "（无）",
                context=context or "（无）",
                source_context=source_context or "（无）",
                chapter_title=chapter_title or "（无）",
            )
        target = self._ask_text(system, user, agent=agent, operation=operation, strict=True)
        if not target or len(target) > max(256, len(source) * 4):
            raise AlignmentError("translation_item_invalid", "模型返回了空译文或异常长译文")
        return target

    def _translate_one_with_protocol_retry(
        self,
        source,
        glossary_terms,
        style,
        context,
        *,
        agent: str,
        operation: str,
        kind: str | None,
        retries: int,
        source_context: str = "",
        chapter_title: str = "",
    ) -> tuple[str | None, int]:
        request_count = 0

        def call() -> str:
            nonlocal request_count
            request_count += 1
            return self._translate_one(
                source,
                glossary_terms,
                style,
                context,
                agent=agent,
                operation=operation,
                kind=kind,
                source_context=source_context,
                chapter_title=chapter_title,
            )

        try:
            return retry_protocol(call, retries=retries), request_count
        except (WorkflowProtocolError, JSONParseError):
            return None, request_count

    def repair_issue(
        self,
        source: str,
        current_target: str,
        *,
        issue_type: str,
        issue_detail: str,
        glossary_terms: list[GlossaryTerm] | None = None,
        context_before: str = "",
        context_after: str = "",
    ) -> str:
        """Ask the editor for one complete replacement target for one lint issue."""
        system = prompts.render("translator_single_system", src=self.src)
        user = prompts.render(
            "translator_repair_user",
            glossary=prompts.render_glossary(glossary_terms or []),
            context_before=context_before or "（无）",
            context_after=context_after or "（无）",
            source=source,
            current_target=current_target,
            issue_type=issue_type,
            issue_detail=issue_detail,
        )

        def call() -> str:
            target = self._ask_text(
                system,
                user,
                agent="editor",
                operation="translate.repair",
                strict=True,
            )
            if not target or len(target) > max(256, len(source) * 4):
                raise AlignmentError("translation_item_invalid", "模型返回了空译文或异常长译文")
            return target

        return retry_protocol(call, retries=self.config.pipeline.protocol_retry_limit)

    def translate_batch(
        self,
        sources: list[str],
        *,
        agent: str,
        operation: str = "translate.batch",
        fallback_agent: str | None = None,
        glossary_terms: list[GlossaryTerm] | None = None,
        style: str = "",
        context: str = "",
        source_context: str = "",
        chapter_title: str = "",
        kind: str | None = None,
    ) -> TranslationBatchResult:
        glossary_terms = glossary_terms or []
        if not sources:
            return TranslationBatchResult((), 0)
        translated_indices = [
            index for index, source in enumerate(sources) if langprofile.needs_translation(source)
        ]
        targets = list(sources)
        if not translated_indices:
            return TranslationBatchResult(tuple(targets), 0)

        request_count = 0
        retries = self.config.pipeline.protocol_retry_limit
        if self.config.pipeline.single_segment_translation:
            for index in translated_indices:
                target, count = self._translate_one_with_protocol_retry(
                    sources[index],
                    glossary_terms,
                    style,
                    context,
                    agent=agent,
                    operation=operation,
                    kind=kind,
                    retries=retries,
                    source_context=source_context,
                    chapter_title=chapter_title,
                )
                request_count += count
                if target is None and fallback_agent is not None:
                    target, count = self._translate_one_with_protocol_retry(
                        sources[index],
                        glossary_terms,
                        style,
                        context,
                        agent=fallback_agent,
                        operation=operation,
                        kind=kind,
                        retries=retries,
                        source_context=source_context,
                        chapter_title=chapter_title,
                    )
                    request_count += count
                if target is None:
                    raise AlignmentError(
                        "translation_segment_contract_failed",
                        f"索引为 {index} 的段落未能返回合法单段译文",
                    )
                targets[index] = target
            return TranslationBatchResult(tuple(targets), request_count)

        translated_sources = [sources[index] for index in translated_indices]

        def call_batch() -> list[str]:
            nonlocal request_count
            request_count += 1
            return self._call_batch(
                translated_sources,
                glossary_terms,
                style,
                context,
                agent=agent,
                operation=operation,
                source_context=source_context,
                chapter_title=chapter_title,
            )

        try:
            translated = retry_protocol(call_batch, retries=retries)
            for index, target in zip(translated_indices, translated, strict=True):
                targets[index] = target
            return TranslationBatchResult(tuple(targets), request_count)
        except (WorkflowProtocolError, JSONParseError):
            pass
        for index in translated_indices:
            source = sources[index]
            attempt = 0

            def call_single(source=source) -> str:
                nonlocal attempt, request_count
                request_count += 1
                first = attempt == 0
                attempt += 1
                return self._translate_one(
                    source,
                    glossary_terms if first else [],
                    style if first else "",
                    context if first else "",
                    agent=agent,
                    operation=operation,
                    kind=kind,
                    source_context=source_context,
                    chapter_title=chapter_title,
                )

            try:
                targets[index] = retry_protocol(call_single, retries=retries)
            except (WorkflowProtocolError, JSONParseError) as error:
                raise AlignmentError(
                    "translation_segment_fallback_failed",
                    f"索引为 {index} 的段落在兜底翻译时失败",
                ) from error
        return TranslationBatchResult(tuple(targets), request_count)
