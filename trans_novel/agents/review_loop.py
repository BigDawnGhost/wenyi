"""有界 Review Agent Loop 与全书跨块冲突仲裁。"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable

from ..config import Config
from ..llm.base import LLMClient
from ..llm.json_parser import parse_json_result
from ..review.evidence import BookEvidenceIndex
from ..review.run_store import ReviewRunStore, review_candidate_id
from . import prompts

_ISSUE_TYPES = {"missing", "added", "mistranslation", "terminology", "pronoun"}
_CONSISTENCY_KINDS = {"term", "pronoun", "fixed"}
_MAX_ARBITRATION_PROPOSALS = 32
_MAX_ARBITRATION_PAYLOAD_BYTES = 96_000
_ARBITRATION_SAMPLE_TEXT_LIMIT = 1500


class ReviewLoopProtocolError(ValueError):
    """Agent Loop 返回了不符合动作协议的内容。"""


@dataclass(frozen=True)
class ReviewLoopOutcome:
    """一个审校叶块经 Agent Loop 核验后的结果。"""

    issues: list[dict[str, Any]]
    dismissed: list[dict[str, Any]]
    fallback_reason: str = ""


def _text(value: Any) -> str:
    """只接受字符串并去除首尾空白。"""
    return value.strip() if isinstance(value, str) else ""


def _normalized(value: str) -> str:
    """统一兼容字符、宽度和大小写以比较建议值。"""
    return unicodedata.normalize("NFKC", value).casefold().strip()


def _identity_text(value: Any) -> str:
    """规整问题身份字段中的空白和兼容字符，降低跨轮措辞抖动。"""
    return re.sub(r"\s+", " ", _normalized(_text(value)))


def _review_issue_key(issue: dict[str, Any]) -> str:
    """生成跨 Review 轮次稳定、与临时 ``issue_id`` 无关的问题键。"""
    consistency = issue.get("consistency")
    consistency_key = (
        _identity_text(consistency.get("key")) if isinstance(consistency, dict) else ""
    )
    subject = consistency_key or _identity_text(issue.get("detail"))
    payload = json.dumps(
        [
            issue.get("chapter"),
            issue.get("index"),
            _identity_text(issue.get("type")),
            subject,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"review-issue-{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:20]}"


def _safe_id(value: str) -> str:
    """把 agent/conflict ID 转为安全文件名。"""
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "agent"


class _ActionLoop:
    """在普通 messages 接口上模拟 request-evidence/final 工具循环。"""

    def __init__(
        self,
        client: LLMClient,
        config: Config,
        evidence: BookEvidenceIndex,
        debug: ReviewRunStore,
    ):
        self.client = client
        self.config = config
        self.evidence = evidence
        self.debug = debug

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
        """执行最多 N 轮取证加一次最终调用；失败返回原因而不抛出。"""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        max_rounds = self.config.pipeline.review_agent_max_evidence_rounds
        if max_rounds == 0:
            messages[-1]["content"] += "\n本次不允许取证；当前响应必须直接输出 final。"
        trace: dict[str, Any] = {
            "agent_id": agent_id,
            "stage": stage,
            "status": "running",
            "turns": [],
        }
        relative = f"agents/{_safe_id(agent_id)}.json"
        # 断点续跑：先探测已有 trace（必须先 load 再写，否则覆盖自己）
        existing = self.debug.load_json(relative)
        resume_turns: list[dict[str, Any]] = []
        if existing is not None:
            existing_status = existing.get("status")
            if existing_status == "finished" and isinstance(existing.get("result"), dict):
                self.debug.log_event(
                    "review_agent_finished",
                    agent_id=agent_id,
                    stage=stage,
                    turns=len(existing.get("turns", [])),
                    resumed=True,
                )
                return existing["result"], ""
            if existing_status == "fallback":
                self.debug.log_event(
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
            # 断点续跑：重放已完成取证轮，重建消息历史与去重状态。
            # 消息顺序、证据 JSON 与取证轮次提示必须与原始运行逐字节
            # 一致，后续 in-flight 调用才能无缝续上。
            first_messages = resume_turns[0].get("messages")
            if isinstance(first_messages, list):
                messages = [dict(message) for message in first_messages]
            self.debug.log_event(
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
                evidence_message = "【证据工具返回 JSON】\n" + json.dumps(
                    cached_results, ensure_ascii=False, indent=2
                )
                evidence_rounds += 1
                if evidence_rounds >= max_rounds:
                    evidence_message += (
                        "\n取证轮次已用完。下一次响应只能输出 action=final，不得再次请求证据。"
                    )
                messages.append({"role": "user", "content": evidence_message})
                allowed_refs.update(self.evidence.evidence_refs(cached_results))
                cached_parsed = cached.get("parsed")
                if isinstance(cached_parsed, dict):
                    for request in cached_parsed.get("requests", []):
                        if not isinstance(request, dict):
                            continue
                        request_id = _text(request.get("request_id"))
                        if request_id:
                            seen_request_ids.add(request_id)
                        tool = _text(request.get("tool"))
                        arguments = request.get("arguments")
                        if tool and isinstance(arguments, dict):
                            seen_requests.add(
                                (tool, json.dumps(arguments, ensure_ascii=False, sort_keys=True))
                            )
            # 从第一个未完成 turn 续跑：最后一个缓存 turn 若无证据结果
            # （in-flight 请求、取证执行中或 final 已解析未落盘）原地重入。
            start_turn = len(resume_turns)
            if "evidence_results" in resume_turns[-1]:
                start_turn += 1
        self.debug.write_json(relative, trace)
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
                self.debug.write_json(relative, trace)
                if cached_turn is not None and isinstance(cached_turn.get("raw_response"), str):
                    raw = cached_turn["raw_response"]
                    turn["status"] = "responded"
                    turn["raw_response"] = raw
                else:
                    try:
                        raw = self.client.complete(
                            sent_messages,
                            tier=self.config.pipeline.review_agent_tier,
                            json_mode=True,
                            stage=stage,
                        )
                    except Exception as error:
                        turn["status"] = "failed"
                        turn["error"] = {
                            "type": type(error).__name__,
                            "message": str(error),
                        }
                        self.debug.write_json(relative, trace)
                        raise
                    turn["status"] = "responded"
                    turn["raw_response"] = raw
                self.debug.write_json(relative, trace)

                # 仅当 raw 与 parsed 都来自同一缓存响应时才复用 parsed；
                # 无 raw 的残留 parsed（手工篡改/损坏 trace）不得遮蔽新调用结果。
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
                self.debug.write_json(relative, trace)
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
                    self.debug.write_json(relative, trace)
                    self.debug.log_event(
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
                    request_id = _text(request.get("request_id"))
                    if (
                        not request_id
                        or request_id in seen_request_ids
                        or request_id in current_ids
                    ):
                        raise ReviewLoopProtocolError("duplicate_evidence_request_id")
                    tool = _text(request.get("tool"))
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
                            "hint": "减少同轮请求数或缩小各请求的上下文范围。",
                        }
                        encoded_size = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
                    results.append(result)
                    batch_size += encoded_size + 1
                evidence_rounds += 1
                allowed_refs.update(self.evidence.evidence_refs(results))
                turn["evidence_results"] = results
                self.debug.write_json(relative, trace)
                self.debug.log_event(
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
                evidence_message = "【证据工具返回 JSON】\n" + json.dumps(
                    results, ensure_ascii=False, indent=2
                )
                if evidence_rounds >= max_rounds:
                    evidence_message += (
                        "\n取证轮次已用完。下一次响应只能输出 action=final，不得再次请求证据。"
                    )
                messages.append({"role": "user", "content": evidence_message})
        except Exception as error:  # noqa: BLE001 - Loop 失败按产品约定回退初审
            reason = (
                str(error)
                if isinstance(error, ReviewLoopProtocolError)
                else f"{type(error).__name__}: {error}"
            )
            trace["status"] = "fallback"
            trace["fallback_reason"] = reason
            self.debug.write_json(relative, trace)
            self.debug.log_event(
                "review_agent_fallback",
                agent_id=agent_id,
                stage=stage,
                reason=reason,
            )
            return None, reason
        trace["status"] = "fallback"
        trace["fallback_reason"] = "loop_ended_without_final"
        self.debug.write_json(relative, trace)
        return None, "loop_ended_without_final"


class ReviewAgentLoop:
    """核验一个成功初审叶块，并允许在该块内补充问题。"""

    def __init__(
        self,
        client: LLMClient,
        config: Config,
        evidence: BookEvidenceIndex,
        debug: ReviewRunStore,
    ):
        self.config = config
        self.evidence = evidence
        self.debug = debug
        self._loop = _ActionLoop(client, config, evidence, debug)

    @staticmethod
    def _consistency(value: Any) -> dict[str, str]:
        """清洗跨块一致性 claim；普通问题返回空字典。"""
        if value is None or value == {}:
            return {}
        if not isinstance(value, dict):
            raise ReviewLoopProtocolError("invalid_consistency")
        kind = _text(value.get("kind"))
        subject = _text(value.get("subject_source"))
        proposed = _text(value.get("proposed_value"))
        if not kind and not subject and not proposed:
            return {}
        if kind not in _CONSISTENCY_KINDS or not subject or not proposed:
            raise ReviewLoopProtocolError("invalid_consistency")
        return {
            "kind": kind,
            "subject_source": subject,
            "proposed_value": proposed,
        }

    @staticmethod
    def _refs(value: Any, allowed_refs: set[str]) -> list[str]:
        """验证最终输出只引用当前 Loop 实际取得的证据。"""
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
        """运行块级有界取证；失败时原样保留所有初审候选。"""
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
        self.debug.log_event(
            "review_agent_started",
            agent_id=agent_id,
            chapter=chapter,
            chunk_base=chunk_base,
            segment_count=len(sources),
            candidate_count=len(candidates),
        )
        system = prompts.render(
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
        user = prompts.render(
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
            """把当前段自身 ref 自动并入模型显式引用，保证建议始终可追溯。"""
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
                candidate_id = _text(decision.get("candidate_id"))
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
                    reason = _text(decision.get("reason"))
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
                detail = _text(decision.get("detail")) or _text(candidate.get("detail"))
                suggestion = _text(decision.get("suggestion")) or _text(candidate.get("suggestion"))
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
                detail = _text(issue.get("detail"))
                suggestion = _text(issue.get("suggestion"))
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
            stage="ReviewAgent",
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


def normalize_review_issues(
    issues: list[dict[str, Any]],
    evidence: BookEvidenceIndex,
) -> list[dict[str, Any]]:
    """确定性清洗，并生成轮内 ID 与跨轮稳定的问题键。"""
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
            kind = _text(consistency.get("kind"))
            subject = _text(consistency.get("subject_source"))
            proposed = _text(consistency.get("proposed_value"))
            if kind in _CONSISTENCY_KINDS and subject and proposed:
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
                        f"glossary:{canonical}" if term is not None else _normalized(canonical)
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
        issue_key = _review_issue_key(item)
        if issue_key in seen_keys:
            continue
        seen_keys.add(issue_key)
        item["issue_key"] = issue_key
        item["issue_id"] = f"review-{len(prepared) + 1:05d}"
        prepared.append(item)
    return prepared


def build_conflict_groups(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """找出不同审校块对同一一致性主题提出的互斥值。"""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for issue in issues:
        consistency = issue.get("consistency")
        if not isinstance(consistency, dict):
            continue
        key = _text(consistency.get("key"))
        proposed = _text(consistency.get("proposed_value"))
        if key and proposed:
            grouped.setdefault(key, []).append(issue)

    conflicts: list[dict[str, Any]] = []
    for key, group in grouped.items():
        chunks = {issue.get("_chunk_id") for issue in group}
        values = {
            _normalized(_text(issue.get("consistency", {}).get("proposed_value")))
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
    """把终局仲裁应用到建议视图，不修改正文或术语库。

    ``suggested`` 冲突保留所有已确认的问题：原建议值落选的位置仍然需要修正，
    因此把其建议改写为最终统一值，同时另存仲裁前版本供逐轮审计。
    ``unresolved`` 冲突保留全部问题并附上未解决标记。
    """
    by_id = {
        str(issue["issue_id"]): dict(issue)
        for issue in issues
        if isinstance(issue.get("issue_id"), str)
    }
    superseded_rows: list[dict[str, Any]] = []
    for arbitration in arbitrations:
        conflict_id = _text(arbitration.get("conflict_id"))
        status = arbitration.get("status")
        annotation = {
            "conflict_id": conflict_id,
            "status": status,
            "recommended_value": _text(arbitration.get("recommended_value")),
            "reason": _text(arbitration.get("reason")),
        }
        if status == "suggested":
            for issue_id in arbitration.get("rejected_issue_ids", []):
                issue = by_id.get(str(issue_id))
                if issue is not None:
                    recommended = annotation["recommended_value"]
                    consistency = issue.get("consistency")
                    issue_annotation = {**annotation, "action": "rewritten"}
                    superseded_rows.append({**issue, "arbitration": issue_annotation})
                    previous_detail = _text(issue.get("detail"))
                    previous_suggestion = _text(issue.get("suggestion"))
                    issue["pre_arbitration_detail"] = previous_detail
                    issue["pre_arbitration_suggestion"] = previous_suggestion
                    issue["detail"] = f"该处相关表达需按终局仲裁统一为「{recommended}」。"
                    issue["suggestion"] = f"按终局仲裁将相关表达统一为「{recommended}」。"
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


class ReviewConflictArbiter:
    """在全部审校块完成后，对每个互斥一致性建议给出只读裁决建议。"""

    def __init__(
        self,
        client: LLMClient,
        config: Config,
        evidence: BookEvidenceIndex,
        debug: ReviewRunStore,
    ):
        self.config = config
        self.evidence = evidence
        self.debug = debug
        self._loop = _ActionLoop(client, config, evidence, debug)

    def arbitrate(self, conflict: dict[str, Any]) -> dict[str, Any]:
        """仲裁一个冲突组；失败时保留全部问题并标记 unresolved。"""
        conflict_id = str(conflict["conflict_id"])
        issue_ids = [str(issue["issue_id"]) for issue in conflict["issues"]]

        def unresolved(reason: str, refs: set[str] | None = None) -> dict[str, Any]:
            """构造不丢问题的保守结果，并记录未完成仲裁的原因。"""
            self.debug.log_event(
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
            proposed = _text(issue["consistency"]["proposed_value"])
            proposal_groups.setdefault(_normalized(proposed), []).append(issue)
        if len(proposal_groups) > _MAX_ARBITRATION_PROPOSALS:
            return unresolved(f"互斥建议值过多（{len(proposal_groups)}），超过选择性仲裁上限。")

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
                        "detail": _text(issue["detail"])[:_ARBITRATION_SAMPLE_TEXT_LIMIT],
                        "suggestion": _text(issue["suggestion"])[:_ARBITRATION_SAMPLE_TEXT_LIMIT],
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
            return unresolved("选择性仲裁样本仍超过输入大小上限。", sampled_refs)

        system = prompts.render(
            "review_arbiter_system",
            src=self.config.source_lang,
            tgt=self.config.target_lang,
            max_evidence_rounds=(self.config.pipeline.review_agent_max_evidence_rounds),
        )
        user = prompts.render(
            "review_arbiter_user",
            src=self.config.source_lang,
            tgt=self.config.target_lang,
            conflict_json=compact_json,
        )
        # 只预授权提示词里实际附有正文的样本 ref。块级 Agent 曾取到但未
        # 展示给仲裁器的证据，必须由仲裁器重新按需查询。
        allowed_refs = set(sampled_refs)

        def validate_final(data: dict[str, Any], valid_refs: set[str]) -> dict[str, Any]:
            if data.get("conflict_id") != conflict_id:
                raise ReviewLoopProtocolError("conflict_id_mismatch")
            if "supported_issue_ids" in data or "rejected_issue_ids" in data:
                raise ReviewLoopProtocolError("arbitration_issue_ids_must_be_omitted")
            status = data.get("status")
            if status not in {"suggested", "unresolved"}:
                raise ReviewLoopProtocolError("invalid_arbitration_status")
            recommended = _text(data.get("recommended_value"))
            reason = _text(data.get("reason"))
            if status == "suggested" and not recommended:
                raise ReviewLoopProtocolError("suggestion_without_value")
            if not reason:
                raise ReviewLoopProtocolError("arbitration_without_reason")
            if status == "suggested":
                normalized_recommendation = _normalized(recommended)
                if normalized_recommendation not in proposal_groups:
                    raise ReviewLoopProtocolError("recommended_value_not_proposed")
                recommended = _text(
                    proposal_groups[normalized_recommendation][0]["consistency"]["proposed_value"]
                )
                supported = [
                    str(issue["issue_id"])
                    for issue in conflict["issues"]
                    if _normalized(_text(issue["consistency"]["proposed_value"]))
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
            stage="ReviewArbiter",
            allowed_refs=allowed_refs,
            validate_final=validate_final,
        )
        if result is not None:
            return result
        return unresolved(f"仲裁 Agent 未能完成：{reason}", allowed_refs)
