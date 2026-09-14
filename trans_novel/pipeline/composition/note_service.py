"""为仅执行输出的服务目标准备 EPUB 注释兼容性检查。"""

from __future__ import annotations

import os
from collections.abc import Callable

from trans_novel.ingest import Chapter, load_document
from trans_novel.pipeline.composition.agents import AgentBundle
from trans_novel.pipeline.composition.context import RunContext
from trans_novel.pipeline.composition.note_compatibility import ensure_epub_note_compatibility
from trans_novel.pipeline.state import RunStore


def ensure_service_epub_note_compatibility(
    store: RunStore,
    *,
    config,
    client,
    goal,
    identity_path: str | None,
    output,
    fingerprint_updates: Callable[
        [RunContext, list[Chapter], list[Chapter]], dict[str, tuple[str, str]]
    ],
) -> None:
    """仅当只执行输出的 EPUB 运行仍需迁移注释时加载源文件。"""
    if not set(goal.phases).issubset({"layout", "assemble"}):
        return
    if not store.exists():
        return
    raw = store.read_json(store.manifest_path)
    meta = raw.get("meta") if isinstance(raw, dict) else None
    if (
        not isinstance(raw, dict)
        or raw.get("fmt") != "epub"
        or (
            isinstance(meta, dict)
            and meta.get("epub_note_slots_version") == 1
            and not os.path.isfile(store.path_for("note_migration.json"))
        )
    ):
        return
    identity = raw.get("identity")
    identity = identity if isinstance(identity, dict) else {}
    source_path = identity_path or str(raw.get("source_path") or "")
    doc = load_document(
        source_path,
        config.source_lang,
        config.target_lang,
        split_segments=config.segment.max_chars_per_segment,
    )
    context = RunContext(
        store=store,
        config=config,
        doc=doc,
        agent_builder=lambda src, tgt: AgentBundle(client, config, src=src, tgt=tgt),
        output=output if output is not None else config.output.model_copy(deep=True),
        output_format=goal.out_format,
        output_relevant=True,
        identity_languages=(
            str(identity.get("source_lang") or ""),
            str(identity.get("target_lang") or ""),
        ),
    )
    try:
        ensure_epub_note_compatibility(
            store,
            doc,
            identity_path=source_path,
            source_lang=config.source_lang,
            target_lang=config.target_lang,
            allow_migration=True,
            require_completed_content=True,
            fingerprint_updates=lambda before, after: fingerprint_updates(context, before, after),
        )
    finally:
        context.close()


__all__ = ["ensure_service_epub_note_compatibility"]
