"""Verify one immutable Autofix issue group and reuse its persisted fixer trace."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from ..agents.review_fixer import ProvisionalPatch, ReviewFixer
from ..agents.review_loop import ReviewAgentLoop
from ..config import Config
from ..glossary.store import GlossaryStore, GlossaryTerm
from ..llm.base import LLMClient
from ..review.autofix_models import text_hash
from ..review.evidence import BookEvidenceIndex
from ..review.models import ReviewOutcome, review_candidate_id
from ..review.run_store import ReviewRunStore
from .review_checkpoint import ReviewTraceStore


class AutofixVerification:
    def __init__(
        self,
        config: Config,
        client: LLMClient,
        evidence: BookEvidenceIndex,
        debug: ReviewRunStore,
        all_terms: list[GlossaryTerm],
        analysis: dict[str, Any],
        outcome: ReviewOutcome,
        style_brief: Callable[[dict], str],
    ):
        self.evidence = evidence
        self.debug = debug
        self.all_terms = all_terms
        self.review_agent = ReviewAgentLoop(
            client,
            config,
            evidence,
            ReviewTraceStore(debug),
            operation="autofix.verify",
        )
        self.fixer = ReviewFixer(client, config, operation="autofix.fix")
        self.style = style_brief(analysis)
        self.book_synopsis = str(analysis.get("book_synopsis", "") or "")
        self.fixer_round = max(
            1,
            int((outcome.result.get("summary") or {}).get("review_round_count") or 0) + 1,
        )

    def fix_job(
        self,
        job: tuple[tuple[int, int], list[dict[str, Any]]],
    ) -> dict[str, Any]:
        location, location_issues = job
        segment = self.evidence.segment_ref(*location)
        if segment is None:
            return {
                "location": location,
                "original_issues": location_issues,
                "verified_issues": [],
                "dismissed": [],
                "patch": None,
                "status": "failed",
                "reason": "segment_not_found",
            }

        localized = [{**dict(issue), "index": 0} for issue in location_issues]
        candidates = {
            review_candidate_id(location[0], location[1], ordinal, self.fixer_round): issue
            for ordinal, issue in enumerate(location_issues)
        }
        loop_outcome = self.review_agent.review_chunk(
            chapter=location[0],
            chunk_base=location[1],
            sources=[segment.source],
            targets=[segment.target],
            initial_issues=localized,
            review_round=self.fixer_round,
        )
        if loop_outcome.fallback_reason:
            return {
                "location": location,
                "original_issues": location_issues,
                "verified_issues": [],
                "dismissed": [],
                "patch": None,
                "status": "failed",
                "reason": f"review_agent_fallback:{loop_outcome.fallback_reason}",
            }

        verified: list[dict[str, Any]] = []
        for ordinal, issue in enumerate(loop_outcome.issues):
            mapped = dict(issue)
            candidate_id = mapped.get("candidate_id")
            original = candidates.get(str(candidate_id))
            if original is not None:
                issue_id = str(
                    original.get("issue_id") or original.get("issue_key") or candidate_id
                )
                issue_key = str(original.get("issue_key") or issue_id)
            else:
                identity = json.dumps(
                    [
                        location[0],
                        location[1],
                        mapped.get("type"),
                        mapped.get("detail"),
                        mapped.get("suggestion"),
                        ordinal,
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                issue_id = f"autofix-agent-{text_hash(identity)[:16]}"
                issue_key = issue_id
            mapped.update(
                {
                    "issue_id": issue_id,
                    "issue_key": issue_key,
                    "chapter": location[0],
                    "index": location[1],
                }
            )
            verified.append(mapped)

        dismissed: list[dict[str, Any]] = []
        for item in loop_outcome.dismissed:
            candidate_id = str(item.get("candidate_id") or "")
            original = candidates.get(candidate_id)
            if original is None:
                continue
            dismissed.append(
                {
                    **dict(item),
                    "issue_id": str(
                        original.get("issue_id") or original.get("issue_key") or candidate_id
                    ),
                    "issue_key": str(original.get("issue_key") or candidate_id),
                    "chapter": location[0],
                    "index": location[1],
                }
            )
        if not verified:
            return {
                "location": location,
                "original_issues": location_issues,
                "verified_issues": [],
                "dismissed": dismissed,
                "patch": None,
                "status": "dismissed",
                "reason": "all_issues_dismissed",
            }

        context = self.evidence.segment_context(
            {
                "chapter": location[0],
                "index": location[1],
                "before": 4,
                "after": 4,
            }
        )
        context_segments = context.get("segments", []) if context.get("ok") else []
        nearby_pairs = [
            (str(item.get("source", "")), str(item.get("target", "")))
            for item in context_segments
            if isinstance(item, dict) and item.get("ref") != segment.ref
        ]
        context_source = "\n".join(
            str(item.get("source", "")) for item in context_segments if isinstance(item, dict)
        )
        relevant_terms = GlossaryStore.terms_in(
            self.all_terms,
            context_source or segment.source,
        )
        trace_path = f"autofix/fixers/ch{location[0]}-text{location[1]}.json"
        existing_trace = self.debug.load_json(trace_path)
        if isinstance(existing_trace, dict):
            cached_patch = existing_trace.get("patch")
            if existing_trace.get("status") == "finished" and isinstance(cached_patch, dict):
                try:
                    patch = ProvisionalPatch(
                        patch_id=str(cached_patch["patch_id"]),
                        round=int(cached_patch["round"]),
                        segment_ref=str(cached_patch["segment_ref"]),
                        chapter=int(cached_patch["chapter"]),
                        index=int(cached_patch["index"]),
                        before_hash=str(cached_patch["before_hash"]),
                        before=str(cached_patch["before"]),
                        after=str(cached_patch["after"]),
                        issue_ids=tuple(str(item) for item in cached_patch["issue_ids"]),
                    )
                except (KeyError, TypeError, ValueError):
                    patch = None
                if patch is not None:
                    return {
                        "location": location,
                        "original_issues": location_issues,
                        "verified_issues": verified,
                        "dismissed": dismissed,
                        "patch": patch,
                        "status": "fixed",
                        "reason": "",
                    }
            if existing_trace.get("status") == "failed":
                cached_error = existing_trace.get("error") or {}
                return {
                    "location": location,
                    "original_issues": location_issues,
                    "verified_issues": verified,
                    "dismissed": dismissed,
                    "patch": None,
                    "status": "failed",
                    "reason": str(cached_error.get("message") or "cached_fixer_failure"),
                }
        trace: dict[str, Any] = {
            "chapter": location[0],
            "index": location[1],
            "segment_ref": segment.ref,
            "status": "running",
        }
        self.debug.write_json(trace_path, trace)

        def record(event: str, data: dict[str, Any]) -> None:
            trace[event] = data
            self.debug.write_json(trace_path, trace)

        try:
            patch = self.fixer.propose(
                self.fixer_round,
                segment.ref,
                location[0],
                location[1],
                segment.source,
                segment.target,
                verified,
                style=self.style,
                book_synopsis=self.book_synopsis,
                chapter_digest=self.evidence.chapter_digests.get(location[0], ""),
                relevant_glossary=relevant_terms,
                nearby_pairs=nearby_pairs,
                trace=record,
            )
        except Exception as error:  # noqa: BLE001 - Preserve unchanged issues when final verification fails.
            trace["status"] = "failed"
            trace["error"] = {
                "type": type(error).__name__,
                "message": str(error),
            }
            self.debug.write_json(trace_path, trace)
            return {
                "location": location,
                "original_issues": location_issues,
                "verified_issues": verified,
                "dismissed": dismissed,
                "patch": None,
                "status": "failed",
                "reason": f"{type(error).__name__}:{error}",
            }
        trace["status"] = "finished"
        trace["patch"] = patch.as_dict()
        self.debug.write_json(trace_path, trace)
        return {
            "location": location,
            "original_issues": location_issues,
            "verified_issues": verified,
            "dismissed": dismissed,
            "patch": patch,
            "status": "fixed",
            "reason": "",
        }
