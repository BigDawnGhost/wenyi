"""Translation agent using the body translation route.
Guarantee paragraph alignment: N source paragraphs must produce N corresponding
translations. Request an equal-length JSON array, retry count mismatches up to
align_retry_limit, then recursively split the batch in half down to individual
paragraphs if needed. This final fallback prevents entire paragraphs from being
omitted while avoiding N-way call blowups on localized batch alignment failures.
"""

from __future__ import annotations

import json
import logging

from ..alignment.objects import marked_sources
from ..llm.json_parser import JsonParseError
from ..llm.preflight import fit_prompt_to_context_window
from ..llm.retrying import TruncatedResponseError
from ..models.glossary import GlossaryTerm
from . import prompts
from .base import Agent, Messages
from .note_tools import batch_note_tools

_LOGGER = logging.getLogger(__name__)


class AlignmentError(Exception):
    pass


class Translator(Agent):
    """Body translator.

    After a successful single-shot batch call, ``last_batch_turn`` holds the
    ``system`` / ``user`` / ``assistant`` messages so polishing can append another
    user turn instead of opening a new conversation. Binary-split / per-paragraph
    fallback clears that transcript.
    """

    def __init__(self, client, config):
        super().__init__(client, config)
        self.last_batch_turn: Messages | None = None
        self.last_batch_indices: list[int] | None = None

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
                reference = {"target_key": target_key, "source": source}
                if "marker" in item:
                    if not isinstance(item["marker"], str) or not isinstance(
                        item.get("source_offset"), str
                    ):
                        raise ValueError(
                            "Protected object context requires a marker and source offset"
                        )
                    reference.update(marker=item["marker"], source_offset=item["source_offset"])
                segment_items.append(reference)
            normalized.append(segment_items)
        return normalized

    def render_glossary_block(self, glossary_terms: list[GlossaryTerm], sources: list[str]) -> str:
        """Render the ``[Glossary]`` block exactly as a body translation request sends it."""
        return prompts.render_glossary(
            glossary_terms,
            source_lang=self.src,
            genre=self.language_policy.context.genre,
            note_source="\n".join(sources) if self.config.pipeline.use_glossary_notes else None,
        )

    def _call_batch(
        self,
        sources: list[str],
        glossary_terms: list[GlossaryTerm],
        style: str,
        context: str,
        book_synopsis: str = "",
        chapter_digest: str = "",
        annotation_contexts: list[list[dict[str, str]]] | None = None,
        next_source: str = "",
        *,
        allow_empty_translations: bool = False,
    ) -> tuple[list[str], Messages]:
        """Translate one batch and validate output types, count and (by default) nonempty content.

        When ``allow_empty_translations`` is true (MinerU PDF path), blank strings are kept as
        formal targets so VLM OCR junk that the model refuses to translate does not abort the run.
        Returns the translations and the three-turn transcript for optional polish continuation.
        """
        n = len(sources)
        model_sources = marked_sources(sources, annotation_contexts or [[] for _ in sources])
        system_template = self.render(
            "translator_system",
            src=self.src,
            tgt=self.tgt,
        ) + prompts.protected_object_guidance(model_sources)
        rendered_annotations = prompts.render_annotation_contexts(
            annotation_contexts or [[] for _ in sources]
        )
        numbered_source = prompts.numbered(model_sources)
        next_ref = prompts.render_source_reference(next_source)
        note_lookup_suffix = (
            "\n" + self.render("glossary_note_lookup")
            if self.config.pipeline.glossary_note_workers
            else ""
        )

        def _render(terms_for_prompt: list[GlossaryTerm]) -> tuple[str, str]:
            user_text = self.render(
                "translator_user",
                src=self.src,
                tgt=self.tgt,
                style=style or "(none)",
                book_synopsis=book_synopsis or "(none)",
                glossary=self.render_glossary_block(terms_for_prompt, sources),
                annotation_contexts=rendered_annotations,
                chapter_digest=chapter_digest or "(none)",
                context=context or "(none)",
                n=n,
                n_minus_1=n - 1,
                numbered_source=numbered_source,
                next_source=next_ref,
            )
            if note_lookup_suffix:
                has_tools = batch_note_tools(terms_for_prompt, "\n".join(sources)) is not None
                if has_tools:
                    user_text += note_lookup_suffix
            return system_template, user_text

        match_text = "\n".join(part for part in (*sources, next_source, chapter_digest) if part)
        system, user, effective_terms, _shrunk = fit_prompt_to_context_window(
            config=self.config,
            operation="translation.body",
            terms=glossary_terms,
            match_text=match_text,
            render_messages=_render,
        )
        tools = (
            batch_note_tools(effective_terms, "\n".join(sources))
            if self.config.pipeline.glossary_note_workers
            else None
        )
        messages: Messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        # Transient provider errors are retried only by the transport. Only JSON protocol errors in
        # successful responses enter alignment recovery, avoiding duplicate retries for 401/403/5xx errors.
        try:
            data, raw = self._complete_json_turn(messages, operation="translation.body")
            if tools is not None and isinstance(data, dict) and set(data) == {"read_note"}:
                try:
                    page = tools.call("read_note", data["read_note"])
                except ValueError as error:
                    raise AlignmentError("Invalid read_note request") from error
                messages.extend(
                    [
                        {"role": "assistant", "content": raw},
                        {
                            "role": "user",
                            "content": json.dumps({"note_evidence": page}, ensure_ascii=False)
                            + "\nNow return only the requested translations.",
                        },
                    ]
                )
                data, raw = self._complete_json_turn(messages, operation="translation.body")
        except TruncatedResponseError as error:
            raise AlignmentError(
                "The translation response reached the output token limit"
            ) from error
        except JsonParseError as error:
            raise AlignmentError(
                "Cannot parse the translation JSON returned by the model"
            ) from error
        items = data.get("translations") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise AlignmentError("The model did not return a translation array")
        if len(items) != n:
            raise AlignmentError(
                f"Translation count mismatch: expected {n} paragraphs, got {len(items)}"
            )
        if any(not isinstance(item, str) for item in items):
            raise AlignmentError("The model returned a non-string translation")
        if not allow_empty_translations and any(not item.strip() for item in items):
            raise AlignmentError("The model returned an empty or non-string translation")
        turn = [
            *messages,
            {"role": "assistant", "content": raw},
        ]
        return items, turn

    def _translate_one(
        self,
        source,
        glossary_terms,
        style,
        context,
        book_synopsis,
        chapter_digest,
        annotation_context,
        next_source: str,
        *,
        allow_empty_translations: bool = False,
    ) -> str:
        """Use the batch protocol for one paragraph as the final alignment fallback."""
        out, _turn = self._call_batch(
            [source],
            glossary_terms,
            style,
            context,
            book_synopsis,
            chapter_digest,
            [annotation_context],
            next_source=next_source,
            allow_empty_translations=allow_empty_translations,
        )
        return out[0]

    def translate_batch_with_transcript(
        self,
        sources: list[str],
        *,
        glossary_terms: list[GlossaryTerm] | None = None,
        style: str = "",
        context: str = "",
        book_synopsis: str = "",
        chapter_digest: str = "",
        annotation_contexts: list[list[dict[str, str]]] | None = None,
        next_source: str = "",
        allow_empty_translations: bool = False,
    ) -> tuple[list[str], Messages | None, list[int] | None]:
        """Thread-safe translation returning ``(targets, turn, indices)`` without mutating self."""
        glossary_terms = glossary_terms or []
        n = len(sources)
        annotation_contexts = self._validate_annotation_contexts(sources, annotation_contexts)
        if n == 0:
            return [], None, None

        translated_indices = [
            index for index, source in enumerate(sources) if self._needs_translation(source)
        ]
        if not translated_indices:
            return list(sources), None, None
        translated_sources = [sources[index] for index in translated_indices]
        translated_annotation_contexts = [
            annotation_contexts[index] for index in translated_indices
        ]

        # A filtered trailing number or symbol remains the immediate source neighbor.
        def _next_source_for(slice_indices: list[int]) -> str:
            following_index = slice_indices[-1] + 1
            return sources[following_index] if following_index < n else next_source

        batch_next_source = _next_source_for(translated_indices)

        attempts = max(0, self.config.pipeline.align_retry_limit) + 1
        last_error: AlignmentError | None = None
        for attempt in range(1, attempts + 1):
            try:
                translated, turn = self._call_batch(
                    translated_sources,
                    glossary_terms,
                    style,
                    context,
                    book_synopsis,
                    chapter_digest,
                    translated_annotation_contexts,
                    next_source=batch_next_source,
                    allow_empty_translations=allow_empty_translations,
                )
                targets = list(sources)
                for index, target in zip(translated_indices, translated):
                    targets[index] = target
                return targets, turn, list(translated_indices)
            except AlignmentError as error:
                # Recover only output protocol/alignment errors; the provider handles transport retries.
                last_error = error
                _LOGGER.warning(
                    "Translation batch alignment failed (size=%d, attempt=%d/%d): %s",
                    len(translated_sources),
                    attempt,
                    attempts,
                    error,
                )
                continue

        if len(translated_sources) == 1:
            index = translated_indices[0]
            _LOGGER.warning(
                "Single-paragraph fallback failed at paragraph %d after %d attempt(s): %s",
                index,
                attempts,
                last_error,
            )
            raise AlignmentError(
                f"Single-paragraph fallback failed at paragraph {index}"
            ) from last_error

        # Fall back to recursive binary splitting down to single paragraphs. If any leaf still
        # fails, stop explicitly and preserve saved batches for resume. Without
        # allow_empty_translations, empty placeholders must not mark the chapter complete;
        # MinerU may persist "" when the model returns a blank string.
        def _translate_split(
            slice_indices: list[int],
            slice_sources: list[str],
            slice_annotations: list[list[dict[str, str]]],
            *,
            try_whole: bool,
        ) -> list[str]:
            size = len(slice_sources)
            if size == 1:
                index = slice_indices[0]
                _LOGGER.warning(
                    "Running single-paragraph alignment fallback at paragraph %d",
                    index,
                )
                try:
                    translated_one = self._translate_one(
                        slice_sources[0],
                        glossary_terms,
                        style,
                        context,
                        book_synopsis,
                        chapter_digest,
                        slice_annotations[0],
                        next_source=_next_source_for(slice_indices),
                        allow_empty_translations=allow_empty_translations,
                    )
                except AlignmentError as error:
                    _LOGGER.warning(
                        "Single-paragraph fallback failed at paragraph %d: %s",
                        index,
                        error,
                    )
                    raise AlignmentError(
                        f"Single-paragraph fallback failed at paragraph {index}"
                    ) from error
                return [translated_one]

            if try_whole:
                try:
                    translated, _turn = self._call_batch(
                        slice_sources,
                        glossary_terms,
                        style,
                        context,
                        book_synopsis,
                        chapter_digest,
                        slice_annotations,
                        next_source=_next_source_for(slice_indices),
                        allow_empty_translations=allow_empty_translations,
                    )
                    return translated
                except AlignmentError as error:
                    _LOGGER.debug(
                        "Split translation sub-batch alignment failed (size=%d, attempt=1/1): %s",
                        size,
                        error,
                    )

            mid = size // 2
            _LOGGER.warning(
                "Falling back to binary split for %d-paragraph batch -> %d + %d",
                size,
                mid,
                size - mid,
            )
            left = _translate_split(
                slice_indices[:mid],
                slice_sources[:mid],
                slice_annotations[:mid],
                try_whole=True,
            )
            right = _translate_split(
                slice_indices[mid:],
                slice_sources[mid:],
                slice_annotations[mid:],
                try_whole=True,
            )
            return left + right

        split_translated = _translate_split(
            translated_indices,
            translated_sources,
            translated_annotation_contexts,
            try_whole=False,
        )
        targets = list(sources)
        for index, target in zip(translated_indices, split_translated):
            targets[index] = target
        return targets, None, None

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
        next_source: str = "",
        allow_empty_translations: bool = False,
    ) -> list[str]:
        """Translate aligned paragraphs with one following source segment as reference only."""
        targets, turn, indices = self.translate_batch_with_transcript(
            sources,
            glossary_terms=glossary_terms,
            style=style,
            context=context,
            book_synopsis=book_synopsis,
            chapter_digest=chapter_digest,
            annotation_contexts=annotation_contexts,
            next_source=next_source,
            allow_empty_translations=allow_empty_translations,
        )
        self.last_batch_turn = turn
        self.last_batch_indices = indices
        return targets
