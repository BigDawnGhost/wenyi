# 01 · Separate whole-book Review decisions, recovery and execution

[Index](README.md) · [简体中文](../zh/refactoring/01-review-workflow.md)

Status: partially implemented; baseline: `7471256`.

Implementation: state, checkpoint conversion, chunk/round execution and pure scan/fix decisions are isolated. The state owner handles clean confirmation, blocked issues, limits, overlay validation and cycles; the coordinator retains artifact-before-acceptance ordering. Final result writing and coordinator cleanup remain pending.

## Evidence and scope

[`review_workflow.py`](../../trans_novel/pipeline/review_workflow.py) has 1,891 lines. `run_session()` at line 707 spans 717 lines; `review_chapter()` at line 1425 spans 397. The former owns checkpoint decoding, mutable overlays, clean streaks, blocked issues, cycle detection, patch history, result assembly and usage flushing. The latter contains cache lookup, initial screening, evidence-loop invocation, adaptive splitting, singleton retries and concurrent merge.

These are intersecting change axes: adding a termination rule currently requires reasoning about checkpoint restoration and final unresolved issues in the same function. Splitting only the file would leave that problem intact. This proposal changes ownership, preserving the present policy.

## Proposed boundary

| Proposed module | Owns | Must not own |
| --- | --- | --- |
| `review/session.py` | `ReviewSessionState`, `RoundResult`, overlay and blocked-issue state, pure `after_scan` / `after_fix` decisions | Files, clients, thread pools |
| `pipeline/review_checkpoint.py` | Decode/encode the existing checkpoint, identity checks and resume selection through `ReviewRunStore` | Decisions about whether an issue is fixed |
| `pipeline/review_chunks.py` | Initial screening, adaptive block recovery, cache reuse, lazy chapter glossary, ordered chunk execution | Whole-book stopping policy or formal publishing |
| `pipeline/review_rounds.py` | One round: chapter scans, normalization, conflict arbitration and patch proposals | Cross-round state or formal target writes |
| `pipeline/review_results.py` | Net changes, unresolved views, result/diagnostic assembly | Additional model calls |
| `pipeline/review_workflow.py` | `ReviewService` session coordination and I/O sequencing | Long nested closures owning shared mutable state |

`ReviewService` owns one mutable session state. Round executors receive a read-only view of effective targets and return results. Pure decisions return an explicit next action and updated state; the coordinator performs checkpoint writes and usage flushes at their existing boundaries. Do not introduce a general event-sourcing engine or a runner for arbitrary workflows.

## Recovery contract

| Persisted boundary | Resume behavior to preserve |
| --- | --- |
| Completed review with matching content/config/glossary | Return the saved outcome without repeating model work. |
| No completed scan checkpoint | Reuse valid chunk/agent artifacts while completing the scan. |
| `phase=scan_done` | Restore that round's issues and snapshots; continue decisions/fixing without rescanning it. |
| `phase=round_done` | Restore overlays, blocked issues, counters and patch history before the next round. |

Keep reduced-limit handling, independent clean confirmations, unresolved fixer failures, no-progress termination and A/B/A cycle rejection exactly as implemented. The order “save scan checkpoint, flush scan usage, begin fixing” is part of the extraction baseline. Blind rechecks see effective shadow text without previous issue descriptions. Formal chapters, manifest and glossary remain read-only here.

## Implementation slices

1. Introduce the typed session and checkpoint codecs without changing JSON keys or defaults. Snapshot encode/decode and uninterrupted-versus-resumed behavior.
2. Extract pure overlay/issue/result helpers and decision functions; use their old call sites unchanged.
3. Move chunk execution and then round execution. Keep pools in those services, preserve cache-before-glossary lookup and one lazy glossary snapshot per chapter.
4. Reduce `run_session()` to load/restore, execute, decide, checkpoint and finalize. Keep runtime usage and outer invocation timing at their existing scopes.

Use proposal 02's pure contracts before extracting agent-related execution. Proposal 03 remains a separate publisher; do not combine it into this coordinator.

## Acceptance

Existing anchors: `tests/test_review_polish.py` covers malformed-block splitting, singleton recovery, ordered concurrency and glossary sharing; `tests/test_orchestrator.py` covers clean confirmations, blocked failures, cycles, trace reuse and usage on failure; `tests/test_routing_resume.py` covers ledger recovery.

Add interruption injection immediately before/after `scan_done`, scan usage flushing and `round_done`. Compare public issues, net changes, blocked issues, request sets/order and usage after resume against the uninterrupted baseline. Confirm valid caches avoid repeated glossary/evidence setup. Measure cold and resumed startup on synthetic multi-chapter data if performance is changed; line reduction is not a benchmark. Run the full suite and both architecture contracts.

Future semantic retrieval can replace an evidence query implementation at a round boundary; new quality policies should extend explicit decisions rather than the session's I/O code. Neither capability is implemented by this refactor.
