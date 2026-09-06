# F08 · Expected language-detection and configuration errors escape CLI handling

[Index](README.md) · [简体中文](../../zh/project-review/2026-09-05/f08-cli-error-contract.md)

Priority：**P2** · Type: reproduced at the baseline; two symptoms fixed on 2026-09-06 · 2026-09-05

Update 2026-09-06: P10 changes failed language detection to `ValueError` and catches YAML/configuration errors in the shared CLI loader. Both reproduced user-visible symptoms are fixed. The original analysis remains as audit history; broader YAML mapping-shape validation is still a possible follow-up.

## Finding and trigger

Reproduced. Failed automatic language detection raises `RuntimeError`, outside prepare/translate's local catches. Malformed YAML raises an untranslated parser exception. Both enter the unhandled CLI error path instead of the repository's concise expected-error contract.

## Evidence

Probe F08 runs prepare with the default empty fake response and receives the original `RuntimeError` from CliRunner. A `language: [` configuration passed to status yields `ParserError`. This verifies escaping exceptions, not every platform's terminal traceback presentation.

## Proposed change

Introduce typed user-actionable errors at configuration, detection, and provider boundaries, with shared CLI presentation. Retain YAML filename/line/column information. Do not swallow programming errors with a blanket top-level catch; offer explicit diagnostic detail separately.

## Related configuration work

Validate mappings before `.get()`, reject unknown critical settings, constrain positive batch sizes/timeouts, and validate enums. TierConfig already forbids extra fields. Handle stricter configuration validation in a separate commit with templates, examples, bilingual docs, and tests aligned.

## Acceptance and delivery

Test processing and credential-exempt commands with malformed YAML, list roots, null sections, failed detection, and mocked permanent provider errors. Require nonzero exits and concise nonsensitive output while retaining diagnosable programming errors. Estimate 1–2 days for errors plus 1–2 for validation; strict rules need migration messages for formerly ignored typos.

## Source and test locations

- [trans_novel/pipeline/preparation.py:186](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/pipeline/preparation.py#L186)
- [trans_novel/cli.py:460](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/cli.py#L460)
- [trans_novel/cli.py:488](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/cli.py#L488)
- [trans_novel/config.py:198](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/config.py#L198)
- [trans_novel/config.py:205](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/trans_novel/config.py#L205)

Reproduce using the shared script in the index; inspect `F08` output. Estimates assume one contributor and exclude external-service or real translation-quality evaluations.
