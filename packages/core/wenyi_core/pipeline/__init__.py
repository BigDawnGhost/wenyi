"""Pipeline facade, shared runtime and domain services.

Stages are grouped by sub-stage so one phase's code sits in one place:

    run/        runtime ownership, usage checkpoints and language policy persistence
    prepare/    source parsing, language detection, prescan and book synthesis
    translate/  batches, polishing, terminology, titles, context and precision drafts
    annotate/   EPUB annotation alignment and DOCX style placement
    review/     read-only shadow review: chunks, rounds, checkpoints and results
    autofix/    verified publication of Review Autofix revisions
    finalize/   live and snapshot export

`orchestrator.py` stays at the top level: it is the facade every workflow enters through
and the only module allowed to assemble several stages at once.

Every public name is re-exported below. That keeps `wenyi_core.pipeline.<module>` valid as
an import path, so callers can move to the new sub-package paths at their own pace.
"""

from __future__ import annotations

# facade: the entry point every workflow goes through
from . import orchestrator  # noqa: F401

# annotate: EPUB annotation alignment and DOCX style placement
from .annotate import (
    annotations,  # noqa: F401
    docx_styles,  # noqa: F401
)

# autofix: verified publication of Review Autofix revisions
from .autofix import (
    autofix_candidates,  # noqa: F401
    autofix_plan,  # noqa: F401
    autofix_publish,  # noqa: F401
    autofix_verification,  # noqa: F401
    review_autofix,  # noqa: F401
)

# finalize: live and snapshot export
from .finalize import finalization  # noqa: F401
from .orchestrator import Orchestrator  # noqa: F401

# prepare: source parsing, language detection, prescan and book synthesis
from .prepare import (
    book_understanding,  # noqa: F401
    epub_objects,  # noqa: F401
    input_preparation,  # noqa: F401
    preparation,  # noqa: F401
)

# review: read-only shadow review: chunks, rounds, checkpoints and results
from .review import (
    review_checkpoint,  # noqa: F401
    review_chunks,  # noqa: F401
    review_results,  # noqa: F401
    review_rounds,  # noqa: F401
    review_workflow,  # noqa: F401
)

# run: runtime ownership, usage checkpoints and language policy persistence
from .run import (
    language_policies,  # noqa: F401
    runtime,  # noqa: F401
    runtime_usage,  # noqa: F401
)

# translate: batches, polishing, terminology, titles, context and precision drafts
from .translate import (
    babeldoc_context,  # noqa: F401
    context,  # noqa: F401
    precision,  # noqa: F401
    precision_records,  # noqa: F401
    terminology,  # noqa: F401
    terminology_metadata,  # noqa: F401
    title_translation,  # noqa: F401
    translation,  # noqa: F401
    translation_batch,  # noqa: F401
)

__all__ = [
    "runtime",
    "runtime_usage",
    "language_policies",
    "preparation",
    "input_preparation",
    "epub_objects",
    "book_understanding",
    "translation",
    "translation_batch",
    "title_translation",
    "terminology",
    "terminology_metadata",
    "context",
    "babeldoc_context",
    "precision",
    "precision_records",
    "annotations",
    "docx_styles",
    "review_workflow",
    "review_rounds",
    "review_chunks",
    "review_checkpoint",
    "review_results",
    "review_autofix",
    "autofix_candidates",
    "autofix_plan",
    "autofix_publish",
    "autofix_verification",
    "finalization",
    "orchestrator",
    "Orchestrator",
]
