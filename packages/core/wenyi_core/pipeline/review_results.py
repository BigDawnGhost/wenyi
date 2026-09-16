"""Build stable Review result projections without changing text or calling models."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..review.conflicts import build_conflict_groups
from ..review.run_store import ReviewRunStore
from ..review.session import ReviewSessionState


def net_changes(
    chapters,
    overrides: Mapping[tuple[int, int], str],
    patch_records: list[dict[str, Any]],
    active_patches: Mapping[tuple[int, int], dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse multiple rounds of shadow patches into one final suggestion per paragraph."""
    baseline = {
        (chapter.index, text_index): segment.target or ""
        for chapter in chapters
        for text_index, segment in enumerate(chapter.text_segments)
    }
    issue_keys_by_location: dict[tuple[int, int], set[str]] = {}
    for patch in patch_records:
        chapter = patch.get("chapter")
        index = patch.get("index")
        if (
            not isinstance(chapter, int)
            or isinstance(chapter, bool)
            or not isinstance(index, int)
            or isinstance(index, bool)
            or patch.get("status") == "rejected_cycle"
        ):
            continue
        keys = issue_keys_by_location.setdefault((chapter, index), set())
        keys.update(str(key) for key in patch.get("issue_keys", []) if isinstance(key, str) and key)

    changes: list[dict[str, Any]] = []
    for location, suggested_target in sorted(overrides.items()):
        if baseline.get(location) == suggested_target:
            continue
        active = active_patches.get(location) or {}
        changes.append(
            {
                "chapter": location[0],
                "index": location[1],
                "suggested_target": suggested_target,
                "issue_keys": sorted(issue_keys_by_location.get(location, set())),
                "review_result": str(active.get("status") or "provisional"),
            }
        )
    return changes


