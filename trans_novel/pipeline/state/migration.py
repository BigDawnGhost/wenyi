"""Atomic migration of legacy V1/V2 run stores to the current schema."""

from __future__ import annotations

import os
from typing import Any

from trans_novel.ingest import Chapter
from trans_novel.pipeline.state.models import (
    NODE_ANALYZE,
    NODE_MINE_TERMS,
    NODE_NAME_TERMS,
    NODE_POLISH,
    NODE_PREPARE,
    NODE_SUCCEEDED,
    NODE_TITLES,
    NODE_TRANSLATE,
    RUN_STATE_SCHEMA_VERSION,
    STATUS_DONE,
    AnalysisFlags,
    ChapterIndex,
    ChapterProgress,
    NodeState,
    PolishBatch,
    RunIdentity,
    RunState,
    chapter_node_key,
    source_bytes_hash,
)
from trans_novel.postprocess.language import normalize_lang_code

_PIPELINE_META_KEYS = frozenset(
    {
        "source_digest",
        "pending_polish",
        "naturalized",
        "review_issues",
        "backtranslation_issues",
        "backtranslation_sample_key",
        "backtranslation_sample_indices",
        "back_matter_mode",
        "review_pending",
    }
)


def _identity(manifest: dict[str, Any]) -> RunIdentity:
    return RunIdentity(
        source_bytes_sha256=source_bytes_hash(str(manifest.get("source_path") or "")),
        run_input_schema_version=1,
        source_lang=normalize_lang_code(manifest.get("source_lang", "")),
        target_lang=normalize_lang_code(manifest.get("target_lang", "")),
    )


def _chapters(
    manifest: dict[str, Any], *, progress_data: dict[str, Any] | None = None
) -> tuple[list[ChapterIndex], dict[int, ChapterProgress]]:
    chapters: list[ChapterIndex] = []
    progress: dict[int, ChapterProgress] = {}
    progress_data = progress_data if isinstance(progress_data, dict) else {}
    for raw in manifest.get("chapters", []):
        if not isinstance(raw, dict) or not isinstance(raw.get("index"), int):
            raise ValueError("状态 manifest 的 chapters 条目损坏（缺少整数 index）")
        ci = raw["index"]
        chapters.append(
            ChapterIndex(
                index=ci,
                title=str(raw.get("title", "") or ""),
                href=raw.get("href"),
                toc_entry_id=raw.get("toc_entry_id"),
                title_translated=raw.get("title_translated"),
            )
        )
        legacy = progress_data.get(str(ci), progress_data.get(ci))
        status = legacy.get("status") if isinstance(legacy, dict) else raw.get("status")
        progress[ci] = ChapterProgress(status="done" if status == "done" else "pending")
    return chapters, progress


