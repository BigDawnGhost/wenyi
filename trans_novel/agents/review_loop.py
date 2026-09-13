"""Bounded review agent loop and cross-block conflict arbitration."""

from __future__ import annotations

import json
from typing import Any, Callable

from ..config import Config
from ..i18n.prompts import render
from ..llm.base import LLMClient
from ..llm.json_parser import parse_json_result
from ..review.contracts import EvidenceQueries, EvidenceTools, ReviewTrace
from ..review.models import (
    CONSISTENCY_KINDS,
    ReviewLoopOutcome,
    clean_text,
    normalize_value,
    review_candidate_id,
)
from . import prompts

_ISSUE_TYPES = {"missing", "added", "mistranslation", "terminology", "pronoun"}
_MAX_ARBITRATION_PROPOSALS = 32
_MAX_ARBITRATION_PAYLOAD_BYTES = 96_000
_ARBITRATION_SAMPLE_TEXT_LIMIT = 1500


class ReviewLoopProtocolError(ValueError):
    """The agent loop returned content that violates the action protocol."""


class _ActionLoop:
    """Implement a request-evidence/final loop using the ordinary messages interface."""

    def __init__(
        self,
        client: LLMClient,
        config: Config,
        evidence: EvidenceTools,
        trace: ReviewTrace,
    ):
        self.client = client
        self.config = config
        self.evidence = evidence
        self.trace = trace

    def run(
        self,
        *,
        agent_id: str,
        system: str,
        user: str,
        stage: str,
        allowed_refs: set[str],
        validate_final: Callable[[dict[str, Any], set[str]], Any],
    ) -> tuple[Any | None, str]:
        """Run up to N evidence rounds plus a final call; return a failure reason instead of
        raising.
        """
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        max_rounds = self.config.pipeline.review_agent_max_evidence_rounds
        if max_rounds == 0:
            messages[-1]["content"] += (
                "\nEvidence requests are disabled for this call; return final immediately."
            )
        trace: dict[str, Any] = {
            "agent_id": agent_id,
            "stage": stage,
            "status": "running",
            "turns": [],
        }
        from ..llm.routing import inference_snapshot

        trace["inference"] = inference_snapshot(self.config.llm, (stage,))
        # Resume: load an existing trace before writing, or the trace would overwrite itself.
        existing = self.trace.load(agent_id)
        if existing is not None and existing.get("inference") != trace["inference"]:
            self.trace.log_event(
                "review_agent_cache_invalidated",
                agent_id=agent_id,
                operation=stage,
                reason="request_changed",
            )
            existing = None
        resume_turns: list[dict[str, Any]] = []
        if existing is not None:
            existing_status = existing.get("status")
            if existing_status == "finished" and isinstance(existing.get("result"), dict):
                self.trace.log_event(
                    "review_agent_finished",
                    agent_id=agent_id,
                    stage=stage,
                    turns=len(existing.get("turns", [])),
                    resumed=True,
                )
                return existing["result"], ""
            if existing_status == "fallback":
                self.trace.log_event(
                    "review_agent_fallback",
                    agent_id=agent_id,
                    stage=stage,
                    reason=str(existing.get("fallback_reason", "")),
                    resumed=True,
                )
                return None, str(existing.get("fallback_reason", ""))
            if existing_status == "running":
                resume_turns = [
                    dict(turn) for turn in existing.get("turns", []) if isinstance(turn, dict)
                ]
        trace["turns"] = resume_turns
        evidence_rounds = 0
        seen_request_ids: set[str] = set()
        seen_requests: set[tuple[str, str]] = set()
        start_turn = 1
        if resume_turns:
            # Replay completed evidence rounds to restore message history and deduplication state.
            # Message order, evidence JSON and round instructions must match the original run byte for byte
            # so an in-flight call can resume seamlessly.
            first_messages = resume_turns[0].get("messages")
            if isinstance(first_messages, list):
                messages = [dict(message) for message in first_messages]
            self.trace.log_event(
                "review_agent_resumed",
                agent_id=agent_id,
                stage=stage,
                turns=len(resume_turns),
            )
            for cached in resume_turns:
                cached_results = cached.get("evidence_results")
                cached_raw = cached.get("raw_response")
                if not isinstance(cached_results, list) or not isinstance(cached_raw, str):
                    continue
                messages.append({"role": "assistant", "content": cached_raw})
                evidence_message = "[Evidence tool results (JSON)]\n" + json.dumps(
                    cached_results, ensure_ascii=False, indent=2
                )
                evidence_rounds += 1
                if evidence_rounds >= max_rounds:
                    evidence_message += "\nEvidence rounds are exhausted. The next response must use action=final; do not request more evidence."
                messages.append({"role": "user", "content": evidence_message})
                allowed_refs.update(self.evidence.evidence_refs(cached_results))
                cached_parsed = cached.get("parsed")
                if isinstance(cached_parsed, dict):
                    for request in cached_parsed.get("requests", []):
                        if not isinstance(request, dict):
                            continue
                        request_id = clean_text(request.get("request_id"))
                        if request_id:
                            seen_request_ids.add(request_id)
                        tool = clean_text(request.get("tool"))
                        arguments = request.get("arguments")
                        if tool and isinstance(arguments, dict):
                            seen_requests.add(
                                (tool, json.dumps(arguments, ensure_ascii=False, sort_keys=True))
                            )
            # Resume at the first unfinished turn. Re-enter the final cached turn if it lacks evidence results
            # because a request, evidence operation or parsed final response was not yet persisted.
            start_turn = len(resume_turns)
            if "evidence_results" in resume_turns[-1]:
                start_turn += 1
        self.trace.save(agent_id, trace)
        cached_by_turn = {
            turn["turn"]: turn for turn in resume_turns if isinstance(turn.get("turn"), int)
        }

        try:
            for turn_number in range(start_turn, max(start_turn, max_rounds + 1) + 1):
                sent_messages = [dict(message) for message in messages]
                cached_turn = cached_by_turn.get(turn_number)
                if cached_turn is not None:
                    turn = cached_turn
                    turn["messages"] = sent_messages
                else:
                    turn: dict[str, Any] = {
                        "turn": turn_number,
                        "messages": sent_messages,
                        "status": "requesting",
                    }
                    trace["turns"].append(turn)
                self.trace.save(agent_id, trace)
                if cached_turn is not None and isinstance(cached_turn.get("raw_response"), str):
                    raw = cached_turn["raw_response"]
                    turn["status"] = "responded"
                    turn["raw_response"] = raw
                else:
                    try:
                        raw = self.client.complete(
                            sent_messages,
                            json_mode=True,
                            operation=stage,
                        )
                    except Exception as error:
                        turn["status"] = "failed"
                        turn["error"] = {
                            "type": type(error).__name__,
                            "message": str(error),
                        }
                        self.trace.save(agent_id, trace)
                        raise
                    turn["status"] = "responded"
                    turn["raw_response"] = raw
                self.trace.save(agent_id, trace)

                # Reuse parsed only when raw and parsed come from the same cached response.
                # Orphaned parsed data from a damaged or edited trace must not hide a fresh response.
                if (
                    cached_turn is not None
                    and isinstance(cached_turn.get("raw_response"), str)
                    and isinstance(cached_turn.get("parsed"), dict)
                ):
                    data = cached_turn["parsed"]
                    turn["parsed"] = data
                    turn["json_repaired"] = bool(cached_turn.get("json_repaired"))
                else:
                    try:
                        parsed = parse_json_result(raw)
                    except ValueError as error:
                        raise ReviewLoopProtocolError("malformed_json") from error
                    data = parsed.value
                    turn["parsed"] = data
                    turn["json_repaired"] = parsed.repaired
                self.trace.save(agent_id, trace)
                if not isinstance(data, dict):
                    raise ReviewLoopProtocolError("response_not_object")
                if not data or list(data)[-1] != "complete":
                    raise ReviewLoopProtocolError("completion_marker_not_last")

                action = data.get("action")
                if action == "final":
                    if data.get("complete") is not True:
                        raise ReviewLoopProtocolError("final_not_complete")
                    result = validate_final(data, allowed_refs)
                    trace["status"] = "finished"
                    trace["result"] = result
                    self.trace.save(agent_id, trace)
                    self.trace.log_event(
                        "review_agent_finished",
                        agent_id=agent_id,
                        stage=stage,
                        turns=turn_number,
                        evidence_rounds=evidence_rounds,
                    )
                    return result, ""

                if action != "request_evidence":
                    raise ReviewLoopProtocolError("unknown_action")
                if data.get("complete") is not False:
                    raise ReviewLoopProtocolError("evidence_action_marked_complete")
                if evidence_rounds >= max_rounds:
                    raise ReviewLoopProtocolError("evidence_round_limit")
                requests = data.get("requests")
                if not isinstance(requests, list) or not 1 <= len(requests) <= 4:
                    raise ReviewLoopProtocolError("invalid_evidence_requests")
                current_ids: set[str] = set()
                current_requests: set[tuple[str, str]] = set()
                for request in requests:
                    if not isinstance(request, dict):
                        raise ReviewLoopProtocolError("evidence_request_not_object")
                    request_id = clean_text(request.get("request_id"))
                    if (
                        not request_id
                        or request_id in seen_request_ids
                        or request_id in current_ids
                    ):
                        raise ReviewLoopProtocolError("duplicate_evidence_request_id")
                    tool = clean_text(request.get("tool"))
                    arguments = request.get("arguments")
                    if not tool or not isinstance(arguments, dict):
                        raise ReviewLoopProtocolError("invalid_evidence_request")
                    signature = (
                        tool,
                        json.dumps(arguments, ensure_ascii=False, sort_keys=True),
                    )
                    if signature in seen_requests or signature in current_requests:
                        raise ReviewLoopProtocolError("duplicate_evidence_request")
                    current_ids.add(request_id)
                    current_requests.add(signature)
                seen_request_ids.update(current_ids)
                seen_requests.update(current_requests)

                results: list[dict[str, Any]] = []
                batch_size = 2
                for request in requests:
                    result = self.evidence.execute(request)
                    encoded_size = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
                    if batch_size + encoded_size > 128_000:
                        result = {
                            "request_id": request["request_id"],
                            "tool": request["tool"],
                            "ok": False,
                            "error": "evidence_batch_too_large",
                            "hint": "Reduce the requests per round or the context range of each request.",
                        }
                        encoded_size = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
                    results.append(result)
                    batch_size += encoded_size + 1
                evidence_rounds += 1
                allowed_refs.update(self.evidence.evidence_refs(results))
                turn["evidence_results"] = results
                self.trace.save(agent_id, trace)
                self.trace.log_event(
                    "review_evidence_supplied",
                    agent_id=agent_id,
                    stage=stage,
                    round=evidence_rounds,
                    requests=[
                        {
                            "request_id": request.get("request_id"),
                            "tool": request.get("tool"),
                            "arguments": request.get("arguments"),
                        }
                        for request in requests
                    ],
                    refs=sorted(self.evidence.evidence_refs(results)),
                )
                messages.append({"role": "assistant", "content": raw})
                evidence_message = "[Evidence tool results (JSON)]\n" + json.dumps(
                    results, ensure_ascii=False, indent=2
                )
                if evidence_rounds >= max_rounds:
                    evidence_message += "\nEvidence rounds are exhausted. The next response must use action=final; do not request more evidence."
                messages.append({"role": "user", "content": evidence_message})
        except Exception as error:  # noqa: BLE001 - Loop failures fall back to the initial review by contract.
            reason = (
                str(error)
                if isinstance(error, ReviewLoopProtocolError)
                else f"{type(error).__name__}: {error}"
            )
            trace["status"] = "fallback"
            trace["fallback_reason"] = reason
            self.trace.save(agent_id, trace)
            self.trace.log_event(
                "review_agent_fallback",
                agent_id=agent_id,
                stage=stage,
                reason=reason,
            )
            return None, reason
        trace["status"] = "fallback"
        trace["fallback_reason"] = "loop_ended_without_final"
        self.trace.save(agent_id, trace)
        return None, "loop_ended_without_final"


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
        self._loop = _ActionLoop(client, config, evidence, trace)

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

    @staticmethod
    def _refs(value: Any, allowed_refs: set[str]) -> list[str]:
        """Verify that final output references only evidence actually obtained by this loop."""
        if value is None:
            return []
        if not isinstance(value, list) or any(not isinstance(ref, str) for ref in value):
            raise ReviewLoopProtocolError("invalid_evidence_refs")
        refs = list(dict.fromkeys(value))
        if any(ref not in allowed_refs for ref in refs):
            raise ReviewLoopProtocolError("unknown_evidence_ref")
        return refs

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
            refs = self._refs(value, valid_refs)
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
        self._loop = _ActionLoop(client, config, evidence, trace)

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
            refs = ReviewAgentLoop._refs(data.get("evidence_refs"), valid_refs)
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
