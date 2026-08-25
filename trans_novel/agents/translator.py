"""翻译 Agent（强档）。

核心保证：句段对齐——输入 N 段，输出必须是 N 段，一一对应。
策略：
1. 整批翻译并要求等长 JSON 数组；
2. 段数不符则重试（最多 align_retry_limit 次）；
3. 仍不符则逐段单独翻译兜底，从结构上保证 1:1，杜绝整段漏译。
"""

from __future__ import annotations

from ..glossary.store import GlossaryTerm
from ..llm.json_parser import JsonParseError
from . import langprofile, prompts
from .base import Agent


class AlignmentError(Exception):
    pass


class Translator(Agent):
    @staticmethod
    def _needs_translation(source: str) -> bool:
        """仅把含语言文字的非空段落发送给模型。

        PDF 表格经常把 ``-``、纯数字或其它占位符解析为独立段落。模型可能
        把这些内容返回为空字符串，进而触发对齐失败；这类段落原样保留即可。
        ``str.isalpha`` 覆盖拉丁、中文、日文、韩文等 Unicode 字母。
        """
        stripped = source.strip()
        return bool(stripped) and any(character.isalpha() for character in stripped)

    @staticmethod
    def _validate_annotation_contexts(
        sources: list[str],
        annotation_contexts: list[list[dict[str, str]]] | None,
    ) -> list[list[dict[str, str]]]:
        """校验逐段注释资料，并裁剪为提示词实际使用的稳定字段。"""
        if annotation_contexts is None:
            return [[] for _ in sources]
        if not isinstance(annotation_contexts, list) or len(annotation_contexts) != len(sources):
            actual = len(annotation_contexts) if isinstance(annotation_contexts, list) else "非列表"
            raise ValueError(f"注释上下文数量不匹配：期望 {len(sources)} 组，实际 {actual} 组")

        normalized: list[list[dict[str, str]]] = []
        for segment_index, items in enumerate(annotation_contexts):
            if not isinstance(items, list):
                raise ValueError(f"第 {segment_index} 段的注释上下文必须是列表")
            segment_items: list[dict[str, str]] = []
            for item_index, item in enumerate(items):
                if not isinstance(item, dict):
                    raise ValueError(f"第 {segment_index} 段第 {item_index} 条注释上下文必须是对象")
                target_key = item.get("target_key")
                source = item.get("source")
                if not isinstance(target_key, str) or not target_key.strip():
                    raise ValueError(
                        f"第 {segment_index} 段第 {item_index} 条注释上下文缺少有效 target_key"
                    )
                if not isinstance(source, str):
                    raise ValueError(
                        f"第 {segment_index} 段第 {item_index} 条注释上下文缺少字符串 source"
                    )
                segment_items.append({"target_key": target_key, "source": source})
            normalized.append(segment_items)
        return normalized

    def _call_batch(
        self,
        sources: list[str],
        glossary_terms: list[GlossaryTerm],
        style: str,
        context: str,
        book_synopsis: str = "",
        chapter_digest: str = "",
        annotation_contexts: list[list[dict[str, str]]] | None = None,
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
            annotation_contexts=prompts.render_annotation_contexts(
                annotation_contexts or [[] for _ in sources]
            ),
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
        self,
        source,
        glossary_terms,
        style,
        context,
        book_synopsis,
        chapter_digest,
        annotation_context,
    ) -> str:
        """借用批量协议翻译单段，作为批量对齐失败后的最终兜底。"""
        out = self._call_batch(
            [source],
            glossary_terms,
            style,
            context,
            book_synopsis,
            chapter_digest,
            [annotation_context],
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
        annotation_contexts: list[list[dict[str, str]]] | None = None,
    ) -> list[str]:
        """翻译一批源段，返回与之等长的译文列表。"""
        glossary_terms = glossary_terms or []
        n = len(sources)
        annotation_contexts = self._validate_annotation_contexts(sources, annotation_contexts)
        if n == 0:
            return []

        translated_indices = [
            index for index, source in enumerate(sources) if self._needs_translation(source)
        ]
        if not translated_indices:
            return list(sources)
        translated_sources = [sources[index] for index in translated_indices]
        translated_annotation_contexts = [
            annotation_contexts[index] for index in translated_indices
        ]

        attempts = self.config.pipeline.align_retry_limit + 1
        for _ in range(attempts):
            try:
                translated = self._call_batch(
                    translated_sources,
                    glossary_terms,
                    style,
                    context,
                    book_synopsis,
                    chapter_digest,
                    translated_annotation_contexts,
                )
                targets = list(sources)
                for index, target in zip(translated_indices, translated):
                    targets[index] = target
                return targets
            except AlignmentError:
                # 只恢复模型输出协议/对齐错误；传输错误已由 provider 统一处理。
                continue

        # 兜底：逐段翻译。任一段仍失败时显式中断，保留已落盘
        # 批次供续跑；不能用空字符串占位，否则章节会被错误标记为已完成。
        targets = list(sources)
        for index, source, annotation_context in zip(
            translated_indices,
            translated_sources,
            translated_annotation_contexts,
        ):
            try:
                targets[index] = self._translate_one(
                    source,
                    glossary_terms,
                    style,
                    context,
                    book_synopsis,
                    chapter_digest,
                    annotation_context,
                )
            except Exception as error:
                raise AlignmentError(f"逐段兜底翻译在第 {index} 段失败") from error
        return targets
