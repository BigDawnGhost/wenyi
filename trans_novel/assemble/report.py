"""QA 报告：把所有需要人工关注的点集中汇总。

人工只需看这一处，即可裁决术语冲突、补查疑似漏译/误译。
"""

from __future__ import annotations

from typing import Any

from ..glossary.store import GlossaryStore
from ..pipeline.runstore import REVIEW_DONE, RunStore, STATUS_DONE


def build_report(store: RunStore, glossary: GlossaryStore) -> dict[str, Any]:
    """汇总完成进度、空译文、术语冲突、审校和回译问题。"""
    m = store.load_manifest()
    chapters_total = len(m["chapters"])
    chapters_done = sum(1 for c in m["chapters"] if c["status"] == STATUS_DONE)
    chapters_reviewed = sum(
        1
        for chapter in m["chapters"]
        if chapter.get("review_status") == REVIEW_DONE
    )

    review_issues: list[dict] = []
    bt_issues: list[dict] = []
    empty_targets: list[dict] = []

    for c in m["chapters"]:
        if c["status"] != STATUS_DONE:
            continue
        ch = store.load_chapter(c["index"])
        if c.get("review_status") == REVIEW_DONE:
            review_issues.extend(ch.meta.get("review_issues", []))
        bt_issues.extend(ch.meta.get("backtranslation_issues", []))
        for s in ch.text_segments:
            if not (s.target and s.target.strip()):
                empty_targets.append({"chapter": c["index"], "index": s.index,
                                      "source": s.source[:60]})

    conflicts = glossary.open_conflicts()
    gstats = glossary.stats()

    return {
        "summary": {
            "chapters_total": chapters_total,
            "chapters_done": chapters_done,
            "chapters_reviewed": chapters_reviewed,
            "terms": gstats["terms"],
            "open_conflicts": len(conflicts),
            "review_issues": len(review_issues),
            "backtranslation_issues": len(bt_issues),
            "empty_targets": len(empty_targets),
        },
        "open_conflicts": conflicts,
        "review_issues": review_issues,
        "backtranslation_issues": bt_issues,
        "empty_targets": empty_targets,
    }
