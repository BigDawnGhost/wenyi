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
import re

from ..alignment.objects import marked_sources
from ..glossary.prompt_scope import split_trimmed_glossary_tiers
from ..glossary.store import GlossaryStore
from ..i18n import genres
from ..llm.json_parser import JsonParseError, UnsafeJsonRepairError, parse_json_result
from ..llm.preflight import fit_prompt_to_context_window
from ..llm.retrying import TruncatedResponseError
from ..models.glossary import TYPE_NAVIGATION, GlossaryTerm
from . import prompts
from .base import Agent, Messages
from .note_tools import batch_note_tools

_LOGGER = logging.getLogger(__name__)
_LEGACY_GLOSSARY_PLACEHOLDER_RE = re.compile(r"\$(?:\{glossary\}|glossary\b)")
# A reply shaped like a JSON object (a brace followed by a key, or empty), a string array, or a
# whole single-quoted literal. Prose that merely opens with a bracket ("[System] …",
# "{name} said", "['Tis the season.") does not match.
_JSON_ATTEMPT_RE = re.compile(
    r"""^\s*(?:\{\s*(?:"|\w+\s*:|\})|\[\s*"|[\[{]\s*'.*[\]}]\s*$)""", re.DOTALL
)
# The translations key as JSON-like output writes it: quoted, or unquoted right after an
# opening brace. Prose such as "The translations: …" does not match.
_ENVELOPE_KEY_RE = re.compile(r"""(?:(["'])translations\1|\{\s*translations)\s*:""")
_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)\n?```", re.DOTALL)


def _fence_body(text: str) -> tuple[str, bool]:
    """Return ``text`` without a surrounding code fence and whether one was present."""
    fence = _FENCE_RE.fullmatch(text)
    # Several fenced blocks joined by commentary are not one fenced reply.
    if fence is None or "```" in fence.group(1):
        return text, False
    return fence.group(1).strip(), True


def _plain_text_target(text: str, source: str) -> str:
    """Return the paragraph from a plain-text-mode reply, refusing JSON wrappers.

    An intact single-item ``translations`` envelope is unwrapped. A reply that mentions the
    envelope key the source lacks, or that is JSON-shaped while the source is not, may carry
    model reasoning around the translation and is rejected. A code fence around prose is
    peeled unless the source paragraph is itself fenced.
    """
    body, fenced = _fence_body(text)
    if _JSON_ATTEMPT_RE.match(body):
        try:
            parsed = parse_json_result(body)
        except JsonParseError:
            parsed = None
        if parsed is not None and parsed.safe_for_complete_payload:
            value = parsed.value
            items = value.get("translations") if isinstance(value, dict) else None
            if isinstance(items, list) and len(items) == 1 and isinstance(items[0], str):
                return items[0].strip()
    source = source.strip()
    source_body, source_fenced = _fence_body(source)
    if _ENVELOPE_KEY_RE.search(text) and not _ENVELOPE_KEY_RE.search(source):
        raise AlignmentError("The plain-text translation fallback returned a JSON wrapper")
    if _JSON_ATTEMPT_RE.match(body) and not _JSON_ATTEMPT_RE.match(source_body):
        raise AlignmentError("The plain-text translation fallback returned a JSON wrapper")
    # A fence embedded in commentary ("Here is the translation: ```…```") is not a translation.
    if not fenced and "```" in text and "```" not in source:
        raise AlignmentError("The plain-text translation fallback returned commentary")
    return body if fenced and not source_fenced else text


class AlignmentError(Exception):
    pass


