"""Polishing agent using the body polishing route.
Improve literary quality in the target language without changing information or paragraph
count. Prefer appending a polish user turn to the translation conversation so shared
prompt prefixes stay cacheable. Preserve the original translation on alignment failure so
polishing cannot drop paragraphs.
"""

from __future__ import annotations

import json

from ..llm.preflight import fit_prompt_to_context_window
from ..llm.retrying import TruncatedResponseError, is_resumable_provider_interrupt
from ..models.glossary import GlossaryTerm
from . import prompts
from .base import _LOGGER, Agent, Messages

_MISALIGNED = object()
_PROVIDER_ERROR = object()


class Polisher(Agent):
    def polish(
        self,
        targets: list[str],
        *,
        glossary_terms: list[GlossaryTerm] | None = None,
        style: str = "",
        next_source: str = "",
        sources: list[str] | None = None,
    ) -> list[str]:
        """Polish an aligned list in a fresh conversation; return input unchanged on failure."""
        polished = self.polish_or_none(
            targets,
            glossary_terms=glossary_terms,
            style=style,
            next_source=next_source,
            sources=sources,
        )
        return list(targets) if polished is None else polished

    def _polish_once(
        self,
        targets: list[str],
        *,
        glossary_terms: list[GlossaryTerm] | None = None,
        style: str = "",
        next_source: str = "",
        sources: list[str] | None = None,
    ) -> list[str] | object:
        """Send one standalone polish request and distinguish misalignment from provider failure."""
        n = len(targets)
        system_template = self.render(
            "polisher_system", src=self.src, tgt=self.tgt, n=n
        ) + prompts.protected_object_guidance(targets)
        note_source = "\n".join(sources or []) if self.config.pipeline.use_glossary_notes else None
        numbered_target = prompts.numbered(targets)
        next_ref = prompts.render_source_reference(next_source)

        def _render(terms_for_prompt: list[GlossaryTerm]) -> tuple[str, str]:
            return (
                system_template,
                self.render(
                    "polisher_user",
                    src=self.src,
                    tgt=self.tgt,
                    glossary=prompts.render_glossary(
                        terms_for_prompt,
                        source_lang=self.src,
                        genre=self.language_policy.context.genre,
                        note_source=note_source,
                    ),
                    style=style or "(none)",
                    n=n,
                    numbered_target=numbered_target,
                    next_source=next_ref,
                ),
            )

        match_text = "\n".join(part for part in (*(sources or ()), next_source) if part)
        system, user, _effective_terms, _shrunk = fit_prompt_to_context_window(
            config=self.config,
            operation="polish.body",
            terms=list(glossary_terms or ()),
            match_text=match_text,
            render_messages=_render,
        )
        try:
            items = self._ask_json(
                system,
                user,
                operation="polish.body",
                key="polished",
                item_type=str,
            )
        except (json.JSONDecodeError, ValueError, TruncatedResponseError):
            return _MISALIGNED
        except Exception as error:
            if is_resumable_provider_interrupt(error):
                raise
            _LOGGER.warning(
                "Agent call failed; returning fallback (operation=polish.body): %s: %s",
                type(error).__name__,
                error,
            )
            return _PROVIDER_ERROR
        if isinstance(items, list) and len(items) == n:
            return items
        return _MISALIGNED

    def polish_or_none(
        self,
        targets: list[str],
        *,
        glossary_terms: list[GlossaryTerm] | None = None,
        style: str = "",
        next_source: str = "",
        sources: list[str] | None = None,
    ) -> list[str] | None:
        """Polish an aligned list, retrying misalignment and splitting halves before giving up."""
        if not targets:
            return []
        attempts = max(0, self.config.pipeline.align_retry_limit) + 1
        for _ in range(attempts):
            result = self._polish_once(
                targets,
                glossary_terms=glossary_terms,
                style=style,
                next_source=next_source,
                sources=sources,
            )
            if result is _PROVIDER_ERROR:
                return None
            if isinstance(result, list):
                return result
        if len(targets) <= 1:
            return None
        mid = len(targets) // 2
        left_sources = sources[:mid] if sources is not None else None
        right_sources = sources[mid:] if sources is not None else None
        left_next = sources[mid] if sources is not None and mid < len(sources) else ""
        left = self.polish_or_none(
            targets[:mid],
            glossary_terms=glossary_terms,
            style=style,
            next_source=left_next,
            sources=left_sources,
        )
        if left is None:
            return None
        right = self.polish_or_none(
            targets[mid:],
            glossary_terms=glossary_terms,
            style=style,
            next_source=next_source,
            sources=right_sources,
        )
        if right is None:
            return None
        return [*left, *right]

    def polish_continue(
        self,
        turn: Messages,
        *,
        n: int,
        next_source: str = "",
    ) -> list[str] | None:
        """Append a polish user turn to a successful translation transcript.

        Returns the polished list on success, or ``None`` when the continuation fails so the
        caller can fall back to a standalone polish call or keep the raw translations.
        """
        if n <= 0 or len(turn) < 3:
            return None
        continue_user = self.render(
            "polisher_continue_user",
            src=self.src,
            tgt=self.tgt,
            n=n,
            next_source=prompts.render_source_reference(next_source),
        ) + prompts.protected_object_guidance([message["content"] for message in turn])
        messages: Messages = [
            *turn,
            {"role": "user", "content": continue_user},
        ]
        items = self._ask_json_messages(
            messages,
            operation="polish.body",
            key="polished",
            default=None,
            item_type=str,
        )
        if isinstance(items, list) and len(items) == n:
            return items
        return None
