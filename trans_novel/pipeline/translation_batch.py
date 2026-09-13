"""Explicit batch inputs and translate/polish results without formal state mutation."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

from ..agents.polisher import Polisher
from ..agents.translator import Translator
from ..glossary.store import GlossaryTerm
from ..ingest.models import Segment
from ..ingest.segmenter import batch_segments
from ..markup.ruby import strip_ruby_markers


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
        )


@dataclass(frozen=True)
class BatchResult:
    targets: tuple[str, ...]
    before_polish: tuple[str | None, ...]


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
        )
        targets = [strip_ruby_markers(target) for target in targets]
        before_polish: tuple[str | None, ...] = (None,) * len(targets)
        if polish:
            before_polish = tuple(targets)
            polished = self._polisher.polish(
                targets, glossary_terms=terms, style=plan.style, next_source=plan.next_source
            )
            if len(polished) == len(targets):
                targets = polished
        return BatchResult(tuple(targets), before_polish)


def resume_batches(segments: list[Segment], max_chars: int) -> list[list[Segment]]:
    """Split character-budget batches again at completed/pending boundaries.
    A changed budget may mix saved translations and empty targets in one batch. Group by
    completion state to translate only missing paragraphs and avoid overwriting confirmed
    content.
    """
    batches: list[list[Segment]] = []
    for raw_batch in batch_segments(segments, max_chars):
        current: list[Segment] = []
        current_done: bool | None = None
        for segment in raw_batch:
            done = bool(segment.target and segment.target.strip())
            if current and done != current_done:
                batches.append(current)
                current = []
            current.append(segment)
            current_done = done
        if current:
            batches.append(current)
    return batches
