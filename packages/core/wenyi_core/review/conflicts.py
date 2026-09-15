"""Normalize review issues and apply conflict decisions without model or storage calls."""

from __future__ import annotations

from typing import Any

from .contracts import EvidenceQueries
from .models import CONSISTENCY_KINDS, clean_text, normalize_value, review_issue_key


def normalize_review_issues(
    issues: list[dict[str, Any]],
    evidence: EvidenceQueries,
) -> list[dict[str, Any]]:
    """Normalize deterministically and assign round-local IDs and stable cross-round issue
    keys.
    """
    prepared: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for issue in sorted(
        issues,
        key=lambda item: (
            item.get("chapter", -1),
            item.get("index", -1),
            item.get("_chunk_id", ""),
            item.get("type", ""),
        ),
    ):
        item = dict(issue)
        consistency = item.get("consistency")
        if isinstance(consistency, dict):
            kind = clean_text(consistency.get("kind"))
            subject = clean_text(consistency.get("subject_source"))
            proposed = clean_text(consistency.get("proposed_value"))
            if kind in CONSISTENCY_KINDS and subject and proposed:
                term, ambiguous = evidence.canonical_term(subject)
                if ambiguous:
                    item["consistency"] = {
                        "kind": kind,
                        "subject_source": subject,
                        "canonical_source": "",
                        "proposed_value": proposed,
                        "ambiguous_sources": ambiguous,
                        "auto_arbitration": False,
                    }
                else:
                    canonical = term.source if term is not None else subject
                    canonical_key = (
                        f"glossary:{canonical}" if term is not None else normalize_value(canonical)
                    )
                    item["consistency"] = {
                        "kind": kind,
                        "subject_source": subject,
                        "canonical_source": canonical,
                        "key": f"{kind}:{canonical_key}",
                        "proposed_value": proposed,
                    }
            else:
                item["consistency"] = {}
        issue_key = review_issue_key(item)
        if issue_key in seen_keys:
            continue
        seen_keys.add(issue_key)
        item["issue_key"] = issue_key
        item["issue_id"] = f"review-{len(prepared) + 1:05d}"
        prepared.append(item)
    return prepared


def build_conflict_groups(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Find mutually exclusive values proposed for one consistency subject across review
    blocks.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for issue in issues:
        consistency = issue.get("consistency")
        if not isinstance(consistency, dict):
            continue
        key = clean_text(consistency.get("key"))
        proposed = clean_text(consistency.get("proposed_value"))
        if key and proposed:
            grouped.setdefault(key, []).append(issue)

    conflicts: list[dict[str, Any]] = []
    for key, group in grouped.items():
        chunks = {issue.get("_chunk_id") for issue in group}
        values = {
            normalize_value(clean_text(issue.get("consistency", {}).get("proposed_value")))
            for issue in group
        }
        values.discard("")
        if len(chunks) < 2 or len(values) < 2:
            continue
        conflicts.append(
            {
                "consistency_key": key,
                "issues": group,
                "first_position": min(
                    (issue.get("chapter", -1), issue.get("index", -1)) for issue in group
                ),
            }
        )
    conflicts.sort(key=lambda item: (item["first_position"], item["consistency_key"]))
    for ordinal, conflict in enumerate(conflicts, 1):
        conflict["conflict_id"] = f"review-conflict-{ordinal:04d}"
    return conflicts


def apply_review_arbitrations(
    issues: list[dict[str, Any]],
    arbitrations: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply final arbitration to the recommendation view without modifying text or glossary.
    For suggested conflicts, retain every confirmed issue: locations whose original
    proposals lost still need correction. Rewrite their suggestions to the chosen value and
    retain pre-arbitration versions for round auditing. For unresolved conflicts, keep all
    issues and mark them unresolved.
    """
    by_id = {
        str(issue["issue_id"]): dict(issue)
        for issue in issues
        if isinstance(issue.get("issue_id"), str)
    }
    superseded_rows: list[dict[str, Any]] = []
    for arbitration in arbitrations:
        conflict_id = clean_text(arbitration.get("conflict_id"))
        status = arbitration.get("status")
        annotation = {
            "conflict_id": conflict_id,
            "status": status,
            "recommended_value": clean_text(arbitration.get("recommended_value")),
            "reason": clean_text(arbitration.get("reason")),
        }
        if status == "suggested":
            for issue_id in arbitration.get("rejected_issue_ids", []):
                issue = by_id.get(str(issue_id))
                if issue is not None:
                    recommended = annotation["recommended_value"]
                    consistency = issue.get("consistency")
                    issue_annotation = {**annotation, "action": "rewritten"}
                    superseded_rows.append({**issue, "arbitration": issue_annotation})
                    previous_detail = clean_text(issue.get("detail"))
                    previous_suggestion = clean_text(issue.get("suggestion"))
                    issue["pre_arbitration_detail"] = previous_detail
                    issue["pre_arbitration_suggestion"] = previous_suggestion
                    issue["detail"] = (
                        f"Final arbitration requires the expression here to use “{recommended}” consistently."
                    )
                    issue["suggestion"] = (
                        f"Use “{recommended}” consistently for this expression as determined by final arbitration."
                    )
                    if isinstance(consistency, dict):
                        issue["consistency"] = {
                            **consistency,
                            "proposed_value": recommended,
                        }
                    issue["arbitration"] = issue_annotation
            for issue_id in arbitration.get("supported_issue_ids", []):
                if str(issue_id) in by_id:
                    by_id[str(issue_id)]["arbitration"] = annotation
        elif status == "unresolved":
            for issue_id in arbitration.get("issue_ids", []):
                if str(issue_id) in by_id:
                    by_id[str(issue_id)]["arbitration"] = annotation

    order = {
        str(issue["issue_id"]): position
        for position, issue in enumerate(issues)
        if isinstance(issue.get("issue_id"), str)
    }
    final = sorted(by_id.values(), key=lambda issue: order.get(str(issue["issue_id"]), -1))
    superseded_rows.sort(key=lambda issue: order.get(str(issue["issue_id"]), -1))
    return final, superseded_rows
