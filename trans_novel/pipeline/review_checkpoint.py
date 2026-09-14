"""Adapt Review persistence to the narrow ports consumed by agents."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..ingest.models import Chapter
from ..review.run_store import ReviewRunStore
from ..review.session import ReviewRoundResult, ReviewSessionState


class ReviewTraceStore:
    """Expose agent traces within one Review and its caller-owned active round scope."""

    def __init__(self, store: ReviewRunStore) -> None:
        self._store = store

    @staticmethod
    def _relative(agent_id: str) -> str:
        safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", agent_id).strip("-") or "agent"
        return f"agents/{safe_id}.json"

    def load(self, agent_id: str) -> dict[str, Any] | None:
        """Read the existing trace using the unchanged filename and round rules."""
        return self._store.load_json(self._relative(agent_id))

    def save(self, agent_id: str, snapshot: dict[str, Any]) -> None:
        """Commit immediately through the existing atomic JSON writer."""
        self._store.write_json(self._relative(agent_id), snapshot)

    def log_event(self, event: str, **data: Any) -> None:
        """Preserve event ordering, locking and active-round annotations."""
        self._store.log_event(event, **data)


@dataclass
class ReviewRecovery:
    """Decoded session and the completed scan eligible for reuse on the first round."""

    state: ReviewSessionState
    start_round: int
    latest: ReviewRoundResult | None


class ReviewCheckpoint:
    """Encode and restore existing checkpoints within one Review directory."""

    def __init__(self, store: ReviewRunStore) -> None:
        self._store = store

    def restore(self, initial_digest: str, max_review_rounds: int) -> ReviewRecovery:
        """Restore checkpoint fields and completed-round diagnostics without model calls."""
        debug = self._store
        state = ReviewSessionState(seen_overlays={initial_digest})
        # Resume from the round checkpoint.
        _checkpoint = debug.load_checkpoint()
        _resume_latest: ReviewRoundResult | None = None
        if _checkpoint is not None:
            start_round = _checkpoint.get("next_round", 1)
            # Tighter settings, such as fewer clean confirmations, may lower the round limit.
            # Clamp an out-of-range checkpoint to the final round instead of producing an empty loop.
            start_round = min(start_round, max_review_rounds)
            state.target_overrides = {
                (o["chapter"], o["index"]): o["target"]
                for o in _checkpoint.get("target_overrides", [])
            }
            state.seen_overlays = set(_checkpoint.get("seen_overlays", []))
            state.patch_records = _checkpoint.get("patch_records", [])
            history_by_id = {patch.get("patch_id"): patch for patch in state.patch_records}
            state.active_patches = {
                (p["chapter"], p["index"]): history_by_id[p.get("patch_id")]
                if p.get("patch_id") in history_by_id
                else p
                for p in _checkpoint.get("active_patches", [])
            }
            state.fix_failures = _checkpoint.get("fix_failures", [])
            state.blocked_issues = _checkpoint.get("blocked_issues", {})
            state.round_summaries = _checkpoint.get("round_summaries", [])
            state.clean_streak = _checkpoint.get("clean_streak", 0)
            state.fix_rounds = _checkpoint.get("fix_rounds", 0)
            # Restore the within-round phase; scan_done allows skipping the completed scan.
            if _checkpoint.get("phase") == "scan_done":
                start_round = _checkpoint.get("next_round", 1)
                # Do not reuse an old scan when a reduced limit is below the checkpoint's round number.
                if start_round > max_review_rounds:
                    _resume_latest = None
                    start_round = max_review_rounds
                else:
                    _resume_latest = ReviewRoundResult(
                        issues=_checkpoint.get("latest_issues", []),
                        pre_arbitration_issues=_checkpoint.get("latest_pre_arbitration_issues", []),
                        arbitration_superseded=_checkpoint.get("latest_arbitration_superseded", []),
                        conflict_groups=_checkpoint.get("latest_conflict_groups", []),
                        residual_conflicts=_checkpoint.get("latest_residual_conflicts", []),
                        fallback_agent_count=_checkpoint.get("latest_fallback_agent_count", 0),
                    )
                debug.log_event(
                    "review_checkpoint_restored",
                    next_round=start_round,
                    phase="scan_done",
                    override_count=len(state.target_overrides),
                    fix_rounds=state.fix_rounds,
                    clean_streak=state.clean_streak,
                )
            else:
                debug.log_event(
                    "review_checkpoint_restored",
                    next_round=start_round,
                    phase="round_done",
                    override_count=len(state.target_overrides),
                    fix_rounds=state.fix_rounds,
                    clean_streak=state.clean_streak,
                )
        else:
            start_round = 1

        # Completed rounds no longer execute their chunk aggregation when resuming.
        for completed_round in range(1, start_round):
            with debug.round_scope(completed_round):
                debug.rebuild_snapshots_from_chunks(completed_round)

        return ReviewRecovery(state, start_round, _resume_latest)

    def save(
        self,
        state: ReviewSessionState,
        current_round: int,
        phase: str = "round_done",
        latest: ReviewRoundResult | None = None,
    ) -> None:
        """Save the round checkpoint with phase round_done or scan_done."""
        payload: dict[str, Any] = {
            "phase": phase,
            "next_round": current_round + 1 if phase == "round_done" else current_round,
            "target_overrides": [
                {"chapter": c, "index": i, "target": t}
                for (c, i), t in sorted(state.target_overrides.items())
            ],
            "seen_overlays": sorted(state.seen_overlays),
            "patch_records": state.patch_records,
            "active_patches": [
                {**p, "chapter": c, "index": i}
                for (c, i), p in sorted(state.active_patches.items())
            ],
            "fix_failures": state.fix_failures,
            "blocked_issues": state.blocked_issues,
            "round_summaries": state.round_summaries,
            "clean_streak": state.clean_streak,
            "fix_rounds": state.fix_rounds,
        }
        if latest is not None:
            payload["latest_issues"] = latest.issues
            payload["latest_pre_arbitration_issues"] = latest.pre_arbitration_issues
            payload["latest_arbitration_superseded"] = latest.arbitration_superseded
            payload["latest_conflict_groups"] = latest.conflict_groups
            payload["latest_residual_conflicts"] = latest.residual_conflicts
            payload["latest_fallback_agent_count"] = latest.fallback_agent_count
        self._store.save_checkpoint(payload)


@dataclass(frozen=True)
class ReviewInputs:
    """Loaded formal snapshots and the run directory selected for one Review session."""

    chapters: list[Chapter]
    analysis: dict[str, Any]
    debug: ReviewRunStore
