"""Translation agent using the strong tier.
Guarantee paragraph alignment: N source paragraphs must produce N corresponding
translations. Request an equal-length JSON array, retry count mismatches up to
align_retry_limit, then translate paragraphs individually. This final fallback prevents
entire paragraphs from being omitted.
"""

from __future__ import annotations

from ..glossary.store import GlossaryTerm
from ..i18n import languages
from ..i18n.prompts import render
from ..llm.json_parser import JsonParseError
from . import prompts
from .base import Agent


class AlignmentError(Exception):
    pass


class Translator(Agent):
    @staticmethod
    def _needs_translation(source: str) -> bool:
        """Send only nonempty paragraphs containing language characters to the model.
        PDF tables often yield separate hyphens, numbers or placeholders. Models may return
        them empty and trigger alignment errors; preserve those paragraphs unchanged.
        str.isalpha covers Unicode letters including Latin, Chinese, Japanese and Korean.
        """
        stripped = source.strip()
        return bool(stripped) and any(character.isalpha() for character in stripped)

    @staticmethod
    def _validate_annotation_contexts(
        sources: list[str],
        annotation_contexts: list[list[dict[str, str]]] | None,
    ) -> list[list[dict[str, str]]]:
        """Validate paragraph annotation references and retain only stable fields used by the
        prompt.
        """
        if annotation_contexts is None:
            return [[] for _ in sources]
        if not isinstance(annotation_contexts, list) or len(annotation_contexts) != len(sources):
            actual = (
                len(annotation_contexts) if isinstance(annotation_contexts, list) else "not a list"
            )
            raise ValueError(
                f"Annotation context count mismatch: expected {len(sources)} groups, got {actual}"
            )

        normalized: list[list[dict[str, str]]] = []
        for segment_index, items in enumerate(annotation_contexts):
            if not isinstance(items, list):
                raise ValueError(f"Annotation context for paragraph {segment_index} must be a list")
            segment_items: list[dict[str, str]] = []
            for item_index, item in enumerate(items):
                if not isinstance(item, dict):
                    raise ValueError(
                        f"Annotation {item_index} in paragraph {segment_index} must be an object"
                    )
                target_key = item.get("target_key")
                source = item.get("source")
                if not isinstance(target_key, str) or not target_key.strip():
                    raise ValueError(
                        f"Annotation {item_index} in paragraph {segment_index} has no valid target_key"
                    )
                if not isinstance(source, str):
                    raise ValueError(
                        f"Annotation {item_index} in paragraph {segment_index} has no string source"
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
        """Translate one batch and strictly validate output types, count and nonempty content."""
        n = len(sources)
        system = render(
            "translator_system",
            src=self.src,
            tgt=self.tgt,
            lang_guidance=languages.translate_guidance(
                self.src, self.config.honorific_strategy, self.tgt
            ),
        )
        user = render(
            "translator_user",
            src=self.src,
            tgt=self.tgt,
            style=style or "(none)",
            book_synopsis=book_synopsis or "(none)",
            glossary=prompts.render_glossary(glossary_terms),
            annotation_contexts=prompts.render_annotation_contexts(
                annotation_contexts or [[] for _ in sources]
            ),
            chapter_digest=chapter_digest or "(none)",
            context=context or "(none)",
            n=n,
            n_minus_1=n - 1,
            numbered_source=prompts.numbered(sources),
        )
        # Transient provider errors are retried only by the transport. Only JSON protocol errors in
        # successful responses enter alignment recovery, avoiding duplicate retries for 401/403/5xx errors.
        try:
            items = self._ask_json(system, user, tier="strong", key="translations")
        except JsonParseError as error:
            raise AlignmentError(
                "Cannot parse the translation JSON returned by the model"
            ) from error
        if not isinstance(items, list):
            raise AlignmentError("The model did not return a translation array")
        if len(items) != n:
            raise AlignmentError(
                f"Translation count mismatch: expected {n} paragraphs, got {len(items)}"
            )
        if any(not isinstance(item, str) or not item.strip() for item in items):
            raise AlignmentError("The model returned an empty or non-string translation")
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
        """Use the batch protocol for one paragraph as the final alignment fallback."""
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
        """Translate a batch and return the same number of target paragraphs."""
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
                # Recover only output protocol/alignment errors; the provider handles transport retries.
                continue

        # Fall back to individual paragraphs. If any still fails, stop explicitly and preserve saved
        # batches for resume. Empty placeholders would incorrectly mark the chapter complete.
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
                raise AlignmentError(
                    f"Single-paragraph fallback failed at paragraph {index}"
                ) from error
        return targets
