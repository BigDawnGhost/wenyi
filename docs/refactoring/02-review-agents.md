# 02 · Separate the Review conversation protocol from persistence and arbitration

[Index](README.md) · [简体中文](../zh/refactoring/02-review-agents.md)

Status: partially implemented; high priority. Baseline: `7471256`.

Implementation: outcomes, segment references and stable identities now live in `review/models.py`; normalization, conflict grouping and applying decisions live in `review/conflicts.py`. Package exports contain only pure types. Agents now use `ReviewTrace` and evidence-query protocols. `pipeline/review_checkpoint.py` adapts traces to existing round-scoped storage. In-memory tests cover all nine durable boundaries; captured snapshots, events and model messages match the original loop. Replay-state and arbiter extraction remain pending.

## Evidence

[`agents/review_loop.py`](../../trans_novel/agents/review_loop.py) has 1,006 lines: `_ActionLoop.run()` is 300 lines, `ReviewAgentLoop.review_chunk()` 200, and `ReviewConflictArbiter.arbitrate()` 170. The module also contains issue identity, normalization, conflict grouping and arbitration application.

The concrete `ReviewRunStore` import at line 17 is a storage dependency, not merely a pure review model. `_ActionLoop` loads and saves traces directly. The current architecture test rejects pipeline imports but does not reject this concrete review storage import. The design should narrow that dependency, not describe the current tests as enforcing a stronger boundary than they do.

## Proposed ownership

- `review/models.py`: pure `ReviewOutcome`, `ReviewLoopOutcome`, issue/consistency types and deterministic candidate identity. Move pure types out of `review/run_store.py` and update consumers together.
- `review/conflicts.py`: normalization, conflict grouping and applying validated arbitration decisions. No LLM client or storage dependency.
- `review/contracts.py`: a small typed trace port and evidence-query contract. Trace operations cover loading a conversation snapshot, saving it at a durable boundary, and emitting a diagnostic event; there is no access to formal chapters or book usage.
- `agents/review_actions.py`: the evidence-request/final protocol and conversation replay. Use in-memory `ConversationState` plus the trace port; keep existing request validation and fallback behavior.
- `agents/review_loop.py`: chunk-specific prompt preparation and final issue validation.
- `agents/review_arbiter.py`: conflict-specific prompt preparation and final decision validation.
- `pipeline/review_checkpoint.py`: implements the trace port using `ReviewRunStore`, bound to the current review/round. Agents do not import that adapter.

Avoid a generic “all agents” framework. `_ActionLoop` already shares a real protocol between two callers; extract that protocol without forcing Translator, Polisher or ReviewFixer into it.

## Replay and evidence rules

Preserve completed/fallback trace reuse, inference identity invalidation, replayed message bytes, parsed-response reuse, and reissuing only an unfinished request. Saving before a request, after receiving raw output, after parsing, and after executing evidence must stay at the same boundaries. An abstract port must not batch those writes until the end.

Keep maximum evidence rounds, request deduplication, allowed-ref validation, the final completion marker, chunk-local new-issue limits, and “requery inherited evidence before citing it” arbitration behavior. A vector retriever may later implement the query contract, but retrieved text alone must not become validated kinship evidence without the existing source references and validation.

## Slices and acceptance

1. Extract pure identity/conflict types and functions; remove their old private import paths in all callers/tests.
2. Add the trace adapter, then move `_ActionLoop` with unchanged trace shape and operation IDs.
3. Move the arbiter and trim `review_loop.py` to chunk review.

Use `tests/test_review_agent.py` replay cases: finished and fallback traces, in-flight turns, already-parsed final output, evidence reexecution without another model request, reduced evidence limits, unknown refs and unresolved arbitration. Add an in-memory trace-port fixture and compare every durable snapshot and model message against the current behavior.

Extend architecture tests to reject concrete `review.run_store` imports from agents and recursively inspect any new agent packages. Keep the declared pure review exports explicit. Run Review, routing/resume, usage and architecture tests; run the full suite when wiring proposal 01.
