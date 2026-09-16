# P03 · Add request budgets, concurrency limits, and cancellation

[Index](README.md) · [简体中文](../../zh/project-review/2026-09-05/p03-request-budgets-and-backpressure.md)

Type: proposed roadmap, not implemented; dependencies and order in index · 2026-09-05

## Baseline and evidence

Keep shared retry classification, usage statistics, stage metrics, and Review round limits. SRT fixes concurrency at 100 and wraps calls in three catch-all attempts, potentially repeating already-exhausted provider retries. Domain pools have no shared request quota.

## Goal and implementation

Make resource use understandable and controllable. Bound calls/tokens first; monetary estimates require an explicit price table. Keep transport retry solely in `llm/retrying.py`, and retry only recoverable output-protocol failures at the domain layer. Configure SRT batch/overlap/concurrency with validation and state identity. Add provider-neutral shared permits and budgets used by domain services, keeping thread pools out of Orchestrator. Cancellation stops new submissions, settles in-flight work, and persists checkpoints.

## Budget semantics

Track P06 embeddings/reranking, P08 local style analysis, and P09 cross-chapter evidence as distinct operations/stages. Record local embedding time/resources and actual or explicitly estimated remote usage, without treating embeddings as chat output tokens. P07 memory hits are reuse metrics, not invented model calls; contextual validation records its actual requests separately.

Expose stage estimates, usage, and remaining allowance. Reserve budget before concurrent submissions and reconcile actual usage afterward. Define possible in-flight overshoot explicitly. Separate rate limits, timeouts, and permanent failures in progress reporting; character/sample estimates are not guarantees.

## Acceptance and sequencing

Mock 401/429, timeouts, malformed outputs, concurrency, and cancellation. Verify bounded retries, no new submissions after exhaustion, no false done status, and no duplicate accounting on resume. Synchronize config models/templates/examples, CLI, and bilingual docs. Depend on F03/F04/F09 and P01 accounting. Estimate 5–8 days across retry/configuration, quota, and budget/cancellation increments. Lower concurrency may trade speed for stability; no unverified current pricing claims are needed.

## Repository evidence

- [trans_novel/srt/translate.py:22](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/srt/translate.py#L22)
- [trans_novel/srt/translate.py:73](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/srt/translate.py#L73)
- [trans_novel/llm/retrying.py:271](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/llm/retrying.py#L271)
- [trans_novel/llm/usage.py:1](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/llm/usage.py#L1)
- [trans_novel/pipeline/metrics.py:118](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/metrics.py#L118)
- [trans_novel/config.py:126](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/config.py#L126)
