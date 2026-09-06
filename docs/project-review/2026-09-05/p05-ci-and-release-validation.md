# P05 · Validate code, document artifacts, and release binaries in CI

[Index](README.md) · [简体中文](../../zh/project-review/2026-09-05/p05-ci-and-release-validation.md)

Type: proposed roadmap, not implemented; dependencies and order in index · 2026-09-05

## Baseline and evidence

Tests uses locked dependencies on Ubuntu with Python 3.10/3.12; local pre-commit has Ruff. CI omits Ruff checks and PR tests target dev only. Builds span platforms, but install with unlocked `pip install . pyinstaller`; binary smoke tests run only help/version. These are configuration observations, not proven build failures. Remote branch protection was not inspected.

## Goal and implementation

Gate deliverable behavior: lint, offline workflows, format preservation, and packaged execution. Add Ruff check/format; decide main-PR coverage according to actual branch policy. Define reproducible application/build dependency constraints. Run packaged binaries with temporary config and fake provider through minimal prepare/translate/export, retaining focused platform-specific locking/smoke tests.

## Artifact and documentation checks

Use synthetic EPUB/DOCX/SRT round trips for anchors/notes/TOC, indices/timestamps, tables/run styles, mono/bilingual, and explicit destinations. Give optional PDF engines dedicated dependency-equipped jobs; keep real bridge/MinerU integration separate from offline CI. Check local documentation links and bilingual configuration/behavior parity, including the cache-documentation drift in F05.

## Acceptance and sequencing

Gates must catch missing dependencies/resources, lost format information, and broken documentation links without real API keys. Keep existing checksums, architecture checks, and macOS signature verification. Estimate 1–2 days for lint/links, then 3–5 for binary/format coverage, before P04. Control CI runtime incrementally. GUI/Web,  and more formats should receive separate plans after existing quality/state guarantees stabilize.

## Repository evidence

- [.github/workflows/tests.yml:1](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/.github/workflows/tests.yml#L1)
- [.github/workflows/build.yml:1](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/.github/workflows/build.yml#L1)
- [.pre-commit-config.yaml:1](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/.pre-commit-config.yaml#L1)
- [pyproject.toml:1](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/pyproject.toml#L1)
- [tests/test_bilingual.py:1](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/tests/test_bilingual.py#L1)
- [tests/test_docx.py:1](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/tests/test_docx.py#L1)
- [tests/test_pdf_support.py:1](https://github.com/BigDawnGhost/wenyi/blob/15943b97592dc38ef9712412b6fd83a41951e1ca/tests/test_pdf_support.py#L1)


Follow-up on 2026-09-06: multilingual work is now tracked in [P10](p10-multilingual-internationalization.md). Its first version adds package-resource collection and a `languages` binary smoke command; the remaining release improvements are still proposed.
