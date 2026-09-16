# F03 · Overlapping tail windows make subtitle output order-dependent

[Index](README.md) · [简体中文](../../zh/project-review/2026-09-05/f03-srt-window-determinism.md)

Priority：**P1** · Type: reproduced defect; not fixed · 2026-09-05

## Finding and trigger

Reproduced with 20 cues. Jobs starting at positions 0 and 10 both have `is_last=True`. The first commits cues 1–20; the second commits 16–20. Different translations for those shared cues make the result depend on completion order.

## Evidence

Job generation continues after a window already reaches the tail. Results merge immediately through `as_completed()`, while cached batches replay in start order. A no-call resume can therefore produce a different tail from the first run.

## Reproduction

Probe F03 feeds A/B results into the real merge function in both possible orders. Cue 16 becomes B or A. This is a deterministic counterexample, not a probabilistic scheduling test.

## Proposed change

Assign each cue exactly one committing window, separating context from the active output range. Stop creating redundant tail jobs. Persist a window-plan identity so old caches are not interpreted under new ownership rules. Stable ordering alone should not retain duplicate ownership.

## Acceptance and delivery

Test 1, 10, 15, 16, 19, 20, 21, 25, 26, 30, and 31 cues. Active ranges must cover every cue once. Reversed completion order, cache replay, and interrupted recovery must produce identical text while preserving indices/timestamps. Estimate 1–2 days. Retain overlapping context and explicitly handle old caches without silently replacing confirmed targets.

## Source and test locations

- [trans_novel/srt/translate.py:105](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/srt/translate.py#L105)
- [trans_novel/srt/translate.py:199](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/srt/translate.py#L199)
- [trans_novel/srt/translate.py:247](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/srt/translate.py#L247)
- [tests/test_srt.py:97](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/tests/test_srt.py#L97)

Reproduce using the shared script in the index; inspect `F03` output. Estimates assume one contributor and exclude external-service or real translation-quality evaluations.
