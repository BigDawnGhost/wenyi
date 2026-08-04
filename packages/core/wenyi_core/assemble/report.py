"""QA 报告：汇总正式流水线中需要人工关注的持久化问题。"""

from __future__ import annotations

from typing import Any

from ..pipeline.runstore import STATUS_DONE, RunStore


def build_report(
    storage: RunStore,
    *,
    consistency_issues: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """汇总完成进度、空译文、术语冲突、回译问题与最新 Review 摘要。"""
    m = storage.load_manifest()
    chapters_total = len(m["chapters"])
    chapters_done = sum(1 for c in m["chapters"] if c["status"] == STATUS_DONE)

    bt_issues: list[dict] = []
    empty_targets: list[dict] = []

    for c in m["chapters"]:
        if c["status"] != STATUS_DONE:
            continue
        ch = storage.load_chapter(c["index"])
        bt_issues.extend(ch.meta.get("backtranslation_issues", []))
        for s in ch.text_segments:
            if not (s.target and s.target.strip()):
                empty_targets.append(
                    {"chapter": c["index"], "index": s.index, "source": s.source[:60]}
                )

    conflicts = storage.open_conflicts()
    low_conf = [
        {"source": t.source, "target": t.target, "type": t.type,
         "confidence": t.confidence, "status": t.status}
        for t in storage.low_confidence_terms()
    ]
    gstats = storage.stats()

    report: dict[str, Any] = {
        "summary": {
            "chapters_total": chapters_total,
            "chapters_done": chapters_done,
            "terms": gstats["terms"],
            "open_conflicts": len(conflicts),
            "backtranslation_issues": len(bt_issues),
            "empty_targets": len(empty_targets),
        },
        "open_conflicts": conflicts,
        "low_confidence_terms": low_conf,
        "backtranslation_issues": bt_issues,
        "empty_targets": empty_targets,
    }
    if consistency_issues is not None:
        report["consistency_issues"] = consistency_issues
    load_review = getattr(storage, "load_latest_review_result", None)
    review = load_review() if callable(load_review) else None
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