def _legacy_lint_issues(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    return [
        item for item in raw if isinstance(item, dict) and item.get("stage") in {"lint", "length"}
    ]


def _copy_chapter(store, ci: int, progress: ChapterProgress, *, v1: bool) -> None:
    path = store.chapter_path_v1(ci) if v1 else store.chapter_path_v2(ci)
    if not os.path.isfile(path):
        raise ValueError(f"状态缺少章节文件：{path}")
    raw = store.read_json(path)
    chapter = Chapter.from_dict(raw)
    meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    if v1:
        progress.pending_polish = [
            PolishBatch.model_validate(item)
            for item in meta.get("pending_polish", [])
            if isinstance(item, dict)
        ]
        progress.lint_issues = _legacy_lint_issues(meta.get("review_issues"))
        mode = meta.get("back_matter_mode")
        progress.back_matter_mode = mode if isinstance(mode, str) and mode else None
    chapter.meta = {k: v for k, v in meta.items() if k not in _PIPELINE_META_KEYS}
    store.write_json(store.chapter_path_v2(ci), chapter.to_dict())


def _state_from_v1(store, manifest: dict[str, Any]) -> RunState:
    chapters, progress = _chapters(manifest)
    for ci, pg in progress.items():
        _copy_chapter(store, ci, pg, v1=True)
    analysis = store.read_json(store.analysis_path) if os.path.isfile(store.analysis_path) else {}
    analysis = analysis if isinstance(analysis, dict) else {}
    nodes: dict[str, NodeState] = {
        NODE_PREPARE: NodeState(node_id=NODE_PREPARE, status=NODE_SUCCEEDED),
        NODE_ANALYZE: NodeState(node_id=NODE_ANALYZE, status=NODE_SUCCEEDED),
    }
    if bool(analysis.get("term_mining_done")):
        nodes[NODE_MINE_TERMS] = NodeState(node_id=NODE_MINE_TERMS, status=NODE_SUCCEEDED)
        nodes[NODE_NAME_TERMS] = NodeState(node_id=NODE_NAME_TERMS, status=NODE_SUCCEEDED)
    for ci, pg in progress.items():
        if pg.status == STATUS_DONE:
            nodes[chapter_node_key(NODE_TRANSLATE, ci)] = NodeState(
                node_id=chapter_node_key(NODE_TRANSLATE, ci), status=NODE_SUCCEEDED
            )
            nodes[chapter_node_key(NODE_POLISH, ci)] = NodeState(
                node_id=chapter_node_key(NODE_POLISH, ci), status="skipped"
            )
    return RunState(
        identity=_identity(manifest),
        title=str(manifest.get("title", "") or ""),
        fmt=str(manifest.get("fmt", "") or ""),
        source_path=str(manifest.get("source_path", "") or ""),
        source_lang=str(manifest.get("source_lang", "") or ""),
        target_lang=str(manifest.get("target_lang", "") or ""),
        meta=manifest.get("meta") if isinstance(manifest.get("meta"), dict) else {},
        initialized=bool(manifest.get("initialized")),
        chapters=chapters,
        progress=progress,
        nodes=nodes,
        analysis_flags=AnalysisFlags(
            term_mining_done=bool(analysis.get("term_mining_done")),
        ),
    )


def migrate_v1_to_v2(store) -> RunState:
    """Compatibility entry point: migrate V1 through the V2 layout to V3."""
    state = _state_from_v1(store, store.read_json(store.manifest_path))
    return _write_state(store, state)


def migrate_v3_to_v4(store) -> RunState:
    """Add the issue-level Repair ledger without touching chapter targets."""
    state = RunState.model_validate(store.read_json(store.manifest_path))
    return _write_state(store, state)


def migrate_v2_to_v3(store) -> RunState:
    manifest = store.read_json(store.manifest_path)
    raw_progress = manifest.get("progress")
    chapters, progress = _chapters(
        manifest, progress_data=raw_progress if isinstance(raw_progress, dict) else None
    )
    for ci, pg in progress.items():
        _copy_chapter(store, ci, pg, v1=False)
        raw = (
            raw_progress.get(str(ci), raw_progress.get(ci))
            if isinstance(raw_progress, dict)
            else {}
        )
        if isinstance(raw, dict):
            pending = raw.get("pending_polish", [])
            pg.pending_polish = [
                PolishBatch.model_validate(item) for item in pending if isinstance(item, dict)
            ]
            pg.lint_issues = _legacy_lint_issues(raw.get("review_issues"))
            mode = raw.get("back_matter_mode")
            pg.back_matter_mode = mode if isinstance(mode, str) and mode else None
        progress[ci] = pg

    nodes: dict[str, NodeState] = {}
    raw_nodes = manifest.get("nodes") if isinstance(manifest.get("nodes"), dict) else {}
    for key, raw in raw_nodes.items():
        if not isinstance(raw, dict):
            continue
        base = str(key).split(":", 1)[0]
        if base not in {
            NODE_PREPARE,
            NODE_ANALYZE,
            NODE_MINE_TERMS,
            NODE_NAME_TERMS,
            NODE_TRANSLATE,
            NODE_POLISH,
            NODE_TITLES,
        }:
            continue
        nodes[key] = NodeState.model_validate(raw)
        if nodes[key].status == NODE_SUCCEEDED:
            nodes[key].input_fingerprint = ""
    flags = (
        manifest.get("analysis_flags") if isinstance(manifest.get("analysis_flags"), dict) else {}
    )
    state = RunState(
        identity=RunIdentity.model_validate(manifest.get("identity") or {}),
        title=str(manifest.get("title", "") or ""),
        fmt=str(manifest.get("fmt", "") or ""),
        source_path=str(manifest.get("source_path", "") or ""),
        source_lang=str(manifest.get("source_lang", "") or ""),
        target_lang=str(manifest.get("target_lang", "") or ""),
        meta=manifest.get("meta") if isinstance(manifest.get("meta"), dict) else {},
        initialized=bool(manifest.get("initialized")),
        chapters=chapters,
        progress=progress,
        nodes=nodes,
        analysis_flags=AnalysisFlags(term_mining_done=bool(flags.get("term_mining_done"))),
    )
    return _write_state(store, state)


def _write_state(store, state: RunState) -> RunState:
    state.run_state_schema = RUN_STATE_SCHEMA_VERSION
    state = RunState.model_validate(state.model_dump(mode="json"))
    store.write_json(store.manifest_path, state.model_dump(mode="json"))
    journal = store.journal_path
    if os.path.isfile(journal):
        raw = store.read_json(journal)
        if not isinstance(raw, dict) or raw.get("node") in {
            "naturalize",
        }:
            os.remove(journal)
    return state


__all__ = ["migrate_v1_to_v2", "migrate_v2_to_v3", "migrate_v3_to_v4"]
