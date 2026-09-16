# F09 · Concurrent subtitle runs lack a shared run lock

[Index](README.md) · [简体中文](../../zh/project-review/2026-09-05/f09-srt-run-lock.md)

Priority：**P1** · Type: reproduced defect; not fixed · 2026-09-05

## Finding and trigger

A deterministic low-level race is reproduced. Two callers/processes translating the same SRT may both validate identity and mutate the same cues, batches, manifest, and usage. SRT locks event appends, but not the translation run. Fixed `.tmp` names additionally race during replacement.

## Evidence and limits

`translate_srt()` has no run-lock scope. Probe F09 uses independent store instances and a barrier to force two completed writes to the same temporary file before replacement; one gets `FileNotFoundError`. This is a two-writer storage counterexample, not an end-to-end multiprocess CLI stress test.

## Proposed change

Hold an SRT-owned cross-process run lock across identity validation, initialization, recovery, translation, and final state publication. Keep different subtitles independent. If independent exports are added, use short state locks and snapshots. Unique temporary names alone do not protect read/modify/write operations or usage aggregation.

## Acceptance and delivery

An offline two-process same-source test must wait or report busy, without duplicate requests, lost targets/usage, or damaged JSON. Re-read state after waiting, release locks on exceptions, and test independent sources plus Linux/Windows implementations. Estimate 2–3 days. Keep the lightweight SRT route outside the book Orchestrator; same-source serialization is intentional.

## Source and test locations

- [trans_novel/srt/store.py:59](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/srt/store.py#L59)
- [trans_novel/srt/store.py:287](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/srt/store.py#L287)
- [trans_novel/srt/translate.py:151](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/srt/translate.py#L151)
- [trans_novel/pipeline/runstore.py:65](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/runstore.py#L65)

Reproduce using the shared script in the index; inspect `F09` output. Estimates assume one contributor and exclude external-service or real translation-quality evaluations.
