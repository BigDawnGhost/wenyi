"""Bounded Review action protocol with explicit in-memory conversation replay."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from ..config import Config
from ..llm.base import LLMClient
from ..llm.json_parser import parse_json_result
from ..review.contracts import EvidenceTools, ReviewTrace
from ..review.models import clean_text


class ReviewLoopProtocolError(ValueError):
    """The agent loop returned content that violates the action protocol."""


def validate_evidence_refs(value: Any, allowed_refs: set[str]) -> list[str]:
    """Verify that final output references only evidence actually obtained by this loop."""
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(ref, str) for ref in value):
        raise ReviewLoopProtocolError("invalid_evidence_refs")
    refs = list(dict.fromkeys(value))
    if any(ref not in allowed_refs for ref in refs):
        raise ReviewLoopProtocolError("unknown_evidence_ref")
    return refs


@dataclass
class ConversationState:
    """Own replayed messages, citable references and request deduplication for one call."""

    messages: list[dict[str, Any]]
    allowed_refs: set[str]
    max_rounds: int
    evidence_rounds: int = 0
    seen_request_ids: set[str] = field(default_factory=set)
    seen_requests: set[tuple[str, str]] = field(default_factory=set)

    def replay(
        self,
        resume_turns: list[dict[str, Any]],
        evidence_refs: Callable[[Any], set[str]],
    ) -> int:
        """Restore completed evidence rounds and return the first unfinished turn number."""
        for cached in resume_turns:
            cached_results = cached.get("evidence_results")
            cached_raw = cached.get("raw_response")
            if not isinstance(cached_results, list) or not isinstance(cached_raw, str):
                continue
            self.messages.append({"role": "assistant", "content": cached_raw})
            evidence_message = "[Evidence tool results (JSON)]\n" + json.dumps(
                cached_results, ensure_ascii=False, indent=2
            )
            self.evidence_rounds += 1
            if self.evidence_rounds >= self.max_rounds:
                evidence_message += "\nEvidence rounds are exhausted. The next response must use action=final; do not request more evidence."
            self.messages.append({"role": "user", "content": evidence_message})
            self.allowed_refs.update(evidence_refs(cached_results))
            cached_parsed = cached.get("parsed")
            if isinstance(cached_parsed, dict):
                for request in cached_parsed.get("requests", []):
                    if not isinstance(request, dict):
                        continue
                    request_id = clean_text(request.get("request_id"))
                    if request_id:
                        self.seen_request_ids.add(request_id)
                    tool = clean_text(request.get("tool"))
                    arguments = request.get("arguments")
                    if tool and isinstance(arguments, dict):
                        self.seen_requests.add(
                            (tool, json.dumps(arguments, ensure_ascii=False, sort_keys=True))
                        )
        # Resume at the first unfinished turn. Re-enter the final cached turn if it lacks evidence results
        # because a request, evidence operation or parsed final response was not yet persisted.
        start_turn = len(resume_turns)
        if "evidence_results" in resume_turns[-1]:
            start_turn += 1
        return start_turn

    def claim_requests(self, data: dict[str, Any]) -> list[dict[str, Any]]:
        """Validate a complete request group before reserving its IDs and signatures."""
        if data.get("complete") is not False:
            raise ReviewLoopProtocolError("evidence_action_marked_complete")
        if self.evidence_rounds >= self.max_rounds:
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
            if not request_id or request_id in self.seen_request_ids or request_id in current_ids:
                raise ReviewLoopProtocolError("duplicate_evidence_request_id")
            tool = clean_text(request.get("tool"))
            arguments = request.get("arguments")
            if not tool or not isinstance(arguments, dict):
                raise ReviewLoopProtocolError("invalid_evidence_request")
            signature = (
                tool,
                json.dumps(arguments, ensure_ascii=False, sort_keys=True),
            )
            if signature in self.seen_requests or signature in current_requests:
                raise ReviewLoopProtocolError("duplicate_evidence_request")
            current_ids.add(request_id)
            current_requests.add(signature)
        self.seen_request_ids.update(current_ids)
        self.seen_requests.update(current_requests)
        return requests


class ReviewActionLoop:
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
        conversation = ConversationState(messages, allowed_refs, max_rounds)
        start_turn = 1
        if resume_turns:
            # Replay completed evidence rounds to restore message history and deduplication state.
            # Message order, evidence JSON and round instructions must match the original run byte for byte
            # so an in-flight call can resume seamlessly.
            first_messages = resume_turns[0].get("messages")
            if isinstance(first_messages, list):
                conversation.messages = [dict(message) for message in first_messages]
            self.trace.log_event(
                "review_agent_resumed",
                agent_id=agent_id,
                stage=stage,
                turns=len(resume_turns),
            )
            start_turn = conversation.replay(resume_turns, self.evidence.evidence_refs)
        self.trace.save(agent_id, trace)
        cached_by_turn = {
            turn["turn"]: turn for turn in resume_turns if isinstance(turn.get("turn"), int)
        }

        try:
            for turn_number in range(start_turn, max(start_turn, max_rounds + 1) + 1):
                sent_messages = [dict(message) for message in conversation.messages]
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
                    result = validate_final(data, conversation.allowed_refs)
                    trace["status"] = "finished"
                    trace["result"] = result
                    self.trace.save(agent_id, trace)
                    self.trace.log_event(
                        "review_agent_finished",
                        agent_id=agent_id,
                        stage=stage,
                        turns=turn_number,
                        evidence_rounds=conversation.evidence_rounds,
                    )
                    return result, ""

                if action != "request_evidence":
                    raise ReviewLoopProtocolError("unknown_action")
                requests = conversation.claim_requests(data)

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
                conversation.evidence_rounds += 1
                conversation.allowed_refs.update(self.evidence.evidence_refs(results))
                turn["evidence_results"] = results
                self.trace.save(agent_id, trace)
                self.trace.log_event(
                    "review_evidence_supplied",
                    agent_id=agent_id,
                    stage=stage,
                    round=conversation.evidence_rounds,
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
                conversation.messages.append({"role": "assistant", "content": raw})
                evidence_message = "[Evidence tool results (JSON)]\n" + json.dumps(
                    results, ensure_ascii=False, indent=2
                )
                if conversation.evidence_rounds >= max_rounds:
                    evidence_message += "\nEvidence rounds are exhausted. The next response must use action=final; do not request more evidence."
                conversation.messages.append({"role": "user", "content": evidence_message})
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