class _EmptyTranslationError(AlignmentError):
    """Internal marker for a valid JSON array containing blank string translations."""


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
        """Render combined glossary entries and optional local notes for token accounting.

        Body translation requests split entity terms into ``system`` (``<glossary_data>``) and
        matched navigation titles or local notes into ``user``; this helper preserves a single
        combined rendering for prompt-size and event token metrics.
        """
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
    ) -> tuple[list[str], Messages | None]:
        """Translate one batch and validate output types, count and (by default) nonempty content.

        When ``allow_empty_translations`` is true (MinerU PDF path), blank strings are kept as
        formal targets so VLM OCR junk that the model refuses to translate does not abort the run.
        Returns the translations and the three-turn transcript for optional polish continuation.
        """
        n = len(sources)
        joined_sources = "\n".join(sources)
        model_sources = marked_sources(sources, annotation_contexts or [[] for _ in sources])
        base_system = self.render(
            "translator_system",
            src=self.src,
            tgt=self.tgt,
        )
        protected_guidance = prompts.protected_object_guidance(model_sources)
        style_heading = genres.compose(
            "${genre_style_heading}", genre=self.language_policy.context.genre
        )
        style_block = prompts.wrap_untrusted_style(style_heading, style or "(none)")
        synopsis_block = prompts.wrap_untrusted_reference(
            "Whole-book synopsis",
            "synopsis_data",
            book_synopsis or "(none)",
            item_label="book synopsis",
        )
        legacy_user_has_glossary = any(
            name == "translator_user" and _LEGACY_GLOSSARY_PLACEHOLDER_RE.search(text) is not None
            for name, text in self.language_policy.templates
        )
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

        match_text = "\n".join(part for part in (*sources, next_source, chapter_digest) if part)
        prefix_budget = self.config.pipeline.glossary_prompt_tokens

        def _render(terms_for_prompt: list[GlossaryTerm]) -> tuple[str, str]:
            # Keep book-wide style, synopsis and the frozen entity prefix at the end of the
            # system message so consecutive batches share a byte-identical KV-cacheable prefix.
            # Per-batch overflow entities, matched navigation titles, local glossary notes,
            # and protected-object guidance stay outside that shared prefix.
            entity_terms, batch_extra_terms = split_trimmed_glossary_tiers(
                glossary_terms, terms_for_prompt, match_text, prefix_budget=prefix_budget
            )
            overflow_terms = [t for t in batch_extra_terms if t.type != TYPE_NAVIGATION]
            nav_terms = [t for t in batch_extra_terms if t.type == TYPE_NAVIGATION]
            shared_glossary = prompts.render_glossary(
                entity_terms,
                source_lang=self.src,
                genre=self.language_policy.context.genre,
            )
            glossary_block = prompts.wrap_untrusted_glossary(shared_glossary)
            system_text = (
                f"{base_system}\n\n{style_block}\n\n{synopsis_block}\n\n"
                f"{glossary_block}{protected_guidance}"
            )
            overflow_glossary = (
                prompts.render_glossary(
                    overflow_terms,
                    source_lang=self.src,
                    genre=self.language_policy.context.genre,
                )
                if overflow_terms
                else ""
            )
            nav_glossary = (
                prompts.render_glossary(
                    nav_terms,
                    source_lang=self.src,
                    genre=self.language_policy.context.genre,
                )
                if nav_terms
                else ""
            )
            batch_notes = (
                prompts.render_glossary_notes(
                    terms_for_prompt,
                    note_source=joined_sources,
                    genre=self.language_policy.context.genre,
                ).strip()
                if self.config.pipeline.use_glossary_notes
                else ""
            )
            legacy_glossary_parts = [
                part for part in (overflow_glossary, nav_glossary, batch_notes) if part
            ]
            user_text = self.render(
                "translator_user",
                src=self.src,
                tgt=self.tgt,
                # Retained for projects whose frozen language-policy snapshot still holds
                # the pre-cache-boundary translator_user template placeholders.
                style="(see style guide in system instructions)",
                book_synopsis="(see [Whole-book synopsis] in system instructions)",
                glossary=(
                    "\n\n".join(legacy_glossary_parts)
                    if legacy_glossary_parts
                    else "(see [Glossary] in system instructions)"
                ),
                annotation_contexts=rendered_annotations,
                chapter_digest=chapter_digest or "(none)",
                context=context or "(none)",
                n=n,
                n_minus_1=n - 1,
                numbered_source=numbered_source,
                next_source=next_ref,
            )
            if not legacy_user_has_glossary:
                user_prefix_parts: list[str] = []
                if overflow_glossary:
                    user_prefix_parts.append(f"[Additional glossary]\n{overflow_glossary}")
                if nav_glossary:
                    user_prefix_parts.append(f"[Navigation titles]\n{nav_glossary}")
                if batch_notes:
                    user_prefix_parts.append(batch_notes)
                if user_prefix_parts:
                    user_text = "\n\n".join(user_prefix_parts) + "\n\n" + user_text
            if note_lookup_suffix:
                has_tools = batch_note_tools(terms_for_prompt, joined_sources) is not None
                if has_tools:
                    user_text += note_lookup_suffix
            return system_text, user_text

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
            data, raw = self._complete_json_turn(
                messages, operation="translation.body", require_complete_payload=True
            )
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
                data, raw = self._complete_json_turn(
                    messages, operation="translation.body", require_complete_payload=True
                )
        except TruncatedResponseError as error:
            raise AlignmentError(
                "The translation response reached the output token limit"
            ) from error
        except UnsafeJsonRepairError as error:
            # Repair can fold trailing reasoning or a second attempt into a translation string
            # while still producing the expected count, so only intact JSON may become a target.
            if n == 1:
                _LOGGER.warning(
                    "Single-paragraph translation JSON needed structural repair; retrying in "
                    "plain-text mode"
                )
                plain = self._translate_one_plain_text(
                    sources[0],
                    glossary_terms,
                    style,
                    (annotation_contexts or [[]])[0],
                )
                return [plain], None
            raise AlignmentError(
                "The translation JSON returned by the model needed structural repair"
            ) from error
        except JsonParseError as error:
            raise AlignmentError(
                "Cannot parse the translation JSON returned by the model"
            ) from error
        items = self._extract_batch_items(data, n)
        if not allow_empty_translations and any(not item.strip() for item in items):
            if n == 1:
                try:
                    items, raw, messages = self._recover_empty_single_paragraph(messages, raw)
                except _EmptyTranslationError:
                    _LOGGER.warning(
                        "Single-paragraph JSON follow-up remained empty; retrying in plain-text mode"
                    )
                    plain = self._translate_one_plain_text(
                        sources[0],
                        glossary_terms,
                        style,
                        (annotation_contexts or [[]])[0],
                    )
                    return [plain], None
            else:
                raise _EmptyTranslationError(
                    "The model returned an empty or non-string translation"
                )
        turn = [
            *messages,
            {"role": "assistant", "content": raw},
        ]
        return items, turn

    @staticmethod
    def _extract_batch_items(data: object, expected_count: int) -> list[str]:
        items = data.get("translations") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise AlignmentError("The model did not return a translation array")
        if len(items) != expected_count:
            raise AlignmentError(
                f"Translation count mismatch: expected {expected_count} paragraphs, got {len(items)}"
            )
        if any(not isinstance(item, str) for item in items):
            raise AlignmentError("The model returned a non-string translation")
        return items

    def _recover_empty_single_paragraph(
        self,
        messages: Messages,
        raw: str,
    ) -> tuple[list[str], str, Messages]:
        """Retry an isolated blank translation once on the same conversation prefix.

        Models occasionally strip stray boilerplate, scraper footers, or fragments to ``""``
        when translating in full book context. A single corrective follow-up turn preserves
        KV-cache reuse and recovers a faithful literal rendering while still rejecting
        persistent empty responses.
        """
        _LOGGER.warning(
            "Single-paragraph translation returned empty string; retrying with non-empty reminder"
        )
        follow_up: Messages = [
            *messages,
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": (
                    'Your previous response returned an empty string "". Every non-empty source '
                    'paragraph must receive a non-empty string in "translations". Even if the '
                    "paragraph looks like a fragment, watermark, boilerplate, or noise, translate "
                    "it literally or preserve it as-is. Return only JSON: "
                    '{"translations":["..."]} with 1 non-empty string.'
                ),
            },
        ]
        try:
            data, retry_raw = self._complete_json_turn(
                follow_up, operation="translation.body", require_complete_payload=True
            )
        except UnsafeJsonRepairError as error:
            raise _EmptyTranslationError(
                "The empty-translation follow-up needed structural JSON repair"
            ) from error
        except TruncatedResponseError as error:
            raise AlignmentError(
                "The translation response reached the output token limit"
            ) from error
        except JsonParseError as error:
            raise AlignmentError(
                "Cannot parse the translation JSON returned by the model"
            ) from error
        items = self._extract_batch_items(data, 1)
        if not items[0].strip():
            raise _EmptyTranslationError("The model returned an empty or non-string translation")
        return items, retry_raw, follow_up

    def _translate_one_plain_text(
        self,
        source: str,
        glossary_terms: list[GlossaryTerm],
        style: str,
        annotation_context: list[dict[str, str]],
    ) -> str:
        """Fallback for a single paragraph when JSON mode collapses into an empty string.

        Some models with JSON-mode decoding collide on leading dialogue quotes (emitting ``""``
        instead of ``"\\"...``) or refuse fragments inside a JSON array. Requesting plain text
        avoids JSON string-quoting collisions while keeping matched glossary terms, style, and
        protected markers intact.
        """
        marked_source = marked_sources([source], [annotation_context])[0]
        prompt_vals = dict(self.language_policy.prompt_values)
        lang_guidance = prompt_vals.get("configured_lang_guidance", "") or prompt_vals.get(
            "lang_guidance", ""
        )
        punct_rule = prompt_vals.get("punct_rule", "")
        protected_guidance = prompts.protected_object_guidance([marked_source])
        matched_terms = GlossaryStore.terms_in(glossary_terms, source)
        glossary_block = (
            prompts.render_glossary(
                matched_terms,
                source_lang=self.src,
                genre=self.language_policy.context.genre,
                note_source=None,
            )
            if matched_terms
            else ""
        )
        system_parts = [
            (
                f"You are an experienced literary translator translating {self.src} into "
                f"{self.tgt}. Translate the user's single paragraph faithfully and return ONLY "
                "the translated paragraph as plain text—no JSON, no array brackets, no "
                "explanations. Even if the paragraph begins with quotation marks or looks like "
                "a fragment, boilerplate, or watermark, translate it literally or preserve it "
                "as-is; never return an empty response."
            ),
        ]
        if lang_guidance:
            system_parts.append(f"Language guidance:\n{lang_guidance}")
        if punct_rule:
            system_parts.append(punct_rule)
        if protected_guidance:
            system_parts.append(protected_guidance)
        if style:
            style_heading = genres.compose(
                "${genre_style_heading}", genre=self.language_policy.context.genre
            )
            system_parts.append(prompts.wrap_untrusted_style(style_heading, style))
        if glossary_block:
            system_parts.append(
                prompts.wrap_untrusted_reference(
                    "Relevant glossary terms",
                    "glossary_data",
                    glossary_block,
                    item_label="glossary",
                )
            )
        user_parts: list[str] = []
        if annotation_context:
            user_parts.append(
                "[Paragraph-specific annotation references] (JSON; reference only, do not copy)\n"
                + json.dumps(annotation_context, ensure_ascii=False)
            )
        user_parts.append(marked_source)
        messages: Messages = [
            {"role": "system", "content": "\n\n".join(system_parts)},
            {"role": "user", "content": "\n\n".join(user_parts)},
        ]
        try:
            raw = self.client.complete(
                messages,
                operation="translation.body",
                json_mode=False,
            )
        except TruncatedResponseError as error:
            raise AlignmentError(
                "The translation response reached the output token limit"
            ) from error
        text = _plain_text_target((raw or "").strip(), source)
        if not text:
            raise _EmptyTranslationError("The model returned an empty or non-string translation")
        return text

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
                return targets, turn, (list(translated_indices) if turn is not None else None)
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
