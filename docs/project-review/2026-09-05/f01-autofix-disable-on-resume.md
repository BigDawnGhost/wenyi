# F01 · Pending Autofix publication ignores the disabled setting

[Index](README.md) · [简体中文](../../zh/project-review/2026-09-05/f01-autofix-disable-on-resume.md)

Priority：**P1** · Type: reproduced defect; not fixed · 2026-09-05

## Finding and trigger

Reproduced. Enable Autofix, interrupt after its index becomes `applying`, then resume with `review --no-autofix` or `pipeline.review_autofix: false`. Pending changes can still reach formal chapters. This requires a pending publication index; a fresh read-only Review follows a different path.

## Evidence

`_run_review_locked()` calls `resume_pending()` before checking the setting. `resume_pending()` directly invokes `_apply_index()` for an applying index. The existing pending-index test covers enabled recovery, but not disabling publication during recovery.

## Reproduction

Probe F01 uses the real planning path, interrupts at publication, and resumes with a disabled setting. The formal target changes from 旧译 to 新译. All fixtures are temporary and all clients are offline.

## Proposed change

Gate publication recovery in routing and at the service entry. Keep the pending index when disabled and expose its unfinished status. Do not delete the index, roll back previously published chapters, or publish remaining chapters automatically.

## Acceptance

Test interruptions before any publication and after partial publication with both settings. Disabled recovery must preserve formal chapter bytes; enabled recovery must finish idempotently without duplicate model calls or usage. Run Autofix, Orchestrator contract, and architecture tests.

## Delivery and tradeoff

One focused PR; estimate 1–2 working days including regression tests. Preserve current defaults and shadow-review semantics. Previously published edits remain present and need accurate progress reporting.

## Source and test locations

- [trans_novel/pipeline/orchestrator.py:186](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/orchestrator.py#L186)
- [trans_novel/pipeline/review_autofix.py:76](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/review_autofix.py#L76)
- [tests/test_review_autofix.py:318](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/tests/test_review_autofix.py#L318)

Reproduce using the shared script in the index; inspect `F01` output. Estimates assume one contributor and exclude external-service or real translation-quality evaluations.
