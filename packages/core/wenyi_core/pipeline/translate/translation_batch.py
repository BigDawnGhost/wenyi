"""Explicit batch inputs and translate/polish results without formal state mutation."""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass

from ...agents.polisher import Polisher
from ...agents.translator import Translator
from ...ingest.segmenter import batch_segments
from ...markup.ruby import strip_ruby_markers
from ...models.document import Segment
from ...models.glossary import GlossaryTerm


@dataclass(frozen=True)
class BatchPlan:
    """Capture the source positions and context used for exactly one serial batch."""

    chapter: int
    start_index: int
    segment_indices: tuple[int, ...]
    sources: tuple[str, ...]
    terms: tuple[GlossaryTerm, ...]
    context: str
    style: str
    book_synopsis: str
    chapter_digest: str
    annotation_contexts: list[list[dict[str, str]]]
    next_source: str
    allow_empty_translations: bool = False

    @classmethod
    def capture(
        cls,
        chapter: int,
        start_index: int,
        segments: list[Segment],
        terms: list[GlossaryTerm],
        context: str,
        style: str,
        book_synopsis: str,
        chapter_digest: str,
        annotation_contexts: list[list[dict[str, str]]],
        next_source: str,
        *,
        allow_empty_translations: bool = False,
    ) -> BatchPlan:
        """Detach mutable term and annotation metadata from subsequent chapter updates."""
        return cls(
            chapter,
            start_index,
            tuple(s.index for s in segments),
            tuple(s.source for s in segments),
            tuple(deepcopy(terms)),
            context,
            style,
            book_synopsis,
            chapter_digest,
            deepcopy(annotation_contexts),
            next_source,
            allow_empty_translations,
        )


@dataclass(frozen=True)
class BatchResult:
    targets: tuple[str, ...]
    before_polish: tuple[str | None, ...]
    precision_key: str | None = None
    polish_failed: bool = False
    polish_empty_reverted: tuple[int, ...] = ()


def keep_nonempty_drafts(
    drafts: Sequence[str], polished: Sequence[str]
) -> tuple[list[str], tuple[int, ...]]:
    """Never let polish blank a non-empty draft; keep the draft and record reverted indices."""
    cleaned: list[str] = []
    reverted: list[int] = []
    for index, (draft, item) in enumerate(zip(drafts, polished)):
        if draft.strip() and not item.strip():
            cleaned.append(draft)
            reverted.append(index)
        else:
            cleaned.append(item)
    return cleaned, tuple(reverted)


class TranslationBatchExecutor:
    def __init__(self, translator: Translator, polisher: Polisher):
        self._translator = translator
        self._polisher = polisher

    def execute(self, plan: BatchPlan, *, polish: bool) -> BatchResult:
        """Translate and optionally polish; the chapter service alone applies the result."""
        terms = list(plan.terms)
        targets = self._translator.translate_batch(
            list(plan.sources),
            glossary_terms=terms,
            style=plan.style,
            context=plan.context,
            book_synopsis=plan.book_synopsis,
            chapter_digest=plan.chapter_digest,
            annotation_contexts=plan.annotation_contexts,
            next_source=plan.next_source,
            allow_empty_translations=plan.allow_empty_translations,
        )
        targets = [strip_ruby_markers(target) for target in targets]
        before_polish: tuple[str | None, ...] = (None,) * len(targets)
        polish_failed = False
        polish_empty_reverted: tuple[int, ...] = ()
        if polish:
            before_polish = tuple(targets)
            turn = self._translator.last_batch_turn
            indices = self._translator.last_batch_indices
            polished: list[str] | None = None
            if turn is not None and indices is not None:
                # Continue the translation conversation and restore filtered source positions.
                continued = self._polisher.polish_continue(
                    turn,
                    n=len(indices),
                    next_source=plan.next_source,
                )
                if continued is not None and len(continued) == len(indices):
                    polished = list(targets)
                    for index, text in zip(indices, continued):
                        polished[index] = strip_ruby_markers(text)
            if polished is None:
                polished = self._polisher.polish_or_none(
                    targets,
                    glossary_terms=terms,
                    style=plan.style,
                    next_source=plan.next_source,
                    sources=list(plan.sources),
                )
            if polished is not None and len(polished) == len(targets):
                targets, polish_empty_reverted = keep_nonempty_drafts(targets, polished)
            else:
                polish_failed = True
        return BatchResult(
            tuple(targets),
            before_polish,
            polish_failed=polish_failed,
            polish_empty_reverted=polish_empty_reverted,
        )

    def polish_batch(
        self,
        targets: Sequence[str],
        *,
        glossary_terms: Sequence[GlossaryTerm],
        style: str,
        next_source: str,
        sources: Sequence[str],
    ) -> tuple[list[str] | None, tuple[int, ...]]:
        """Polish already-translated drafts, preserving non-empty drafts if polish blanks them."""
        polished = self._polisher.polish_or_none(
            list(targets),
            glossary_terms=list(glossary_terms),
            style=style,
            next_source=next_source,
            sources=list(sources),
        )
        if polished is None or len(polished) != len(targets):
            return None, ()
        return keep_nonempty_drafts(targets, polished)


def resume_batches(segments: list[Segment], max_tokens: int) -> list[list[Segment]]:
    """Split token-budget batches again at completed/pending boundaries.
    A changed budget may mix saved translations and unset targets in one batch. Group by
    completion state to translate only missing paragraphs and avoid overwriting confirmed
    content. A saved blank string is complete; only None means pending.
    """
    batches: list[list[Segment]] = []
    for raw_batch in batch_segments(segments, max_tokens):
        current: list[Segment] = []
        current_done: bool | None = None
        for segment in raw_batch:
            done = segment.target is not None
            if current and done != current_done:
                batches.append(current)
                current = []
            current.append(segment)
            current_done = done
        if current:
            batches.append(current)
    return batches
