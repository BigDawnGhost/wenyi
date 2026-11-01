# Component decoupling and directory plan

[简体中文](../zh/design/component-decoupling.md) · [Implemented architecture](../architecture.md)

## Status and decision

**Implemented: F1–F7 and S0–S5.** The initial review used source revision `80d448f` and added documentation only; this document now records the completed extraction. Execution lifecycle, explicit backend dependencies, input preparation and book understanding, usage ownership, UI public contracts, shared verification, and CLI file migration have separate owners. The temporary DAL, client-type and host-fixture forwarding paths are removed. See the [current module map](../architecture.md), [CLI workflow](../cli.md#common-commands), and verification record below.

Keep the existing modular monorepo and deployment boundaries. Decouple responsibilities **inside** packages before moving directories. Preserve public entry points during each migration, and remove compatibility forwarding only after all callers and tests have migrated.

| Approach | Trade-off | Decision |
|---|---|---|
| Reorganize files without changing dependencies | Smaller-looking files, but hidden dependencies and duplicated rules remain | Reject as the primary task |
| Extract use cases and consumer-sized ports incrementally | Reviewable changes; temporary forwarding paths need an explicit removal criterion | **Recommended** |
| Rebuild around new domain packages or microservices | Large import, packaging and operational changes without an identified deployment need | Out of scope |

No database/state migration, new runtime service, new workspace package, prompt change, or UI redesign is required by this plan.

## 1. Boundaries already worth preserving

- Core owns translation behavior; CLI, shared Backend and platform adapters depend on it, not the reverse.
- The [Orchestrator](../../packages/core/wenyi_core/pipeline/orchestrator.py#L30) already delegates to services. Its stage routing and lock scopes are legitimate responsibilities, not another reason to split it.
- [BackendContext](../../packages/backend/wenyi_backend/context.py#L89) supplies per-application adapters. Its ContextVar is request/task scoped, **not** a process-wide Web/Desktop selector. Retain context propagation to threads and callbacks.
- Web owns PostgreSQL and Redis/Arq; Desktop owns its workspace, SQLite catalog, credentials and local runner. PostgreSQL adapters must remain in `apps/api`, never move into Core.
- Shared UI already uses feature directories and injected host capabilities. Keep routes, shell, primitives and features separate; do not move every page merely to rename its parent directory.
- `ArtifactStorage`, review evidence/models, markup, DOCX style policy, language policies and independent SRT execution already have useful boundaries. Extend these rather than introduce parallel abstractions.
- [Python boundary tests](../../packages/backend/tests/test_backend_boundaries.py#L11), [frontend boundary tests](../../packages/ui/tests/platform-boundaries.test.mjs#L31) and [clean-install package checks](../../scripts/check_backend_packages.py#L19) already protect platform isolation. They need targeted extensions, not replacement.

## 2. Findings and intended seams

Priorities below describe migration order, not confirmed production failures. File length, a composition object, or a startup singleton alone is not evidence of a defect.

### F1 — Task execution and scheduler entry points share one lifecycle owner

**Original finding:** `workers/tasks.py` combined scheduler context, configuration, durable identity checks, cancellation, domain operations and telemetry/storage cleanup. Web and Desktop called the same worker-shaped entry points.

**Risk:** changing scheduler invocation or terminal-state handling touches the business execution path. Late completion and duplicate delivery depend on job identity and lock checks embedded in that path.

**Implemented P1 seam:** [ExecutionRequest and dependencies](../../packages/backend/wenyi_backend/execution/contracts.py#L20) are explicit inputs to the [runner](../../packages/backend/wenyi_backend/execution/runner.py#L63), which owns admission rechecks, interruption, cleanup and terminal-state handling. [Operations](../../packages/backend/wenyi_backend/execution/operations.py#L108) own preview/book/subtitle work; `workers/tasks.py` retains the existing task names and signatures as thin wrappers. Web Arq and Desktop scheduling remain platform adapters. CLI continues to call Core directly, not this HTTP application layer.

The runner owns the long book lock from durable identity rechecks through recovery and domain execution. Successful catalog status writes follow live-statistics cleanup and lock exit, preserving the existing execution contract rather than the original plan's proposed wider lock. Resource cleanup is registered at acquisition, including startup failures; async cancellation drains synchronous work before returning. Read routes also close per-operation storage on success and error.

The separate export coordinator retains the same `export_lock` across claim, render, publish and terminal status. Book rendering uses the consistent Core snapshot and assemble lock; SRT retains its independent state-lock snapshot path. Rendering does not update catalog status, and publication does not invoke a writer. Existing export plan/path/response helpers are reused rather than creating a second export policy.

### F2 — Scoped context is correct, but dynamic forwarding hides service dependencies

**Original finding:** the shared DAL forwarded arbitrary attributes through the current repository, and the PostgreSQL repository forwarded again to its platform DAL. The aggregate repository combined unrelated consumer capabilities.

**Risk:** a service can acquire additional persistence capabilities without changing its declared inputs; dynamic forwarding also weakens static checking.

**Implemented:** project/job protocols in `ports/` are supplied explicitly to execution, project, admission and source-view services. Read projections have a read-only contract. [Execution bindings](../../packages/backend/wenyi_backend/execution/bindings.py#L63) and [HTTP bindings](../../packages/backend/wenyi_backend/routers/dependencies.py#L19) compose dependencies from `BackendContext`; business services do not discover them through ambient context. Routes use the typed repository directly for catalog operations. The shared dynamic DAL is deleted, and [PostgresRepository](../../apps/api/wenyi_api/adapters.py#L28) declares its supported methods explicitly instead of using `__getattr__`.

Do **not** split logically coupled admission and compensation into unrelated repository calls. Existing `PostgresRepository` and `LocalBackend` implement multiple small protocols; SQL stays in platform adapters. General admission and compensation keep their existing platform boundaries, not a new shared cross-store transaction. Two separately tested safety fixes accompany this extraction: pause atomically checks current run identity/status in the catalog, and source replacement commits cache invalidation before publishing a source reference. An uncertain publication preserves the candidate file; automatic orphan cleanup is not introduced.

### F3 — Input preparation mixes parsing, initialization and model-assisted understanding

**Original finding:** preparation and Backend preview duplicated parser arguments, source checks and parsed-document cache rules, while preparation also coordinated concurrent book understanding.

**Risk:** parser options, cache identity and source-change handling have several callers that must evolve together; preview should not need a model-equipped runtime.

**Implemented:** the shared input-preparation API owns parsing options, source identity checks and validated cached documents, using injected artifact access where caching is supported. Backend retains HTTP preview projection; `PreparationService` retains initialization, stage coordination and manifest commit ownership. Retired parser forwarding methods are removed after caller migration.

The caller-specific cache policy is preserved, including PDF's known state path and pre-conversion initialization marker versus title-based discovery for other formats. [BookUnderstandingService](../../packages/core/wenyi_core/pipeline/book_understanding.py#L34) separately owns digest/synopsis coordination, stable-order concurrency and resumable checkpoints. It receives configuration, agents and the usage coordinator, not the whole Runtime; source hashes, cache versions and language-policy fingerprints are unchanged.

### F4 — Runtime assembly also owns recoverable usage publication

**Original finding:** Runtime constructed clients and agents while also updating a client checkpoint and publishing both book and Review ledgers. Passing it to extracted services exposed more than their actual accounting dependency.

**Risk:** moving apparently small accounting code can change checkpoint timing, recovery or once-only charging. Passing the entire runtime into every extracted service would preserve the coupling.

**Implemented:** [UsageCoordinator](../../packages/core/wenyi_core/pipeline/runtime_usage.py#L28) owns the single client checkpoint and depends on explicit usage-source, book-ledger and Review-ledger protocols. All book workflow consumers share that owner. Recovery precedes reading totals; the durable absolute-total journal precedes the in-memory checkpoint; retry cannot replay a delta. Existing arithmetic, empty Review ledger behavior, event payloads and storage formats are preserved.

No matrix of unused ports is introduced. Language binding and timing remain legitimate Runtime responsibilities; `RunStore` and the aggregate `Storage` contract stay in place. Independent SRT accounting is unchanged.

### F5 — UI contracts expose implementation paths and a type-only dependency cycle

**Original finding:** platform contracts imported API implementation types, while wildcard package exports and host aliases exposed shared UI internals. This was a type-only back edge and public API design issue, not a demonstrated runtime cycle.

**Risk:** moving a private UI module forces host edits, and foundational contracts depend on the client implementation they support. This is a public API design issue, not an invalid host-to-shared dependency.

**Implemented P1 seam:** [API types](../../packages/ui/src/api/types.ts#L1) depend only on the generated schema; the client and [platform contracts](../../packages/ui/src/platform/contracts.ts#L1) consume them. Transport, progress, preferences, activity and optional capabilities are composable interfaces. The single client, pre-mount configuration and `platform()` facade retain their behavior.

[Explicit package exports](../../packages/ui/package.json#L6) cover the app, platform, API/types, HTTP, i18n, document language, styles, approved primitives, settings layout and formatting. Both TypeScript and Vite honor these exports; host `@/` points only at host `src`, and shared UI uses relative imports. [Resolved-import checks](../../packages/ui/tests/public-surface.test.mjs#L14) reject private subpaths, relative escapes and alias bypasses, including deliberately forbidden examples.

The single client now lives in `api/client.ts`. Consumers import types from `api/types.ts`; the old `lib/api.ts` implementation/type-forwarding entry is removed without changing the public `@wenyi/ui/api` export.

Keep query/mutation ownership with its feature. Split endpoint modules only as consumers require it; do not add a second state cache, generic service framework or standalone API-client package.

### F6 — Some shared verification still belongs to one host or repeats contracts

**Original finding:** Desktop imported Web's fixtures and shell tests; bundle checks repeated route chunk names, and schema generation needed a live API without a CI freshness check.

**Risk:** shared test changes are owned by Web accidentally; new routes or schema changes may not update every duplicated test/type input. These are guardrail gaps, not proof that current types or routes are broken.

**Implemented guardrails:** [offline schema export](../../scripts/export_openapi.py#L21) uses the shared app factory and unavailable runtime ports without a production lifespan. `pnpm check:schema` generates temporary types and compares them with the committed file; CI runs it before frontend checks. [Route-loader inspection](../../packages/ui/tests/source-contracts.mjs#L43) derives chunks from the manifest, with separate required-URL/redirect assertions and existing audience/shell E2E coverage.

**Implemented test ownership:** all 18 cross-host suites, API fixtures, select helpers and the select harness live in UI test support. Each host explicitly registers suites with its own bootstrap and capability options; no host imports another host's tests. Host fixture forwarders are removed. Boundary checks require both hosts to register every shared suite and forbid native bootstrap in shared support. Native save/drop/credentials/bootstrap and browser behavior remain separate; test support is not a runtime export.

### F7 — CLI migration reaches a private file-store method

**Original finding:** the CLI migration command knew ledger paths, created backups and called `RunStore._write_json`.

**Risk:** a storage refactor can silently break a user-facing migration command.

**Implemented:** [migrate_usage_ledgers](../../packages/core/wenyi_core/storage/file_migrations.py#L13) owns enumeration, validation, exact-byte backups, the run lock and atomic writes through the public file-storage interface. It reuses the pure ledger converter and validates all candidates before changing any ledger. Completed conversions are skipped on retry; an interrupted publication retains the original and its backup. CLI owns argument validation and presentation. No Web/Desktop state is discovered or migrated.

Unrelated CLI command reorganization is outside this plan.

## 3. Target directory map

This is the **implemented selected module map**, not a full tree. Unlisted modules stay where they are; each extracted module has actual consumers.

```text
packages/
  core/wenyi_core/
    pipeline/
      orchestrator.py                  # unchanged facade
      preparation.py                  # initialization and stage coordination
      input_preparation.py            # extracted parsing/cache rules shared with preview
      book_understanding.py           # digest/synopsis concurrency and checkpoints
      runtime.py                      # composition, language binding and timing
      runtime_usage.py                # run-owned usage checkpoint/publication
      runstore.py                     # unchanged file-state compatibility layer
    storage/
      protocol.py                     # retained; consumer-sized protocols as needed
      file.py / sqlite.py             # retained adapters
      file_migrations.py              # public CLI file migration operation
  backend/wenyi_backend/
    application.py / context.py       # retained HTTP and adapter composition
    ports/
      projects.py / jobs.py           # implemented consumer-sized persistence contracts
    execution/
      contracts.py                    # implemented typed run request and dependencies
      runner.py                       # implemented lifecycle and resource ownership
      operations.py                   # implemented preview/book/SRT dispatch into Core
      exporting.py                    # implemented rendering/publication separation
      bindings.py                     # implemented context-to-port composition
    workers/tasks.py                  # implemented stable task entry-point wrappers
    routers/                          # retained HTTP translation layer
      dependencies.py                 # explicit HTTP-to-service dependency binding
    export_plan.py / export_paths.py  # retained, not duplicated
  ui/
    src/
      App.tsx / routes/              # retained shell and routing
      platform.ts                    # implemented stable host entry point
      platform/contracts.ts          # implemented composable host capability contracts
      platform/runtime.ts            # implemented pre-mount configuration and access
      api/types.ts                   # implemented aliases derived from generated schema
      api/client.ts                  # the single client, with no type forwarding
      components/ui/index.ts         # implemented approved host-facing primitives
      features/                      # retained; feature-local queries/components
      i18n/                          # retained
    tests/e2e/
      fixtures.ts                    # shared API fixtures
      shell.ts / ...                 # 18 explicitly registered cross-host suites
      select-helper.ts / select-harness.tsx
  shared-schema/src/api.d.ts          # retained generated contract
apps/
  api/wenyi_api/                      # PostgreSQL, Redis/Arq and Web assembly stay here
  web/src/                           # browser host stays here
  desktop/
    backend/wenyi_desktop/            # catalog, local runner, credentials stay here
    frontend/src/                    # native host and optional UI stay here
    src/                             # Rust IPC/security boundary stays here
```

Logical dependency directions after extraction:

```text
CLI ----------------------------------------> Core facade/services
Web Arq / Desktop runner -> Backend execution -> Core facade/services
HTTP routes -------------> Backend use cases -> explicit platform ports
Web / Desktop UI hosts --> shared UI public entry points
UI features -> API client -> platform contracts -> generated API types
Core services -> Storage/ArtifactStorage ports <- file / SQLite / PostgreSQL adapters
```

Assembly is allowed to know concrete adapters. Business services must not discover adapters by importing platform packages. No shared `utils/`, global service locator, new `common` package or generic event bus is introduced.

## 4. Migration stages and acceptance criteria

Stages are review units, not promises about calendar duration. Do not combine a directory move, a behavior change and a persistence migration in one change.

| Stage | Scope and dependencies | Required acceptance evidence |
|---|---|---|
| S0 — Guardrails (complete) | Schema freshness, resolved-import/route checks, backend service boundaries and package-content checks | Scans assert non-empty input; deliberately forbidden imports are detected; fake-port schema export uses no external services; both host builds precede bundle checks |
| S1 — UI public surface (complete) | F5/F6: public types/exports, all shared fixtures/suites/helpers, host-owned bootstrap | Both hosts typecheck/build; boundary tests and E2E pass; no URL, query-key, DOM/shell, native capability or credential behavior change |
| S2 — Backend execution (complete) | F1/F2: lifecycle runner, consumer ports, explicit application services and platform adapters; stable worker wrappers | Duplicate/stale jobs, pause/cancel, cleanup, compensation, lock contention, duplicate exports and publication/terminal failures; concurrent app/worker isolation and thread/callback propagation; isolated PostgreSQL/Redis and Desktop runner contracts |
| S3 — Input and book understanding (complete) | F3: model-free parser/cache API and separate book-understanding service | Cache hit/miss/config/source mismatch, PDF failure/retry, manifest-last commit, no-model preview, stable ordering and interrupted prescan resume |
| S4 — Accounting and file migration (complete) | F4/F7: one usage checkpoint owner and public local-file migration operation | Crash/retry proves book/Review usage merged once; journal/event failures, backups and atomic publication retries; full Python suite and storage adapters |
| S5 — Remove forwarding (complete) | Retired shared DAL, parser helpers, client type forwarding and host fixture wrappers removed | No callers of retired entry points; docs/tests/import scanners agree; clean-install smoke tests discover actual modules/resources |

Each extraction starts with characterization tests for existing behavior. A genuine bug fix first adds a failing regression case and is separated from behavior-preserving moves. If a stage cannot preserve its contract, stop that stage and revisit its design rather than normalize a changed snapshot.

**Rollback:** keep old public entry points delegating to the new implementation during migration, not two separately evolving implementations. Because the plan does not change durable formats, an extraction can be reverted without a data rollback. Never undo a failed experiment by deleting user state, caches, books or exports.

## 5. Non-negotiable contracts

1. Preserve source hashes, segment/chapter identities, annotation anchors, DOCX styles and `babeldoc_id`; `None` and an intentional empty translation remain distinct.
2. Commit initialization manifest last; retain completed-batch skipping, resumable Review checkpoints and stable original-order merging of concurrent work.
3. Preserve run/state/event/export lock scopes, snapshot consistency and atomic file publication. Slow native or external operations must not hold catalog write transactions.
4. Keep recoverable usage publication and once-only Review deltas; preserve event project/run identity, durable job names/statuses, stale-run rejection and configuration snapshots.
5. Review edits shadow translations only; explicit Autofix still writes a recoverable index before publishing formal targets.
6. SRT stays outside the book Orchestrator, glossary and full-book Review. BabelDOC remains an external AGPL HTTP service, never a Core dependency.
7. Preserve Web/Desktop state and dependency isolation, loopback authentication, opaque native file grants, credential secrecy, and “cancel save before enqueue means no task.”
8. Preserve CLI commands, HTTP contracts, generated types, URLs, navigation ordering, language-policy fingerprints, prompt resources and clean-install behavior.

## 6. Verification record

### Initial documentation-only audit

Baseline environment: `uv sync --locked --all-packages --group dev`. The initial test attempt could not import `wenyi_backend` before this workspace setup; after setup the following two selections passed: **47 + 41 = 88 tests**, with no skips.

```bash
uv run --no-sync pytest -q \
  packages/core/tests/test_architecture_boundaries.py \
  packages/core/tests/test_orchestrator_contract.py \
  packages/backend/tests/test_backend_boundaries.py \
  packages/backend/tests/test_context_isolation.py \
  packages/backend/tests/test_repository_contracts.py

uv run --no-sync pytest -q \
  packages/core/tests/test_storage_injection.py \
  packages/core/tests/test_preparation.py \
  packages/core/tests/test_srt.py \
  packages/backend/tests/test_worker_tasks.py \
  packages/backend/tests/test_export_admission.py \
  apps/api/tests/test_desktop_route_isolation.py
```

These establish the initial audit's limited baseline, **not** implementation or full-system validation. That documentation-only audit did not run frontend typechecks/builds/E2E, the full Python suite, clean-wheel installs, native builds, PostgreSQL/Redis integration, or model-quality evaluation. Implementation follow-ups require their own verification. No private books, project state or real credentials were used.

### Completed implementation acceptance

Local validation used Python 3.12 and Node 22/pnpm 9, temporary fixture state and disposable PostgreSQL/Redis services:

| Check | Result |
|---|---|
| `uv run --no-sync pytest -q -rs` with isolated test database/Redis URLs | 2743 passed, 73 subtests passed, 5 skipped; includes architecture, storage, recovery, API integration and Desktop lifecycle coverage |
| Both hosts: `typecheck`, `typecheck:tests`, `build` | Passed |
| Both hosts: full `test:e2e --workers=2 --max-failures=1` | Web 285 passed; Desktop 246 passed on full rerun with `--trace=retain-on-failure` |
| `pnpm check:schema` and `pnpm test:frontend-boundaries` after both builds | Schema unchanged; 13 boundary tests passed |
| Ruff check/format, targeted Pyright on extracted Core/backend services and adapters, `git diff --check` | Passed; Pyright reported no errors or warnings |
| Five Python package sdist/wheel builds; resource/version and clean-install checks | Passed; 66 bundled language resources and CLI/shared/Web/Desktop dependency isolation verified |
| Default `uv sync --locked` in a fresh environment | CLI/Core only; refreshed editable-package metadata matches the built Git-derived version |

The shared API mock now matches the root `/api/` path rather than any nested `api` directory. A regression test reproduced interception of frontend source modules before the fix and passes afterward. One initial Desktop source-view wait timed out; its isolated rerun and the subsequent full traced suite passed without relaxing assertions. This is recorded as an intermittent test observation, not a separately established product fix.

The five Python skips are three Windows-only cases, the opt-in Nginx proxy integration, and an absent optional Weaver PDF sample. Native Tauri packaging, other OS/Python matrix entries and real-model/real-parser quality comparisons were not run. The extraction changes no prompts or translation semantics. No private books, existing project state or real credentials were used.

For implementation, follow the [repository verification rules](../../AGENTS.md#验证与完成): affected Python tests plus Ruff check/format and `git diff --check`; full Python tests for state/locking/resume or cross-domain changes; both frontend builds/typechecks and affected Playwright suites for shared UI changes. Use isolated `WENYI_TEST_DATABASE_URL` and `WENYI_TEST_REDIS_URL` for integration checks and report skips as unverified. Directory/package moves additionally require clean installation, sdist/wheel resource checks and non-empty architecture scans. Model-quality comparison is necessary only if a separately approved change alters prompt or translation semantics.
