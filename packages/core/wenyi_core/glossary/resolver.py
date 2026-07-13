"""术语冲突的人工裁决辅助（供 CLI 使用）。

自动冲突判定在 GlossaryStore.upsert_term 内完成；这里提供"人工拍板"的封装：
确定某词的最终译法、锁定、并把相关冲突标记为已解决。
"""

from __future__ import annotations

from .store import GlossaryStore


def resolve(store: GlossaryStore, source: str, target: str) -> None:
    """裁定 source 的最终中文译法：覆盖、锁定、清除冲突标记。"""
    store.lock_term(source, target)
    store.mark_conflicts_resolved(source)


def lock(store: GlossaryStore, source: str) -> None:
    """锁定现有译法（不改 target），并清除冲突标记。"""
    store.lock_term(source)
    store.mark_conflicts_resolved(source)


def pending_review(store: GlossaryStore) -> dict:
    """汇总需要人工关注的项：冲突 + 低置信度术语。"""
    return {
        "conflicts": store.open_conflicts(),
        "low_confidence": [
            {"source": t.source, "target": t.target, "type": t.type,
             "confidence": t.confidence, "status": t.status}
            for t in store.low_confidence_terms()
        ],
    }
