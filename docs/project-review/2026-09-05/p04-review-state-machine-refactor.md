# P04 · Separate the Review state machine and strengthen service contracts

[Index](README.md) · [简体中文](../../zh/project-review/2026-09-05/p04-review-state-machine-refactor.md)

Type: proposed roadmap, not implemented; dependencies and order in index · 2026-09-05

## Baseline and evidence

Preserve the tested Orchestrator façade. Complexity is now inside domain services: this revision has 1,833 lines in review_workflow, 898 in review_autofix, and 968 in review_loop. `run_session()` combines cache/checkpoint handling, loops, overlays, termination, results, and accounting. Size alone is not a defect; intersecting state rules make recovery harder to review.

## Goal and implementation

Independently verify stop/failure/resume transitions while initially preserving prompts, models, ordering, publication, and accounting. Type session state, round results, checkpoints, and publication plans at service boundaries. Extract pure transitions and termination decisions; keep files/events/usage in adapters, model work in agents, and formal publishing separate from shadow review. Split incrementally by cache identity, round execution, recovery, and result assembly.

## Acceptance

Compare identical FakeClient traces before/after: issues/changes, formal targets, semantic event order, and usage, ignoring timestamps. Cover clean confirmations, Fix limits, no progress, cycles, unresolved arbitration, singleton failures, and interruption. Run the full suite and architecture/Orchestrator contracts; retain dependency direction and domain-owned concurrency.

## Sequencing and tradeoff

Fix F01/F05 first and use corrected behavior as the refactor baseline. Estimate 5–10 days in small PRs after stability and quality infrastructure. This refactor does not change review-quality policy; prompt or termination-policy changes require P02 evaluation.

## Repository evidence

- [trans_novel/pipeline/review_workflow.py:690](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/review_workflow.py#L690)
- [trans_novel/pipeline/review_autofix.py:111](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/review_autofix.py#L111)
- [trans_novel/agents/review_loop.py:1](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/agents/review_loop.py#L1)
- [trans_novel/pipeline/orchestrator.py:186](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/orchestrator.py#L186)
- [trans_novel/review/run_store.py:29](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/review/run_store.py#L29)
- [tests/test_architecture_boundaries.py:1](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/tests/test_architecture_boundaries.py#L1)
