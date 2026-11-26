"""Explicit batch inputs and translate/polish results without formal state mutation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass

from ...agents.base import Messages
from ...agents.batch_risk import has_embedded_verse
from ...agents.polisher import Polisher
from ...agents.sparse_editor import SparseSemanticEditor
from ...agents.translator import Translator
from ...ingest.segmenter import batch_segments, verse_block_indices
from ...llm.retrying import is_resumable_provider_interrupt
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
    variants_by_source: Mapping[str, Sequence[str]] | None = None

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
        variants_by_source: Mapping[str, Sequence[str]] | None = None,
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
            dict(variants_by_source) if variants_by_source else None,
        )


@dataclass(frozen=True)
class BatchResult:
    targets: tuple[str, ...]
    before_polish: tuple[str | None, ...]
    precision_key: str | None = None
    polish_failed: bool = False
    polish_empty_reverted: tuple[int, ...] = ()
    polish_interrupted: BaseException | None = None


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


@dataclass(frozen=True)
class BatchDraft:
    """An unpolished batch translation plus the transcript a polish call may continue."""

    targets: tuple[str, ...]
    turn: Messages | None = None
    turn_indices: tuple[int, ...] | None = None


@dataclass(frozen=True)
class PolishJob:
    """Detached inputs for polishing one saved draft, possibly on another thread."""

    chapter: int
    start_index: int
    segment_indices: tuple[int, ...]
    sources: tuple[str, ...]
    terms: tuple[GlossaryTerm, ...]
    style: str
    next_source: str
    draft: BatchDraft
    variants_by_source: Mapping[str, Sequence[str]] | None = None

    @classmethod
    def from_plan(cls, plan: BatchPlan, draft: BatchDraft) -> PolishJob:
        return cls(
            plan.chapter,
            plan.start_index,
            plan.segment_indices,
            plan.sources,
            plan.terms,
            plan.style,
            plan.next_source,
            draft,
            plan.variants_by_source,
        )


class TranslationBatchExecutor:
    def __init__(
        self,
        translator: Translator,
        polisher: Polisher,
        sparse_editor: SparseSemanticEditor | None = None,
    ):
        self._translator = translator
        self._polisher = polisher
        self._sparse_editor = sparse_editor

    def execute(
        self,
        plan: BatchPlan,
        *,
        polish: bool,
        polish_mode: str = "full",
    ) -> BatchResult:
        """Translate and optionally polish; the chapter service alone applies the result."""
        draft = self.translate(plan)
        targets = list(draft.targets)
        before_polish: tuple[str | None, ...] = (None,) * len(targets)
        polish_failed = False
        polish_empty_reverted: tuple[int, ...] = ()
        polish_interrupted: BaseException | None = None
        if polish:
            before_polish = tuple(targets)
            try:
                if polish_mode == "sparse" and self._sparse_editor is not None:
                    sparse = self._sparse_editor.edit_batch(
                        plan.sources,
                        targets,
                        glossary_terms=list(plan.terms),
                        style=plan.style,
                        next_source=plan.next_source,
                        variants_by_source=plan.variants_by_source,
                    )
                    polished = None if sparse.failed else list(sparse.targets)
                else:
                    polished = self._continue_polish(draft, plan.next_source)
                    if polished is None:
                        polished = self._polisher.polish_or_none(
                            targets,
                            glossary_terms=list(plan.terms),
                            style=plan.style,
                            next_source=plan.next_source,
                            sources=list(plan.sources),
                        )
            except BaseException as exc:  # noqa: BLE001 - budget/cancel stops must still save the draft.
                if isinstance(exc, Exception) and not is_resumable_provider_interrupt(exc):
                    raise
                polish_failed = True
                polish_interrupted = exc
            else:
                if polished is not None and len(polished) == len(targets):
                    targets, polish_empty_reverted = keep_nonempty_drafts(targets, polished)
                else:
                    polish_failed = True
        return BatchResult(
            tuple(targets),
            before_polish,
            polish_failed=polish_failed,
            polish_empty_reverted=polish_empty_reverted,
            polish_interrupted=polish_interrupted,
        )

    def translate(self, plan: BatchPlan) -> BatchDraft:
        """Translate one batch thread-safely and return its per-call transcript."""
        context_for_model = plan.context
        has_couplets = bool(verse_block_indices(plan.sources))
        has_embedded = any(
            has_embedded_verse(src, source_lang=self._translator.src) for src in plan.sources
        )
        if has_couplets or has_embedded:
            verse_note = (
                "[Verse formatting note: this batch contains verse couplets or embedded poems/lyrics. "
                "Render each verse line/couplet with explicit \\n line breaks, never inline prose or ' / '.]"
            )
            context_for_model = (
                f"{context_for_model}\n\n{verse_note}".strip() if context_for_model else verse_note
            )

        targets, turn, indices = self._translator.translate_batch_with_transcript(
            list(plan.sources),
            glossary_terms=list(plan.terms),
            style=plan.style,
            context=context_for_model,
            book_synopsis=plan.book_synopsis,
            chapter_digest=plan.chapter_digest,
            annotation_contexts=plan.annotation_contexts,
            next_source=plan.next_source,
            allow_empty_translations=plan.allow_empty_translations,
        )
        cleaned_targets = [strip_ruby_markers(target) for target in targets]
        return BatchDraft(
            tuple(cleaned_targets),
            turn if turn is not None and indices is not None else None,
            tuple(indices) if turn is not None and indices is not None else None,
        )

    def polish(self, job: PolishJob, *, polish_mode: str = "full") -> tuple[str, ...] | None:
        """Polish a saved draft without touching state; ``None`` keeps the draft."""
        if polish_mode == "sparse" and self._sparse_editor is not None:
            sparse = self._sparse_editor.edit_batch(
                job.sources,
                job.draft.targets,
                glossary_terms=list(job.terms),
                style=job.style,
                next_source=job.next_source,
                variants_by_source=job.variants_by_source,
            )
            if sparse.failed or len(sparse.targets) != len(job.draft.targets):
                return None
            return sparse.targets
        polished = self._continue_polish(job.draft, job.next_source)
        if polished is None:
            polished = self._polisher.polish_or_none(
                list(job.draft.targets),
                glossary_terms=list(job.terms),
                style=job.style,
                next_source=job.next_source,
                sources=list(job.sources),
            )
        if polished is None or len(polished) != len(job.draft.targets):
            return None
        return tuple(polished)

    def _continue_polish(self, draft: BatchDraft, next_source: str) -> list[str] | None:
        """Continue the translation conversation and restore filtered source positions."""
        if draft.turn is None or draft.turn_indices is None:
            return None
        indices = draft.turn_indices
        continued = self._polisher.polish_continue(
            draft.turn,
            n=len(indices),
            next_source=next_source,
        )
        if continued is None or len(continued) != len(indices):
            return None
        polished = list(draft.targets)
        for index, text in zip(indices, continued):
            polished[index] = strip_ruby_markers(text)
        return polished


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
