# Component decoupling and directory plan

[简体中文](../zh/design/component-decoupling.md) · [Implemented architecture](../architecture.md)

## Status and decision

**Incremental plan.** The initial review used source revision `80d448f` and added documentation only. The model-free input-parsing portion of F3 has since been implemented, including CLI `parse` and Web/Desktop read-only source views; see the [current module map](../architecture.md) and [CLI workflow](../cli.md#common-commands). Other target paths below remain proposals, not completed migrations.

Keep the existing modular monorepo and deployment boundaries. Decouple responsibilities **inside** packages before moving directories. Preserve public entry points during each migration, and remove compatibility forwarding only after all callers and tests have migrated.

| Approach | Trade-off | Decision |
|---|---|---|
| Reorganize files without changing dependencies | Smaller-looking files, but hidden dependencies and duplicated rules remain | Reject as the primary task |
| Extract use cases and consumer-sized ports incrementally | Reviewable changes; temporary forwarding paths need an explicit removal criterion | **Recommended** |
| Rebuild around new domain packages or microservices | Large import, packaging and operational changes without an identified deployment need | Out of scope |

No database/state migration, new runtime service, new workspace package, prompt change, or UI redesign is required by this plan.

## 1. Boundaries already worth preserving

- Core owns translation behavior; CLI, shared Backend and platform adapters depend on it, not the reverse.
- The [Orchestrator](../../packages/core/wenyi_core/pipeline/orchestrator.py#L17) already delegates to services. Its stage routing and lock scopes are legitimate responsibilities, not another reason to split it.
- [BackendContext](../../packages/backend/wenyi_backend/context.py#L143) supplies per-application adapters. Its ContextVar is request/task scoped, **not** a process-wide Web/Desktop selector. Retain context propagation to threads and callbacks.
- Web owns PostgreSQL and Redis/Arq; Desktop owns its workspace, SQLite catalog, credentials and local runner. PostgreSQL adapters must remain in `apps/api`, never move into Core.
- Shared UI already uses feature directories and injected host capabilities. Keep routes, shell, primitives and features separate; do not move every page merely to rename its parent directory.
- `ArtifactStorage`, review evidence/models, markup, DOCX style policy, language policies and independent SRT execution already have useful boundaries. Extend these rather than introduce parallel abstractions.
- [Python boundary tests](../../packages/backend/tests/test_backend_boundaries.py#L11), [frontend boundary tests](../../packages/ui/tests/platform-boundaries.test.mjs#L31) and [clean-install package checks](../../scripts/check_backend_packages.py#L19) already protect platform isolation. They need targeted extensions, not replacement.

## 2. Findings and intended seams

Priorities below describe migration order, not confirmed production failures. File length, a composition object, or a startup singleton alone is not evidence of a defect.

### F1 — Task execution and scheduler entry points share one lifecycle owner

**Evidence:** [workers/tasks.py](../../packages/backend/wenyi_backend/workers/tasks.py#L50) binds worker context; its [_execute](../../packages/backend/wenyi_backend/workers/tasks.py#L196) also resolves configuration, checks durable job identity, monitors cancellation, runs domain operations, and owns telemetry/storage cleanup. [Desktop LocalRuntime](../../apps/desktop/backend/wenyi_desktop/local_runtime.py#L258) calls these functions using the same worker-shaped dictionary.

**Risk:** changing scheduler invocation or terminal-state handling touches the business execution path. Late completion and duplicate delivery depend on job identity and lock checks embedded in that path.

**P1 seam:** introduce a typed execution request and an explicitly supplied execution dependency object. Extract one runner that owns admission rechecks, interruption, cleanup and terminal-state handling; operation functions own only preview/book/subtitle work. Keep `workers/tasks.py` as thin compatibility entry points with the existing task names and signatures. Web Arq and Desktop scheduling remain platform adapters. CLI continues to call Core directly, not this HTTP application layer.

The runner owns the long book lock from durable identity rechecks through recovery, execution and successful status writes. Move export execution separately: one export coordinator retains the same `export_lock` across claim, render, publish and terminal status. Book rendering uses the consistent Core snapshot and assemble lock; SRT retains its independent state-lock snapshot path. Rendering must not update catalog status, and publication must not invoke a writer. Reuse existing export plan/path/response helpers instead of creating a second export policy.

### F2 — Scoped context is correct, but dynamic forwarding hides service dependencies

**Evidence:** [dal.__getattr__](../../packages/backend/wenyi_backend/dal.py#L1) forwards through the current repository; [Repository](../../packages/backend/wenyi_backend/context.py#L28) combines project, job and export operations. [PostgresRepository](../../apps/api/wenyi_api/adapters.py#L21) also forwards to the platform DAL. The execution function's signature does not reveal these dependencies.

**Risk:** a service can acquire additional persistence capabilities without changing its declared inputs; dynamic forwarding also weakens static checking.

**P1 seam, alongside F1:** define job and project protocols from the extracted consumers, then pass those ports explicitly. Keep `BackendContext` as the composition root and keep the existing settings/export/telemetry contracts. Replace dynamic forwarding incrementally with typed calls.

Do **not** split logically coupled admission and compensation into unrelated repository calls. Preserve each platform's existing boundaries: Desktop uses single SQLite transactions; Web uses its current locks, sequential writes and failure compensation, not one shared database transaction. Strengthening Web atomicity would be a separate behavior change. Existing `PostgresRepository` and `LocalBackend` may implement multiple small protocols. Do not create one new adapter object per method or move SQL into shared Backend.

### F3 — Input preparation mixes parsing, initialization and model-assisted understanding

**Evidence:** [PreparationService](../../packages/core/wenyi_core/pipeline/preparation.py#L63) owns parsed-cache validation, state lookup and initialization as well as book understanding. The Backend [preview path](../../packages/backend/wenyi_backend/workers/tasks.py#L72) calls its static cache helpers but independently assembles parser arguments, source checks and the parsed-document cache payload. Core's [prepare path](../../packages/core/wenyi_core/pipeline/preparation.py#L149) has its own parsing branches.

**Risk:** parser options, cache identity and source-change handling have several callers that must evolve together; preview should not need a model-equipped runtime.

**P1 seam:** extract an input-preparation API for parsing options, source identity checks and validated cached documents. Return a document and its identity; use injected artifact access where caching is supported. Backend retains HTTP preview projection, and Core retains initialization and manifest commit ownership.

First preserve the existing cache-read/write policy for each caller; do not silently add caches or change invalidation. PDF's known state path and pre-conversion initialization marker must remain distinct from title-based discovery for other formats. Later extract book understanding, including its stable-order concurrency and checkpoints, while keeping `PreparationService` as the coordinator.

### F4 — Runtime assembly also owns recoverable usage publication

**Evidence:** [PipelineRuntime](../../packages/core/wenyi_core/pipeline/runtime.py#L36) constructs clients and agents, while [flush_usage](../../packages/core/wenyi_core/pipeline/runtime.py#L113) updates a client checkpoint and publishes both book and Review ledgers. [Storage](../../packages/core/wenyi_core/storage/protocol.py#L30) is a broad aggregate, but an artifact-only port already exists.

**Risk:** moving apparently small accounting code can change checkpoint timing, recovery or once-only charging. Passing the entire runtime into every extracted service would preserve the coupling.

**P2 seam:** first extract a run-owned usage coordinator with explicit client-usage and ledger dependencies. Preserve one client and one checkpoint owner per run. Reuse the existing usage arithmetic; retain the aggregate `Storage` for compatibility and introduce narrower protocols only for actual consumers.

Do not pre-create a matrix of empty ports. Language binding and timing stay in Runtime until a specific consumer needs an independent contract. Keep `RunStore` and storage schemas in place during this stage.

### F5 — UI contracts expose implementation paths and a type-only dependency cycle

**Evidence:** [platform.ts](../../packages/ui/src/platform.ts#L1) imports `ProjectDetail` from the API implementation, while [lib/api.ts](../../packages/ui/src/lib/api.ts#L1) imports platform access and source capabilities. This is a **type-only back edge**, not a demonstrated runtime cycle. The package [wildcard export](../../packages/ui/package.json#L6), host [TypeScript aliases](../../apps/desktop/frontend/tsconfig.json#L16) and [Vite aliases](../../apps/desktop/frontend/vite.config.ts#L7) expose shared UI internals; [DesktopCredential](../../apps/desktop/frontend/src/DesktopCredential.tsx#L6) consumes primitive implementation paths.

**Risk:** moving a private UI module forces host edits, and foundational contracts depend on the client implementation they support. This is a public API design issue, not an invalid host-to-shared dependency.

**P1 seam, independently parallelizable:** extract schema-derived API types, then let platform contracts and the client import those types. Keep a single API client. Compose transport/progress, preferences/activity and optional native UI capabilities from smaller interfaces; retain pre-mount host configuration and the `platform()` facade.

Publish explicit host entry points for App, platform contracts, HTTP helpers, i18n, styles and approved primitives. Update package exports, TypeScript resolution and Vite resolution together: wildcard aliases must not bypass the new exports. Shared UI's own `@/` imports also need conversion or a package-local resolution strategy **before** a host alias is redirected to host `src`.

Keep query/mutation ownership with its feature. Split endpoint modules only as consumers require it; do not add a second state cache, generic service framework or standalone API-client package.

### F6 — Some shared verification still belongs to one host or repeats contracts

**Evidence:** Desktop [fixtures](../../apps/desktop/frontend/tests/fixtures.ts#L1) and [shell suite](../../apps/desktop/frontend/tests/unified-shell.spec.ts#L1) import Web tests. The [bundle test](../../packages/ui/tests/platform-boundaries.test.mjs#L14) hand-maintains route chunk names despite the runtime route manifest. [Schema generation](../../package.json#L15) reads a running localhost API; the reviewed [CI workflow](../../.github/workflows/tests.yml#L94) does not regenerate and compare that contract.

**Risk:** shared test changes are owned by Web accidentally; new routes or schema changes may not update every duplicated test/type input. These are guardrail gaps, not proof that current types or routes are broken.

**P0/P1 seam:** add deterministic offline OpenAPI generation using the shared app factory with fake ports and no production lifespan. Generate TypeScript to a temporary file and compare it with the committed schema; retain one source of truth. Derive route enumeration from the manifest, while keeping independent assertions for required URLs, redirects, audience filtering and shell continuity.

Move platform-neutral fixtures and parameterized shared behavior into UI test support. Each host explicitly registers the suite with its own setup; native save/drop/credentials/bootstrap and browser behavior remain separate. Keep test support outside runtime exports and production bundles.

### F7 — CLI migration reaches a private file-store method

**Evidence:** [migrate_usage](../../packages/cli/wenyi_cli/model_commands.py#L114) knows ledger paths, creates backups and calls `RunStore._write_json`.

**Risk:** a storage refactor can silently break a user-facing migration command.

**P2 seam:** introduce a public file-storage migration function owning enumeration, backup, locking and atomic writes; reuse the pure ledger converter. CLI owns argument validation and presentation. This is intentionally a CLI file-state operation, not a new Web/Desktop migration route.

The workflow command may later extract pure option validation when touched; do not reorganize all CLI commands as a prerequisite.

## 3. Target directory map

This is a **selected target map**, not a full tree. `+` means proposed, `~` means retained with a narrower role; unlisted modules stay where they are. Create modules only in the stage that gives them a real implementation.

```text
packages/
  core/wenyi_core/
    pipeline/
      orchestrator.py                  # unchanged facade
      preparation.py                  # ~ initialization and stage coordination
      input_preparation.py            # extracted parsing/cache rules shared with preview
      book_understanding.py           # + digest/synopsis coordination, later stage
      runtime.py                      # ~ composition, language binding and timing
      runtime_usage.py                # + run-owned usage checkpoint/publication
      runstore.py                     # unchanged file-state compatibility layer
    storage/
      protocol.py                     # retained; consumer-sized protocols as needed
      file.py / sqlite.py             # retained adapters
      file_migrations.py              # + public CLI file migration operation
  backend/wenyi_backend/
    application.py / context.py       # retained HTTP and adapter composition
    ports/
      projects.py / jobs.py           # + consumer-sized persistence contracts
    execution/
      contracts.py                    # + typed run request and dependencies
      runner.py                       # + identity, cancellation, cleanup, terminal state
      operations.py                   # + preview/book/SRT dispatch into Core
      exporting.py                    # + rendering/publication with distinct functions
    workers/tasks.py                  # ~ stable task entry-point wrappers
    routers/                          # retained HTTP translation layer
    export_plan.py / export_paths.py  # retained, not duplicated
    dal.py                           # transitional forwarding, no new consumers
  ui/
    src/
      App.tsx / routes/              # retained shell and routing
      platform.ts                    # ~ compatibility entry point
      platform/contracts.ts          # + composable host capability contracts
      platform/runtime.ts            # + pre-mount configuration and access
      api/types.ts                   # + aliases derived from generated schema
      api/client.ts                  # + the existing single client, relocated later
      lib/api.ts                     # ~ temporary API re-export
      components/ui/index.ts         # + approved host-facing primitives
      features/                      # retained; feature-local queries/components
      i18n/                          # retained
    tests/e2e/
      fixtures.ts / contracts.ts     # + host-neutral support, not runtime exports
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
| S0 — Guardrails | Establish baseline; add schema freshness and resolved-import/route checks for planned seams | Scans assert non-empty input; a deliberately forbidden import is detected; fake-port schema export uses no external services; both existing host builds precede bundle checks |
| S1 — UI public surface | F5 type extraction/exports, then F6 shared test support; independent of Python extraction after S0 | Both hosts typecheck/build; boundary tests and affected E2E pass; no URL, query-key, DOM/shell, native capability or credential behavior change |
| S2 — Backend execution | F1 runner plus only the F2 ports it consumes; preserve worker wrappers before changing host callers | Cover duplicate/stale jobs, pause/cancel, cleanup, compensation, lock contention, duplicate exports and publication/terminal failures; concurrent app/worker isolation and `to_thread`/raw-thread/callback propagation; isolated PostgreSQL/Redis and Desktop runner contracts |
| S3 — Shared input preparation | F3; can start after S0, coordinate preview call-site changes with S2 | Cache hit/miss/config/source mismatch, PDF failure/retry, manifest-last commit and no-model preview tests; book-understanding extraction is a separate follow-up |
| S4 — Accounting and file migration | F4/F7 after the consuming interfaces are stable; separate changes | Crash/retry tests prove book/Review usage merged once; backups and atomic writes unchanged; full Python suite and storage adapters verified |
| S5 — Remove forwarding | Only after migrated callers and packaging checks pass | No remaining callers of retired entry points; docs/tests/import scanners updated together; package smoke tests discover actual modules/resources |

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

## 6. Verification and initial audit result

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

For implementation, follow the [repository verification rules](../../AGENTS.md#验证与完成): affected Python tests plus Ruff check/format and `git diff --check`; full Python tests for state/locking/resume or cross-domain changes; both frontend builds/typechecks and affected Playwright suites for shared UI changes. Use isolated `WENYI_TEST_DATABASE_URL` and `WENYI_TEST_REDIS_URL` for integration checks and report skips as unverified. Directory/package moves additionally require clean installation, sdist/wheel resource checks and non-empty architecture scans. Model-quality comparison is necessary only if a separately approved change alters prompt or translation semantics.
