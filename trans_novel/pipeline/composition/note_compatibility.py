"""检查持久化 EPUB 注释槽位的应用层兼容性。"""

from __future__ import annotations

from collections.abc import Callable

from trans_novel.ingest import Chapter, Document
from trans_novel.ingest.epub.note_migration import reconcile_note_slots
from trans_novel.ingest.epub.reader import ensure_slot_compatibility
from trans_novel.pipeline.state import (
    IdentityMismatchError,
    RunStore,
    normalize_lang_code,
    source_bytes_hash,
)
from trans_novel.pipeline.state.models import NODE_SUCCEEDED, TRANSLATION_POLICY_VERSION
from trans_novel.pipeline.state.note_eligibility import completed_legacy_note_content
from trans_novel.pipeline.state.note_migration import commit_note_migration

FingerprintUpdates = Callable[[list[Chapter], list[Chapter]], dict[str, tuple[str, str]]]


def _migration_needed(doc: Document, manifest: dict) -> bool:
    meta = manifest.get("meta")
    if not isinstance(meta, dict) or meta.get("epub_note_slots_version") != 1:
        return True
    return meta.get("epub_notes") != doc.meta.get("epub_notes")


def _preflight_identity(
    store: RunStore, identity_path: str, source_lang: str, target_lang: str
) -> None:
    raw = store.read_json(store.manifest_path)
    identity = raw.get("identity") if isinstance(raw, dict) else None
    if not isinstance(identity, dict):
        raise IdentityMismatchError("运行状态缺少源文件校验信息，无法确认与当前输入一致")
    expected, actual = identity.get("source_bytes_sha256"), source_bytes_hash(identity_path)
    if not expected or not actual:
        raise IdentityMismatchError("无法读取当前源文件，不能核验运行身份")
    if expected != actual:
        raise IdentityMismatchError("源文件内容与运行状态不一致（文件被修改或不是同一本书）")
    for label, configured, saved in (
        ("源语言", source_lang, identity.get("source_lang")),
        ("目标语言", target_lang, identity.get("target_lang")),
    ):
        if configured and saved and normalize_lang_code(configured) != saved:
            raise IdentityMismatchError(f"{label}不一致（状态 {saved}，当前 {configured}）")


def ensure_epub_note_compatibility(
    store: RunStore,
    doc: Document,
    *,
    identity_path: str,
    source_lang: str,
    target_lang: str,
    allow_migration: bool,
    fingerprint_updates: FingerprintUpdates | None = None,
    require_completed_content: bool = False,
) -> None:
    """严格校验当前槽位，或在满足条件时原子迁移旧布局。"""
    if not store.exists() or doc.fmt != "epub":
        return
    source_lang = normalize_lang_code(source_lang)
    target_lang = normalize_lang_code(target_lang)
    _preflight_identity(store, identity_path, source_lang, target_lang)
    with store.lock():
        state = store.load_state()
        store.verify_identity(
            source_bytes_sha256=source_bytes_hash(identity_path),
            source_lang=source_lang,
            target_lang=target_lang,
        )
        if state.fmt != "epub" or state.meta.get("epub_schema") != 4:
            raise ValueError("EPUB note migration rejected: persisted state is not schema-4 EPUB")
        if state.meta.get("epub_sha256") != doc.meta.get("epub_sha256"):
            raise ValueError("EPUB note migration rejected: source archive identity changed")
        policy_version = state.identity.translation_policy_version
        if policy_version > TRANSLATION_POLICY_VERSION:
            raise ValueError("EPUB note migration rejected: future translation policy")
        persisted = [store.load_chapter(chapter.index) for chapter in state.chapters]
        if not _migration_needed(doc, state.model_dump(mode="json")):
            ensure_slot_compatibility(doc, persisted)
            return
        if not allow_migration:
            ensure_slot_compatibility(doc, persisted)
            return
        if (
            policy_version < TRANSLATION_POLICY_VERSION or require_completed_content
        ) and not completed_legacy_note_content(state, persisted):
            raise ValueError(
                "EPUB note migration rejected: run is not complete output-ready content"
            )
        migrated = reconcile_note_slots(doc, persisted)
        updates: dict[str, tuple[str, str]] | None = None
        if policy_version == TRANSLATION_POLICY_VERSION:
            content_nodes = {
                "analyze",
                "mine_terms",
                "name_terms",
                "translate",
                "polish",
                "titles",
                "deterministic_qa",
                "repair",
            }
            succeeded = any(
                node.status == NODE_SUCCEEDED and key.partition(":")[0] in content_nodes
                for key, node in state.nodes.items()
            )
            if succeeded:
                if fingerprint_updates is None:
                    raise ValueError(
                        "EPUB note migration rejected: current-policy fingerprints require rebasing"
                    )
                updates = fingerprint_updates(persisted, migrated)
        meta = {
            **state.meta,
            "epub_notes": doc.meta.get("epub_notes"),
            "epub_note_slots_version": 1,
        }
        commit_note_migration(store, chapters=migrated, meta=meta, fingerprint_updates=updates)


__all__ = ["ensure_epub_note_compatibility"]
