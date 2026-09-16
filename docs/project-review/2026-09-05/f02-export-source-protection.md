# F02 · An explicit export path can overwrite the source before validation fails

[Index](README.md) · [简体中文](../../zh/project-review/2026-09-05/f02-export-source-protection.md)

Priority：**P1** · Type: reproduced defect; not fixed · 2026-09-05

## Finding and trigger

Reproduced for TXT: when `--out` names the source, the writer overwrites it before the final source-hash check raises an error. The run fails after the original bytes are lost, and source identity no longer matches saved state. SRT also lacks an input/output alias check; other formats were not individually exercised.

## Evidence and reproduction

`assemble()` dispatches without rejecting identical paths; the text writer opens the destination with `w`. Probe F02 calls the real snapshot export service and observes both `ValueError` and changed source bytes. Post-export validation detects damage too late.

## Proposed change

Resolve all enabled output paths before writing and reject collisions with the source or critical state files. Include normalized paths, symlinks, and `samefile` checks for existing hard links. Render to temporary artifacts, validate, then publish. Define a separate publication boundary for HTML assets and other multi-file outputs.

## Acceptance

Input aliases must fail before any write. Check unchanged source/state bytes, valid explicit/default destinations, mono/bilingual output, and preservation of existing artifacts on writer failure. Cover TXT, EPUB, DOCX, SRT, and concurrent snapshot exports.

## Delivery and tradeoff

Ship path protection first, estimated 1–2 days. Atomic artifact publication can follow in a separate 2–4 day PR; source protection need not wait. Temporary artifacts increase peak disk use, and multi-file output needs explicit handling.

## Source and test locations

- [trans_novel/assemble/writer.py:56](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/assemble/writer.py#L56)
- [trans_novel/assemble/text_writer.py:44](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/assemble/text_writer.py#L44)
- [trans_novel/pipeline/finalization.py:158](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/finalization.py#L158)
- [trans_novel/assemble/srt_writer.py:10](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/assemble/srt_writer.py#L10)

Reproduce using the shared script in the index; inspect `F02` output. Estimates assume one contributor and exclude external-service or real translation-quality evaluations.
