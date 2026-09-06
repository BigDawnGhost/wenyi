# F04 · Failed and empty subtitle translations are marked complete

[Index](README.md) · [简体中文](../../zh/project-review/2026-09-05/f04-srt-failure-state.md)

Priority：**P1** · Type: reproduced defect; not fixed · 2026-09-05

## Finding and trigger

Two cases are reproduced: failed batch and singleton calls save source text as `done`, preventing retry on resume; valid JSON containing an empty translation also becomes `done` and may export an empty cue. Completion reporting counts these entries.

## Evidence and reproduction

The singleton helper catches every exception and returns source text. Batch parsing accepts empty strings, the missing check tests key presence only, and final persistence assigns `STATUS_DONE`. Probe F04 observes source-as-done followed by zero calls with a healthy client, plus empty-as-done.

## Proposed change

Represent success and failure explicitly. An export copy may fall back to source text, but state must remain failed/pending. Validate cue keys and nonempty translations; resume only verified successful entries and ignore invalid cached results.

## Acceptance

Exercise permanent failures, exhausted retries, malformed JSON, missing keys, empty and whitespace targets. A later successful run must translate only missing items. Do not infer failure merely from target/source equality: punctuation and legitimate unchanged text need explicit outcomes. Report partial failure and separate success/failure counts in CLI.

## Delivery and tradeoff

Estimate 2–3 days. Coordinate cache handling with F03, keeping the commits separable. Historical source-as-target entries cannot reliably be classified after the fact; provide explicit retry ranges or document that limitation instead of retranslating all confirmed cues.

## Source and test locations

- [trans_novel/srt/translate.py:37](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/srt/translate.py#L37)
- [trans_novel/srt/translate.py:89](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/srt/translate.py#L89)
- [trans_novel/srt/translate.py:273](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/srt/translate.py#L273)
- [trans_novel/srt/translate.py:301](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/srt/translate.py#L301)
- [trans_novel/srt/store.py:210](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/srt/store.py#L210)

Reproduce using the shared script in the index; inspect `F04` output. Estimates assume one contributor and exclude external-service or real translation-quality evaluations.
