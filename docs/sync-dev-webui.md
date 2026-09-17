# Notes on syncing `dev` into the WebUI layout

[简体中文](zh/sync-dev-webui.md)

## Baseline and principles

This port keeps the architecture from `webui@ff65cddf600c1c0cbdc6023625a820361644ad9b` and aligns domain source and tests with `dev@851159271e3f255ebafc0d67e26818c45209a5c8`. React/Vite, FastAPI, Arq, Redis, PostgreSQL, and the core/CLI package split remain. Domain capabilities were ported module by module; the branches were not merged wholesale.

Deployment uses a fresh database schema. There is no automatic migration for legacy Web data, strategies, or projects. Create a new project after changing the source file or target language.

## Capability map

| Capability | Where it lives / how to verify |
|---|---|
| Languages, metadata, prompts | core `i18n`; language detection, variants, same-language rejection, 54 packaged resource checks. |
| Providers and model routing | core `llm`; connections/models/tiers/operation routes, retries, budgets, fallbacks, usage tests. |
| Translation, polishing, context, glossary | core domain services; token batching, same-session polish, pre-polish text, glossary checkpoints, title and resume tests. |
| EPUB / HTML / DOCX / PDF | dedicated ingest/assemble modules; styles, tables, annotations, ruby, resources/navigation, PDF backend/default export tests. |
| Review / Autofix | core `review` and domain services; completed reuse, interrupted resume, fingerprint changes, shadow revision, publish idempotency, human-edit protection. |
| SRT | core `srt`; timeline, batch cache, pause usage, resume without double billing, preserving manual subtitle edits. |
| Web PostgreSQL | storage-port injection; transaction rollback, project locks, MVCC snapshots, full workflows, and no local state-copy tests. |
| Web execution and export | separate workflow/export queues; async parse, model config, review history, subtitle editing, human proofreading, export download; durable job identity and an independent recovery loop after abnormal exits. |
| CLI package | `packages/cli/wenyi_cli`; `wenyi` and `python -m wenyi_cli`, with core independent from terminal modules. |

Standalone consistency QA, back-translation sampling, the old severe-issue repair option, and the old model-tier/character-budget fields were removed. Punctuation normalization lives under output settings and only affects export copies.

## Review documentation corrections

Older usage docs described review as “always a full rerun that creates a new directory” and “read-only by default”. Current source behavior is:

- Matching content, review config/route, and glossary fingerprints reuse a completed result or resume the same interrupted review.
- `pipeline.review_autofix` defaults to `true`; set `false` or use `--no-autofix` to keep suggestions only.
- Internal shadow revision stays separate from formal publication. Autofix uses a recoverable publish index and text hashes to protect human edits.

English and Chinese README, usage, configuration, and pipeline docs were updated for these behaviors.

## Verification record and limits

Final full test run: **Python 3.12: 878 passed, 1 skipped; Python 3.10: 875 passed, 4 skipped**, each with 49 subtests passed. Ruff and `git diff --check` passed. core/CLI/API sdist and wheel builds passed, and the core wheel was checked for all **54** language, prompt, and export resources. Git-ignore exceptions were added for packaged resource directories so `data/` and `*.txt` rules do not exclude release assets.

Real PostgreSQL integration tests cover the shared storage contract, rollback, locks, snapshots, and full book/subtitle flows in `apps/api/tests/test_storage_pg_integration.py` (requires `WENYI_TEST_DATABASE_URL`). Frontend type/build/browser tests and API task tests belong to their own suites and CI; treat delivery notes as the source of final run results.

Tests use controllable models and PDF-service doubles and do not consume real model credentials. This sync does not claim a before/after quality comparison on real models or long novels, and offline passes are not the same as a translation-quality conclusion. Docker images and external MinerU/BabelDOC services still need deployment acceptance where those runtimes are available.

For the full acceptance scope, concurrency boundaries, and external verification limits, see the [validation record](validation.md).
