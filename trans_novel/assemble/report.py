"""Deterministic QA report for a translated book."""

from __future__ import annotations

from typing import Any

from trans_novel.glossary.store import GlossaryStore
from trans_novel.pipeline.state import (
    NODE_FAILED_PERMANENT,
    NODE_FAILED_RETRYABLE,
    NODE_REPAIR,
    STATUS_DONE,
    RunStore,
)


def _audit_detail(detail: object) -> str:
    text = str(detail or "")
    for phrase in ("，请核对后重译", "，请重新翻译", "，请补全", "，请核对补全", "，请核对精简"):
        text = text.replace(phrase, "")
    return text


def _chapter_processing(state) -> dict[str, list[dict[str, Any]]]:
    preserved: list[dict[str, Any]] = []
    review_required: list[dict[str, Any]] = []
    for chapter in state.chapters:
        processing = chapter.processing
        if processing is None:
            continue
        item = {
            "chapter": chapter.index,
            "title": chapter.title,
            "reason": processing.reason,
        }
        if processing.action == "preserve":
            preserved.append(item)
        if processing.review_required:
            review_required.append(item)
    return {"preserved": preserved, "review_required": review_required}


def build_report(store: RunStore, glossary: GlossaryStore) -> dict[str, Any]:
    manifest = store.load_manifest()
    chapters_total = len(manifest["chapters"])
    lint_issues: list[dict] = []
    empty_targets: list[dict] = []
    back_matter: list[dict] = []
    chapters_done = 0
    for chapter_meta in manifest["chapters"]:
        ci = chapter_meta["index"]
        progress = store.load_progress(ci)
        if progress.status != STATUS_DONE:
            continue
        chapters_done += 1
        chapter = store.load_chapter(ci)
        for issue in progress.lint_issues:
            record = next(
                (
                    item
                    for item in progress.repair_ledger.values()
                    if item.index == issue.get("index")
                    and item.type == issue.get("type")
                    and item.detail == issue.get("detail")
                ),
                None,
            )
            if record is None or record.status != "accepted_after_exhaustion":
                lint_issues.append(issue)
        if progress.back_matter_mode:
            back_matter.append(
                {
                    "chapter": ci,
                    "title": chapter_meta.get("title", ""),
                    "mode": progress.back_matter_mode,
                }
            )
        for segment in chapter.text_segments:
            if not (segment.target and segment.target.strip()):
                empty_targets.append(
                    {"chapter": ci, "index": segment.index, "source": segment.source[:60]}
                )

    state = store.load_state()
    repair_node = state.nodes.get(NODE_REPAIR)
    raw_repair = ((repair_node.output or {}).get("repair") or {}) if repair_node else {}
    ledger = [
        record for progress in state.progress.values() for record in progress.repair_ledger.values()
    ]
    repair = {
        "detected": int(raw_repair.get("detected", len(ledger))),
        "resolved": int(raw_repair.get("resolved", sum(r.status == "resolved" for r in ledger))),
        "accepted_after_exhaustion": int(
            raw_repair.get(
                "accepted_after_exhaustion",
                sum(r.status == "accepted_after_exhaustion" for r in ledger),
            )
        ),
        "attempts": int(raw_repair.get("attempts", sum(r.attempts for r in ledger))),
        "deferred": bool(raw_repair.get("deferred", False)),
        "audit": [
            {**entry, "detail": _audit_detail(entry.get("detail"))}
            for entry in raw_repair.get("audit", [])
            if isinstance(entry, dict)
        ],
    }
    deterministic_issues = list(lint_issues)
    failed_nodes = [
        {
            "node": key,
            "status": node.status,
            "kind": node.failure.kind if node.failure else None,
            "message": (node.failure.message if node.failure else "")[:200],
            "attempts": node.attempts,
        }
        for key, node in sorted(state.nodes.items())
        if node.status in (NODE_FAILED_RETRYABLE, NODE_FAILED_PERMANENT)
    ]
    gstats = glossary.stats()
    conflicts = glossary.open_conflicts()
    low_conf = [
        {
            "source": term.source,
            "target": term.target,
            "type": term.type,
            "confidence": term.confidence,
            "status": term.status,
        }
        for term in glossary.low_confidence_terms()
    ]
    summary = {
        "chapters_total": chapters_total,
        "chapters_done": chapters_done,
        "terms": gstats["terms"],
        "open_conflicts": len(conflicts),
        "lint_issues": len(lint_issues),
        "deterministic_issues": len(deterministic_issues),
        "empty_targets": len(empty_targets),
        "failed_nodes": len(failed_nodes),
    }
    report = {
        "summary": summary,
        "open_conflicts": conflicts,
        "low_confidence_terms": low_conf,
        "lint_issues": lint_issues,
        "deterministic_issues": deterministic_issues,
        "repair": repair,
        "requires_user_action": False,
        "empty_targets": empty_targets,
        "failed_nodes": failed_nodes,
    }
    classified = any(chapter.processing is not None for chapter in state.chapters) or (
        state.initialized and state.identity.translation_policy_version >= 2
    )
    if classified:
        report["chapter_processing"] = _chapter_processing(state)
    else:
        summary["back_matter_chapters"] = len(back_matter)
        report["back_matter_chapters"] = back_matter
    return report
