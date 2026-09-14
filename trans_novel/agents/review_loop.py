"""Prepare leaf-block review prompts and validate confirmed or additional issues."""

from __future__ import annotations

import json
from typing import Any

from ..config import Config
from ..i18n.prompts import render
from ..llm.base import LLMClient
from ..review.contracts import EvidenceQueries, ReviewTrace
from ..review.models import (
    CONSISTENCY_KINDS,
    ReviewLoopOutcome,
    clean_text,
    review_candidate_id,
)
from . import prompts
from .review_actions import ReviewActionLoop, ReviewLoopProtocolError, validate_evidence_refs

_ISSUE_TYPES = {"missing", "added", "mistranslation", "terminology", "pronoun"}


class ReviewAgentLoop:
    """Verify a successfully reviewed leaf block and allow additional issues within that block."""

    def __init__(
        self,
        client: LLMClient,
        config: Config,
        evidence: EvidenceQueries,
        trace: ReviewTrace,
        *,
        operation: str = "review.verify",
    ):
        self.operation = operation
        self.config = config
        self.evidence = evidence
        self.trace = trace
        self._loop = ReviewActionLoop(client, config, evidence, trace)

    @staticmethod
    def _consistency(value: Any) -> dict[str, str]:
        """Normalize cross-block consistency claims; return an empty dictionary for ordinary
        issues.
        """
        if value is None or value == {}:
            return {}
        if not isinstance(value, dict):
            raise ReviewLoopProtocolError("invalid_consistency")
        kind = clean_text(value.get("kind"))
        subject = clean_text(value.get("subject_source"))
        proposed = clean_text(value.get("proposed_value"))
        if not kind and not subject and not proposed:
            return {}
        if kind not in CONSISTENCY_KINDS or not subject or not proposed:
            raise ReviewLoopProtocolError("invalid_consistency")
        return {
            "kind": kind,
            "subject_source": subject,
            "proposed_value": proposed,
        }

    def review_chunk(
        self,
        *,
        chapter: int,
        chunk_base: int,
        sources: list[str],
        targets: list[str],
        initial_issues: list[dict[str, Any]],
        review_round: int | None = None,
    ) -> ReviewLoopOutcome:
        """Run bounded block-level evidence review; preserve all initial candidates on failure."""
        candidates: list[dict[str, Any]] = []
        for ordinal, issue in enumerate(initial_issues):
            candidate = dict(issue)
            candidate["candidate_id"] = review_candidate_id(
                chapter,
                chunk_base,
                ordinal,
                review_round,
            )
            candidates.append(candidate)

        round_prefix = f"r{review_round}-" if review_round is not None else ""
        agent_id = f"{round_prefix}chunk-ch{chapter}-base{chunk_base}-n{len(sources)}"
        self.trace.log_event(
            "review_agent_started",
            agent_id=agent_id,
            chapter=chapter,
            chunk_base=chunk_base,
            segment_count=len(sources),
            candidate_count=len(candidates),
        )
        system = render(
            "review_agent_system",
            src=self.config.source_lang,
            tgt=self.config.target_lang,
            max_evidence_rounds=(self.config.pipeline.review_agent_max_evidence_rounds),
        )
        current_refs = {
            local_index: ref.ref
            for local_index in range(len(sources))
            if (ref := self.evidence.segment_ref(chapter, chunk_base + local_index)) is not None
        }
        user = render(
            "review_agent_user",
            src=self.config.source_lang,
            tgt=self.config.target_lang,
            chapter=chapter,
            last_index=max(0, len(sources) - 1),
            pairs=prompts.numbered_pairs_with_refs(
                sources,
                targets,
                [current_refs.get(index, "") for index in range(len(sources))],
            ),
            segment_refs_json=json.dumps(
                [{"index": index, "ref": ref} for index, ref in sorted(current_refs.items())],
                ensure_ascii=False,
                indent=2,
            ),
            candidates_json=json.dumps(candidates, ensure_ascii=False, indent=2),
        )
        allowed_refs = set(current_refs.values())

        def issue_refs(index: int, value: Any, valid_refs: set[str]) -> list[str]:
            """Include the current segment reference with explicit citations so suggestions
            remain traceable.
            """
            refs = validate_evidence_refs(value, valid_refs)
            current = current_refs.get(index)
            return list(dict.fromkeys([*([current] if current else []), *refs]))

        def validate_final(
            data: dict[str, Any], valid_refs: set[str]
        ) -> dict[str, list[dict[str, Any]]]:
            decisions = data.get("decisions")
            new_issues = data.get("new_issues", [])
            if not isinstance(decisions, list) or not isinstance(new_issues, list):
                raise ReviewLoopProtocolError("invalid_final_issue_lists")
            expected = {candidate["candidate_id"] for candidate in candidates}
            by_id: dict[str, dict[str, Any]] = {}
            for decision in decisions:
                if not isinstance(decision, dict):
                    raise ReviewLoopProtocolError("decision_not_object")
                candidate_id = clean_text(decision.get("candidate_id"))
                if not candidate_id or candidate_id in by_id or candidate_id not in expected:
                    raise ReviewLoopProtocolError("invalid_candidate_decision")
                by_id[candidate_id] = decision
            if set(by_id) != expected:
                raise ReviewLoopProtocolError("candidate_decisions_incomplete")

            kept: list[dict[str, Any]] = []
            dismissed: list[dict[str, Any]] = []
            candidates_by_id = {candidate["candidate_id"]: candidate for candidate in candidates}
            for candidate_id in sorted(expected):
                decision = by_id[candidate_id]
                candidate = candidates_by_id[candidate_id]
                verdict = decision.get("verdict")
                if verdict == "dismissed":
                    reason = clean_text(decision.get("reason"))
                    if not reason:
                        raise ReviewLoopProtocolError("dismissal_without_reason")
                    dismissed.append(
                        {
                            "candidate_id": candidate_id,
                            "index": candidate["index"],
                            "type": candidate["type"],
                            "detail": candidate["detail"],
                            "suggestion": candidate["suggestion"],
                            "reason": reason,
                            "evidence_refs": issue_refs(
                                candidate["index"],
                                decision.get("evidence_refs"),
                                valid_refs,
                            ),
                        }
                    )
                    continue
                if verdict != "confirmed":
                    raise ReviewLoopProtocolError("invalid_candidate_verdict")
                detail = clean_text(decision.get("detail")) or clean_text(candidate.get("detail"))
                suggestion = clean_text(decision.get("suggestion")) or clean_text(
                    candidate.get("suggestion")
                )
                if not detail or not suggestion:
                    raise ReviewLoopProtocolError("confirmed_issue_missing_text")
                kept.append(
                    {
                        "index": candidate["index"],
                        "type": candidate["type"],
                        "detail": detail,
                        "suggestion": suggestion,
                        "origin": "initial",
                        "candidate_id": candidate_id,
                        "consistency": self._consistency(decision.get("consistency")),
                        "evidence_refs": issue_refs(
                            candidate["index"],
                            decision.get("evidence_refs"),
                            valid_refs,
                        ),
                    }
                )

            limit = min(50, max(4, len(sources) * 2))
            if len(new_issues) > limit:
                raise ReviewLoopProtocolError("too_many_new_issues")
            for issue in new_issues:
                if not isinstance(issue, dict):
                    raise ReviewLoopProtocolError("new_issue_not_object")
                index = issue.get("index")
                if (
                    isinstance(index, bool)
                    or not isinstance(index, int)
                    or not 0 <= index < len(sources)
                ):
                    raise ReviewLoopProtocolError("new_issue_outside_chunk")
                issue_type = issue.get("type")
                detail = clean_text(issue.get("detail"))
                suggestion = clean_text(issue.get("suggestion"))
                if issue_type not in _ISSUE_TYPES or not detail or not suggestion:
                    raise ReviewLoopProtocolError("invalid_new_issue")
                kept.append(
                    {
                        "index": index,
                        "type": issue_type,
                        "detail": detail,
                        "suggestion": suggestion,
                        "origin": "agent",
                        "consistency": self._consistency(issue.get("consistency")),
                        "evidence_refs": issue_refs(
                            index,
                            issue.get("evidence_refs"),
                            valid_refs,
                        ),
                    }
                )
            return {"issues": kept, "dismissed": dismissed}

        result, reason = self._loop.run(
            agent_id=agent_id,
            system=system,
            user=user,
            stage=self.operation,
            allowed_refs=allowed_refs,
            validate_final=validate_final,
        )
        if result is None:
            fallback = [
                {
                    **dict(issue),
                    "origin": "initial",
                    "agent_fallback": True,
                    "fallback_reason": reason,
                    "evidence_refs": (
                        [current_refs[int(issue["index"])]]
                        if int(issue["index"]) in current_refs
                        else []
                    ),
                }
                for issue in initial_issues
            ]
            return ReviewLoopOutcome(fallback, [], fallback_reason=reason)
        return ReviewLoopOutcome(result["issues"], result["dismissed"])