def project_issues(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove internal review fields to produce stable user-facing issues."""
    public: dict[str, dict[str, Any]] = {}
    for issue in issues:
        issue_key = issue.get("issue_key")
        chapter = issue.get("chapter")
        index = issue.get("index")
        if (
            not isinstance(issue_key, str)
            or not issue_key
            or not isinstance(chapter, int)
            or isinstance(chapter, bool)
            or not isinstance(index, int)
            or isinstance(index, bool)
        ):
            continue
        public[issue_key] = {
            "issue_key": issue_key,
            "chapter": chapter,
            "index": index,
            "type": str(issue.get("type") or ""),
            "detail": str(issue.get("detail") or ""),
            "suggestion": str(issue.get("suggestion") or ""),
        }
    return sorted(
        public.values(),
        key=lambda issue: (issue["chapter"], issue["index"], issue["issue_key"]),
    )


def conflict_records(
    groups: list[dict[str, Any]],
    arbitrations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Serialize conflicts and arbitration decisions into stable per-round records."""
    return [
        {
            "conflict_id": group["conflict_id"],
            "consistency_key": group["consistency_key"],
            "issue_ids": [issue["issue_id"] for issue in group["issues"]],
            "proposals": [
                {
                    "issue_id": issue["issue_id"],
                    "chapter": issue["chapter"],
                    "index": issue["index"],
                    "proposed_value": issue["consistency"]["proposed_value"],
                }
                for issue in group["issues"]
            ],
            "arbitration": arbitration,
        }
        for group, arbitration in zip(groups, arbitrations)
    ]


def unresolved_conflict_records(
    issues: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Rebuild conflicts from final unresolved issues so an empty last round cannot hide them."""
    groups = build_conflict_groups(issues)
    arbitrations: list[dict[str, Any]] = []
    for group in groups:
        issue_ids = [str(issue["issue_id"]) for issue in group["issues"]]
        annotations = [
            issue.get("arbitration")
            for issue in group["issues"]
            if isinstance(issue.get("arbitration"), dict)
        ]
        reasons = [
            str(annotation.get("reason", "")).strip()
            for annotation in annotations
            if str(annotation.get("reason", "")).strip()
        ]
        evidence_refs = sorted(
            {
                str(ref)
                for issue in group["issues"]
                for ref in issue.get("evidence_refs", [])
                if isinstance(ref, str) and ref
            }
        )
        arbitrations.append(
            {
                "conflict_id": group["conflict_id"],
                "consistency_key": group["consistency_key"],
                "issue_ids": issue_ids,
                "status": "unresolved",
                "recommended_value": "",
                "reason": reasons[-1]
                if reasons
                else "Final unresolved issues still contain conflicting proposals.",
                "supported_issue_ids": issue_ids,
                "rejected_issue_ids": [],
                "evidence_refs": evidence_refs,
            }
        )
    return conflict_records(groups, arbitrations)


def unresolved_fallback_count(issues: list[dict[str, Any]]) -> int:
    """Count distinct degraded review blocks still represented in unresolved issues."""
    return len(
        {
            str(issue.get("_chunk_id") or issue.get("issue_key") or issue.get("issue_id"))
            for issue in issues
            if issue.get("agent_fallback")
        }
    )


def write_completed(debug: ReviewRunStore, state: ReviewSessionState, loaded) -> dict[str, Any]:
    """Write final diagnostic projections and close the Review before usage is merged."""
    latest = state.latest
    if latest is None:  # pragma: no cover - max_review_rounds is at least one.
        raise RuntimeError("Review loop finished without a review round")

    unresolved = state.effective_issues(latest)
    final_conflicts = unresolved_conflict_records(unresolved)
    final_residual_conflicts = [
        record
        for record in final_conflicts
        if record.get("arbitration", {}).get("status") == "unresolved"
    ]
    final_fallback_agent_count = unresolved_fallback_count(unresolved)
    initial_issues, dismissed = debug.result_snapshots()
    debug.write_json("rounds/final/initial_issues.json", initial_issues)
    debug.write_json("rounds/final/dismissed_issues.json", dismissed)
    debug.write_json(
        "rounds/final/pre_arbitration_issues.json",
        latest.pre_arbitration_issues,
    )
    debug.write_json(
        "rounds/final/arbitration_superseded_issues.json",
        latest.arbitration_superseded,
    )
    debug.write_json(
        "rounds/final/residual_conflicts.json",
        [
            {
                "conflict_id": record["conflict_id"],
                "consistency_key": record["consistency_key"],
                "issue_ids": record["issue_ids"],
            }
            for record in final_residual_conflicts
        ],
    )
    debug.write_json("rounds/final/conflicts.json", final_conflicts)
    debug.write_json("rounds/final/patch-history.json", state.patch_records)
    debug.write_json(
        "rounds/final/not_rereported_patches.json",
        [patch for patch in state.patch_records if patch["status"] == "not_rereported"],
    )
    debug.write_json(
        "rounds/final/unresolved_issues.json",
        unresolved,
    )
    debug.write_json("rounds/final/fix_failures.json", state.fix_failures)
    debug.write_json("rounds/final/rounds.json", state.round_summaries)
    debug.write_json(
        "rounds/final/shadow_targets.json",
        [
            {
                "chapter": chapter,
                "index": index,
                "target": target,
            }
            for (chapter, index), target in sorted(state.target_overrides.items())
        ],
    )
    public_issues = project_issues(unresolved)
    changes = net_changes(
        loaded,
        state.target_overrides,
        state.patch_records,
        state.active_patches,
    )
    summary = {
        "initial_issue_count": len(initial_issues),
        "dismissed_issue_count": len(dismissed),
        "pre_arbitration_issue_count": len(latest.pre_arbitration_issues),
        "arbitration_superseded_count": len(latest.arbitration_superseded),
        "issue_count": len(public_issues),
        "conflict_count": len(final_conflicts),
        "unresolved_conflict_count": len(final_residual_conflicts),
        "fallback_agent_count": final_fallback_agent_count,
        "review_round_count": len(state.round_summaries),
        "fix_round_count": state.fix_rounds,
        "patch_count": len(state.patch_records),
        "change_count": len(changes),
        "not_rereported_patch_count": sum(
            patch["status"] == "not_rereported" for patch in state.patch_records
        ),
        "shadow_override_count": len(state.target_overrides),
        "blocked_issue_count": len(state.blocked_issues),
        "clean_streak": state.clean_streak,
    }
    debug.write_json("rounds/final/summary.json", summary)
    result = debug.finish(
        status="completed",
        termination=state.termination,
        summary=summary,
        issues=public_issues,
        changes=changes,
    )
    return result


def write_partial(
    debug: ReviewRunStore, state: ReviewSessionState, loaded, error: Exception, resumable: bool
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Save partial diagnostics for an unfinished Review.

    Provider/quota stops record ``interrupted``; local/protocol errors record ``failed``
    for logs. Both statuses remain eligible for ``find_resumable`` when fingerprints match.
    """
    initial_issues, dismissed = debug.result_snapshots()
    partial_issues = state.effective_issues(state.latest) if state.latest is not None else []
    public_issues = project_issues(partial_issues)
    partial_changes = net_changes(
        loaded,
        state.target_overrides,
        state.patch_records,
        state.active_patches,
    )
    summary = {
        "issue_count": len(public_issues),
        "change_count": len(partial_changes),
        "conflict_count": (len(state.latest.conflict_groups) if state.latest is not None else 0),
        "fallback_agent_count": (
            state.latest.fallback_agent_count if state.latest is not None else 0
        ),
    }
    error_payload = {"type": type(error).__name__, "message": str(error)}
    debug.write_json("rounds/final/initial_issues.json", initial_issues)
    debug.write_json("rounds/final/dismissed_issues.json", dismissed)
    debug.write_json(
        "rounds/final/partial_issues.json",
        partial_issues,
    )
    debug.write_json("rounds/final/partial_patches.json", state.patch_records)
    debug.write_json("rounds/final/fix_failures.json", state.fix_failures)
    if resumable:
        debug.mark_interrupted(
            error=error_payload,
            summary=summary,
            issues=public_issues,
            changes=partial_changes,
        )
    else:
        debug.finish(
            status="failed",
            termination="error",
            summary=summary,
            issues=public_issues,
            changes=partial_changes,
            error=error_payload,
        )
    return public_issues, partial_changes
