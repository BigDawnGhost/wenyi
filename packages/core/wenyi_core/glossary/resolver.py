"""CLI helpers for human resolution of glossary conflicts.
GlossaryStore.upsert_term detects conflicts automatically. These wrappers choose the final
translation and mark related conflicts resolved.
"""

from __future__ import annotations

from .store import GlossaryStore


def resolve(store: GlossaryStore, source: str, target: str) -> bool:
    """Resolve a source term's final translation and clear conflicts; report whether the term
    exists.
    """
    if not store.resolve_term(source, target):
        return False
    store.mark_conflicts_resolved(source)
    return True
