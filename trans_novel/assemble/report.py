"""翻译报告：汇总正式流水线中需要人工关注的持久化问题。"""

from __future__ import annotations

from typing import Any

from ..glossary.store import GlossaryStore
from ..pipeline.runstore import STATUS_DONE, RunStore


def build_report(store: RunStore, glossary: GlossaryStore) -> dict[str, Any]:
    """汇总完成进度、空译文、术语冲突和最新 Review 摘要。"""
    m = store.load_manifest()
    chapters_total = len(m["chapters"])
    chapters_done = sum(1 for c in m["chapters"] if c["status"] == STATUS_DONE)

    empty_targets: list[dict] = []

    for c in m["chapters"]:
        if c["status"] != STATUS_DONE:
            continue
        ch = store.load_chapter(c["index"])
        for s in ch.text_segments:
            if not (s.target and s.target.strip()):
                empty_targets.append(
                    {"chapter": c["index"], "index": s.index, "source": s.source[:60]}
                )

    conflicts = glossary.open_conflicts()
    gstats = glossary.stats()

    report: dict[str, Any] = {
        "summary": {
            "chapters_total": chapters_total,
            "chapters_done": chapters_done,
            "terms": gstats["terms"],
            "open_conflicts": len(conflicts),
            "empty_targets": len(empty_targets),
        },
        "open_conflicts": conflicts,
        "empty_targets": empty_targets,
    }
    review = store.load_latest_review_result()
    if review is not None:
        review_summary = review.get("summary") or {}
        report["review"] = {
            "review_id": review.get("review_id"),
            "status": review.get("status"),
            "termination": review.get("termination"),
            "issue_count": int(review_summary.get("issue_count") or 0),
            "change_count": int(review_summary.get("change_count") or 0),
            "read_only": True,
        }
    return report
