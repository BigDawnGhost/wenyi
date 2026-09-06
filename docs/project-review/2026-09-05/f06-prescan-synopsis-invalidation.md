# F06 · Recovering a chapter digest leaves the book synopsis stale

[Index](README.md) · [简体中文](../../zh/project-review/2026-09-05/f06-prescan-synopsis-invalidation.md)

Priority：**P2** · Type: reproduced defect; not fixed · 2026-09-05

## Finding and trigger

Reproduced. When one chapter digest fails to an empty string and another succeeds, a partial book synopsis is saved. Resume repairs the missing digest but keeps the existing synopsis, so later translation still receives incomplete whole-book context.

## Evidence and reproduction

Missing digests are retried, but synopsis generation checks only whether its stored string is empty. Probe F06 produces `[chapter 0, empty]`, repairs chapter 1, and observes only one synthesis call with the old `chapter 0|` synopsis remaining.

## Proposed change

Store the ordered digest-input fingerprint, coverage, and completion status with each synopsis. Recompute on dependency changes. Explicitly choose whether incomplete prescan blocks translation or permits a documented degraded run. If translation already started, freeze and expose context versions at run boundaries instead of silently changing the supposedly stable prefix.

## Acceptance and delivery

Test partial/all failures followed by recovery, legacy missing fingerprints, empty chapters, changed digests, and unchanged resume. Verify actual prompt context and recompute only invalidated work. Estimate 1–3 days. A necessary extra synthesis call improves available context; leave completed targets intact unless a separately evaluated retranslation is requested.

## Source and test locations

- [trans_novel/pipeline/preparation.py:301](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/preparation.py#L301)
- [trans_novel/pipeline/preparation.py:360](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/preparation.py#L360)
- [trans_novel/agents/base.py:63](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/agents/base.py#L63)
- [tests/test_orchestrator.py:1007](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/tests/test_orchestrator.py#L1007)

Reproduce using the shared script in the index; inspect `F06` output. Estimates assume one contributor and exclude external-service or real translation-quality evaluations.
