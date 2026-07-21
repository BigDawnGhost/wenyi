"""术语冲突的人工裁决辅助（供 CLI 使用）。

自动冲突判定在 GlossaryStore.upsert_term 内完成；这里提供"人工拍板"的封装：
确定某词的最终译法，并把相关冲突标记为已解决。
"""

from __future__ import annotations

from .store import GlossaryStore


def resolve(store: GlossaryStore, source: str, target: str) -> bool:
    """裁定 source 的最终译法并清除冲突标记，返回术语是否存在。"""
    if not store.resolve_term(source, target):
        return False
    store.mark_conflicts_resolved(source)
    return True


def pending_review(store: GlossaryStore) -> dict:
    """汇总需要人工关注的译法冲突。"""
    return {"conflicts": store.open_conflicts()}
