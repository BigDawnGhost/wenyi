# WebUI / `dev` sync validation

[简体中文](zh/validation.md)

## Review workbench update · 2026-09-17

Recorded for implementation commit `444bd3c` (not a new validation run for later documentation edits):

| Check | Result |
|---|---|
| Python 3.12 full suite, with isolated PostgreSQL and Redis | 955 passed, 3 skipped, 49 subtests passed |
| Full Playwright suite | 59 passed |
| Follow-up review and localization checks after final publication-display adjustment | 15 passed |
| TypeScript, Vite build, Ruff checks/format, Prettier, and `git diff --check` | Passed |

Coverage includes task-scoped progress, ticking/frozen timers, history isolation, pending
versus published revisions, final publication text, sparse paragraph IDs, and Chinese mobile
layout. The remaining notices were Arq's Redis-close deprecation and Vite's main-chunk size
warning. Python 3.10 and package builds were not repeated for this interface update; no
real model or PDF service was called. Temporary test services were removed afterward.

The sections below preserve the **original WebUI migration** evidence and environment
limitations. Their test counts and screenshots describe that earlier implementation, not
the current interface; see the [interface guide](web-interface.md) for current behavior.

## Scope

The implementation keeps the React/Vite, FastAPI, Arq/Redis, PostgreSQL, and core/CLI package architecture from `webui@ff65cddf`, and ports domain behavior from `dev@85115927`. It uses a fresh Web database schema; legacy projects were not migrated.

## Automated checks

Final verification results:

| Check | Result |
|---|---|
| Python 3.12 full suite (including real PDF font tests) | 878 passed, 1 skipped, 49 subtests passed |
| Python 3.10 full suite (base dependencies) | 875 passed, 4 skipped, 49 subtests passed |
| PostgreSQL / API / worker tests | 82 passed, included in both Python full runs |
| Playwright browser regressions | 7 passed |
| TypeScript / Vite production build | Passed |
| Ruff / `git diff --check` | Passed |
| core / CLI / API sdist and wheel | All builds passed |

The only Python 3.12 skip is a Weaver PDF sample that lives outside the repository. Python 3.10 additionally skips 3 font tests that need optional fpdf2; those three already passed on Python 3.12. Both Python runs only showed dependency deprecation notices from the FastAPI test client and Redis close APIs used by Arq.

The run included:

- Python 3.10 and 3.12: core, CLI, and real PostgreSQL storage/API/worker tests.
- Real Redis: durable job enqueueing, Arq consumption, and database state write-back.
- TypeScript, production build, and 7 Playwright browser tests.
- Separate core, CLI, and API package builds; core includes all 54 language/prompt/export resources, and API includes the database schema and background recovery modules.
- Optional PDF rendering checks: font embedding, actual page display, and text, not only output-file existence.
- Ruff and Git whitespace checks.

## Real browser flows

The browser talked to a real FastAPI, PostgreSQL, Redis, and independent Arq workflow/export workers with token auth enabled. Only the model client was replaced with a controllable offline client; API responses were not mocked.

| Flow | Result |
|---|---|
| EPUB | Create, upload, async parse, preview, project-config validate/save, model-config check, prepare, translate, polish, default Review/Autofix, and human edits all completed. |
| Review again | A new run started after human edits; the previous run history remained readable. |
| DOCX / EPUB export | Browser downloads succeeded; export content included human edits; the durable export job run ID matched the queue ID and status was done. |
| SRT | Upload, parse, translate, timeline display, stats, human edits, and bilingual export completed; exports kept human translations, source text, and original timestamps. |
| Auth | HTTP downloads required a Bearer token; WebSocket sent project data only after the first packet validated the same token. |

These browser flows produced no JavaScript exceptions or API 500 responses. Final page screenshots:

- [Whole-book review](images/web-review.png)
- [Subtitle compare and edit](images/web-subtitles.png)

## State and concurrency checks

- File storage and PostgreSQL share the domain contract; Web does not create JSON/SQLite state copies.
- Parse results are reused only after source SHA-256 and parse-config validation.
- Completed reviews are reused; interrupted reviews and Autofix continue from the original checkpoint; human edits are not overwritten by older publish records.
- After human edits, historical “reviewed” status and stale issues are no longer shown; a new review adopts the new result when it finishes.
- Even without progress callbacks while a model request is waiting, the pause monitor can cancel and persist `paused`.
- Stale queue deliveries or late exceptions do not execute over or overwrite newer work.
- Queue submit failures, brief database write-lock contention, and abnormal worker exits leave durable error or recovery state.
- Exports use a short consistent snapshot and do not hold a database write transaction across model calls or rendering.
- Exports have an independent queue, run identity, file directory, and lock; they can run beside translation, and abnormal exits do not leave them pending forever.
- Submitted tasks use the saved configuration snapshot; later project-config edits do not change already submitted tasks.

## External verification not run

- No real model credentials were used for long-novel quality, cost, or throughput comparison; offline passes are not a translation-quality conclusion.
- Real MinerU was not called and a BabelDOC bridge was not deployed; parse/backfill/failure paths were covered with offline service doubles.
- This machine lacked the Docker Compose plugin and a usable Docker socket, so containers were not actually built and started. Compose/Dockerfile updates were reviewed; CI configures PostgreSQL/Redis, frontend tests, and optional PDF rendering dependencies.
- The Weaver sample PDF is not shipped in the repository, so the corresponding integration sample test is skipped when unavailable.
