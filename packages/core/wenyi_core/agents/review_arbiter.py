"""Prepare cross-block arbitration prompts and validate read-only recommendations."""

from __future__ import annotations

import json
from typing import Any

from ..config import Config
from ..i18n.prompts import render
from ..llm.base import LLMClient
from ..review.contracts import EvidenceQueries, ReviewTrace
from ..review.models import clean_text, normalize_value
from .review_actions import ReviewActionLoop, ReviewLoopProtocolError, validate_evidence_refs

_MAX_ARBITRATION_PROPOSALS = 32
_MAX_ARBITRATION_PAYLOAD_BYTES = 96_000
_ARBITRATION_SAMPLE_TEXT_LIMIT = 1500


class ReviewConflictArbiter:
    """Produce read-only recommendations for conflicting consistency proposals after all blocks
    finish.
    """

    def __init__(
        self,
        client: LLMClient,
        config: Config,
        evidence: EvidenceQueries,
        trace: ReviewTrace,
    ):
        self.config = config
        self.evidence = evidence
        self.trace = trace
        self._loop = ReviewActionLoop(client, config, evidence, trace)

    def arbitrate(self, conflict: dict[str, Any]) -> dict[str, Any]:
        """Arbitrate one conflict; retain all issues and mark unresolved on failure."""
        conflict_id = str(conflict["conflict_id"])
        issue_ids = [str(issue["issue_id"]) for issue in conflict["issues"]]

        def unresolved(reason: str, refs: set[str] | None = None) -> dict[str, Any]:
            """Build a conservative result that preserves issues and records why arbitration
            was incomplete.
            """
            self.trace.log_event(
                "review_arbitration_unresolved",
                conflict_id=conflict_id,
                issue_count=len(issue_ids),
                reason=reason,
            )
            return {
                "conflict_id": conflict_id,
                "consistency_key": conflict["consistency_key"],
                "issue_ids": issue_ids,
                "status": "unresolved",
                "recommended_value": "",
                "reason": reason,
                "supported_issue_ids": issue_ids,
                "rejected_issue_ids": [],
                "evidence_refs": sorted(refs or set()),
            }

        proposal_groups: dict[str, list[dict[str, Any]]] = {}
        for issue in conflict["issues"]:
            proposed = clean_text(issue["consistency"]["proposed_value"])
            proposal_groups.setdefault(normalize_value(proposed), []).append(issue)
        if len(proposal_groups) > _MAX_ARBITRATION_PROPOSALS:
            return unresolved(
                f"Too many conflicting values ({len(proposal_groups)}) for selective arbitration."
            )

        sampled_refs: set[str] = set()
        proposal_rows: list[dict[str, Any]] = []
        for grouped_issues in proposal_groups.values():
            sample_positions = list(
                dict.fromkeys(
                    (
                        0,
                        (len(grouped_issues) - 1) // 2,
                        len(grouped_issues) - 1,
                    )
                )
            )
            samples: list[dict[str, Any]] = []
            for sample_position in sample_positions:
                issue = grouped_issues[sample_position]
                segment = self.evidence.segment_ref(
                    int(issue["chapter"]),
                    int(issue["index"]),
                )
                if segment is not None:
                    sampled_refs.add(segment.ref)
                samples.append(
                    {
                        "issue_id": issue["issue_id"],
                        "chapter": issue["chapter"],
                        "index": issue["index"],
                        "type": issue["type"],
                        "detail": clean_text(issue["detail"])[:_ARBITRATION_SAMPLE_TEXT_LIMIT],
                        "suggestion": clean_text(issue["suggestion"])[
                            :_ARBITRATION_SAMPLE_TEXT_LIMIT
                        ],
                        "segment_ref": segment.ref if segment is not None else "",
                        "source": (
                            segment.source[:_ARBITRATION_SAMPLE_TEXT_LIMIT]
                            if segment is not None
                            else ""
                        ),
                        "target": (
                            segment.target[:_ARBITRATION_SAMPLE_TEXT_LIMIT]
                            if segment is not None
                            else ""
                        ),
                    }
                )
            proposal_rows.append(
                {
                    "proposed_value": grouped_issues[0]["consistency"]["proposed_value"],
                    "issue_count": len(grouped_issues),
                    "samples": samples,
                }
            )

        compact = {
            "conflict_id": conflict_id,
            "consistency_key": conflict["consistency_key"],
            "issue_count": len(issue_ids),
            "proposals": proposal_rows,
        }
        compact_json = json.dumps(compact, ensure_ascii=False, indent=2)
        if len(compact_json.encode("utf-8")) > _MAX_ARBITRATION_PAYLOAD_BYTES:
            return unresolved(
                "Selective arbitration samples still exceed the input size limit.", sampled_refs
            )

        system = render(
            "review_arbiter_system",
            src=self.config.source_lang,
            tgt=self.config.target_lang,
            max_evidence_rounds=(self.config.pipeline.review_agent_max_evidence_rounds),
        )
        user = render(
            "review_arbiter_user",
            src=self.config.source_lang,
            tgt=self.config.target_lang,
            conflict_json=compact_json,
        )
        # Preauthorize only sample refs whose text appears in the arbiter prompt. Evidence previously
        # obtained by a block agent but not shown here must be requested again by the arbiter.
        allowed_refs = set(sampled_refs)

        def validate_final(data: dict[str, Any], valid_refs: set[str]) -> dict[str, Any]:
            if data.get("conflict_id") != conflict_id:
                raise ReviewLoopProtocolError("conflict_id_mismatch")
            if "supported_issue_ids" in data or "rejected_issue_ids" in data:
                raise ReviewLoopProtocolError("arbitration_issue_ids_must_be_omitted")
            status = data.get("status")
            if status not in {"suggested", "unresolved"}:
                raise ReviewLoopProtocolError("invalid_arbitration_status")
            recommended = clean_text(data.get("recommended_value"))
            reason = clean_text(data.get("reason"))
            if status == "suggested" and not recommended:
                raise ReviewLoopProtocolError("suggestion_without_value")
            if not reason:
                raise ReviewLoopProtocolError("arbitration_without_reason")
            if status == "suggested":
                normalized_recommendation = normalize_value(recommended)
                if normalized_recommendation not in proposal_groups:
                    raise ReviewLoopProtocolError("recommended_value_not_proposed")
                recommended = clean_text(
                    proposal_groups[normalized_recommendation][0]["consistency"]["proposed_value"]
                )
                supported = [
                    str(issue["issue_id"])
                    for issue in conflict["issues"]
                    if normalize_value(clean_text(issue["consistency"]["proposed_value"]))
                    == normalized_recommendation
                ]
                supported_set = set(supported)
                rejected = [issue_id for issue_id in issue_ids if issue_id not in supported_set]
            else:
                supported = issue_ids
                rejected = []
            refs = validate_evidence_refs(data.get("evidence_refs"), valid_refs)
            return {
                "conflict_id": conflict_id,
                "consistency_key": conflict["consistency_key"],
                "issue_ids": issue_ids,
                "status": status,
                "recommended_value": recommended,
                "reason": reason,
                "supported_issue_ids": supported,
                "rejected_issue_ids": rejected,
                "evidence_refs": refs,
            }

        result, reason = self._loop.run(
            agent_id=f"arbiter-{conflict_id}",
            system=system,
            user=user,
            stage="review.arbitrate",
            allowed_refs=allowed_refs,
            validate_final=validate_final,
        )
        if result is not None:
            return result
        return unresolved(f"Arbitration agent did not complete: {reason}", allowed_refs)
