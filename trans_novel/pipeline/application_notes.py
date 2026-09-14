"""协调 Application 的 EPUB 注释状态兼容性检查。"""

from __future__ import annotations

from functools import partial

from trans_novel.pipeline.composition import (
    ensure_epub_note_compatibility,
    ensure_service_epub_note_compatibility,
)
from trans_novel.pipeline.contracts import GOAL_RUN_ALL, GOAL_TRANSLATE
from trans_novel.pipeline.planning import note_fingerprint_updates


def ensure_document_notes(application, store, doc, identity_path: str, goal, context) -> None:
    full_resume = (
        goal.phases in (GOAL_TRANSLATE.phases, GOAL_RUN_ALL.phases)
        and goal.only_chapter is None
        and application.frozen_preparation is None
    )
    ensure_epub_note_compatibility(
        store,
        doc,
        identity_path=identity_path,
        source_lang=application.config.source_lang,
        target_lang=application.config.target_lang,
        allow_migration=full_resume,
        fingerprint_updates=partial(note_fingerprint_updates, application.config, store, context),
    )


def ensure_service_notes(application, store, goal, input_path, output) -> None:
    ensure_service_epub_note_compatibility(
        store,
        config=application.config,
        client=application.client,
        goal=goal,
        identity_path=input_path,
        output=output,
        fingerprint_updates=partial(note_fingerprint_updates, application.config, store),
    )


__all__ = ["ensure_document_notes", "ensure_service_notes"]
