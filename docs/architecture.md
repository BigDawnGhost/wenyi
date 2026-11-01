# Module responsibilities

[简体中文](zh/architecture.md) · [Pipeline](pipeline.md) · [Web development](web.md)

This is a map of the implemented module responsibilities and boundaries.
Core paths below are relative to `packages/core/wenyi_core/`.

The [component decoupling record](design/component-decoupling.md) documents the completed
F1–F7 extractions, their preserved contracts and verification limits.

For Desktop-only application logging and user-triggered support bundles, see the
[user guide](desktop.md#diagnostics-and-feedback) and [incremental diagnostics design](design/application-diagnostics.md).
Desktop owns collection and native publication; shared components expose optional observation hooks only.

| Area | Owner |
|---|---|
| Entry points | `packages/cli/wenyi_cli/cli.py` assembles the CLI; `commands/` registers commands with an invocation context. Web entry points live in `apps/api/wenyi_api/`; the local Desktop entry point lives in `apps/desktop/backend/wenyi_desktop/`. |
| HTTP applications | `packages/backend/wenyi_backend/` owns shared routes, schemas, application services and platform ports. Each app and worker receives its own backend context; platform adapters supply persistence, task scheduling, credentials and telemetry. |
| Backend execution | `packages/backend/wenyi_backend/execution/` separates typed requests, workflow lifecycle, domain operations and export coordination. `bindings.py` supplies explicit project/job ports and existing platform services; `workers/tasks.py` retains stable scheduler entry points. |
| User interface | `packages/ui/` owns shared pages, components and translations. `apps/web/` and `apps/desktop/frontend/` provide independent entries and platform services. Only Desktop includes native import/save and credential adapters. |
| Pipeline routing | `pipeline/orchestrator.py` assembles services and owns routing and lock scopes; domain work stays in the services. |
| Source preparation | `pipeline/input_preparation.py` owns model-free parsing, source/config identity and source snapshots; CLI `parse`, Backend previews and `PreparationService` share it. `packages/backend/wenyi_backend/source_view.py` projects uninitialized source chapters read-only; `pipeline/preparation.py` coordinates initialization and commits the manifest last. |
| Book understanding | `pipeline/book_understanding.py` owns chapter digests, whole-book synopsis, stable-order concurrency and resumable caches. It receives agents/configuration/usage explicitly, not the whole Runtime. |
| Runtime and accounting | `pipeline/runtime.py` assembles clients/agents and binds language/timing. `pipeline/runtime_usage.py` owns the single usage checkpoint and recoverable book/Review publication through consumer-sized ledger protocols. |
| Translation | `pipeline/translation.py` coordinates translation; `translation_batch.py` returns explicit batch results; `title_translation.py` handles titles. |
| Whole-book review | `pipeline/review_workflow.py` coordinates sessions; `review_checkpoint.py`, `review_rounds.py`, `review_chunks.py`, and `review_results.py` separate recovery, decisions, execution, and result handling. |
| Autofix | `pipeline/review_autofix.py` coordinates the separate publication service; `autofix_candidates.py`, `autofix_plan.py`, and `autofix_publish.py` separate candidates, the recoverable index, and writes to formal translations. |
| Review agents | `agents/review_*.py` owns model interactions; `review/` provides shared evidence, types, and run artifacts without depending on pipeline orchestration. |
| EPUB / HTML | `markup/` owns shared deterministic anchors, annotations, ruby and segment handling; readers and writers remain in `ingest/` and `assemble/`. |
| DOCX | `document_styles/docx.py` owns pure style policy; DOCX readers and writers own parsing and document emission. |
| Persistence | Domain services use `storage/protocol.py`. CLI uses `storage/file.py`; Desktop injects `storage/sqlite.py` into its own workspace; Web injects `apps/api/wenyi_api/storage_pg.py` for PostgreSQL state. |
| CLI file migration | `storage/file_migrations.py` owns explicit local-ledger discovery, validation, exact-byte backups, run locking and atomic publication. CLI handles arguments/presentation; this does not discover Web/Desktop state. |

Keep Core independent of CLI and HTTP frameworks. Agents must not depend on the pipeline
or concrete state stores. Shared markup and style policy must not depend on LLMs or a
particular writer. SRT remains an independent lightweight path; BabelDOC remains an
external HTTP service.

Web and Desktop share behavior, not project state or infrastructure. Web uses PostgreSQL
and Redis/Arq; Desktop owns a SQLite catalog, per-project SQLite stores, and a local runner.
Desktop does not read or migrate CLI/Web state. The shared backend does not import platform
packages, and the shared UI receives native capabilities rather than importing an app.
The Web Docker image installs only Web and shared runtime dependencies; the Desktop
sidecar is built in a separate environment without PostgreSQL, Redis, or Arq libraries.

UI hosts import only the explicit `@wenyi/ui` package exports: app, platform, API/types,
HTTP helpers, i18n, document language, styles, approved primitives, settings layout and
formatting. Host `@/` aliases resolve to their own source trees; shared UI uses relative
imports. `api/types.ts` depends on the generated schema, not the API client, and
`platform/contracts.ts` composes transport, progress, preferences, activity and optional
capabilities. `api/client.ts` and the pre-mount `platform()` facade remain single owners;
the old `lib/api.ts` implementation/type forwarder is removed.

`scripts/export_openapi.py` builds the shared HTTP schema with unavailable runtime ports,
without platform entry points or a production lifespan. `pnpm check:schema` compares
offline-generated types against the committed contract in CI. Frontend boundary tests
resolve host imports and derive lazy chunks from the route manifest while retaining
independent URL checks. All 20 cross-host suites, API fixtures and select test support
live in `packages/ui/tests/e2e/`, outside runtime exports. Hosts register them explicitly
with their own bootstrap and capability options; cross-host test imports and fixture
forwarders are forbidden.

Backend execution, project, job-admission and source-view services receive explicit
dependencies rather than discovering them through ambient context or dynamic DAL
forwarding. HTTP routes and scheduler/composition bindings resolve the scoped context;
`routers/dependencies.py` binds HTTP requests to service inputs. The shared dynamic DAL
is removed, and PostgreSQL exposes an explicit supported adapter surface. Platform admission
and compensation remain single port operations with their existing transaction boundaries.
The workflow runner owns durable job rechecks, cancellation and resource cleanup. Its
run lock covers recovery and domain work; successful catalog status writes follow the
live-statistics scope and run-lock exit. Async cancellation drains synchronous checkpoint
writes before releasing the scheduler slot. Export coordination holds one export lock
through claim, render, publication and terminal status; rendering never publishes.

The creator of a per-operation Storage instance closes it on success and failure.
Read routes use explicit close scopes, including error paths; the runner registers
cleanup immediately after each acquisition, including failures during telemetry startup.
Pause admission checks the latest non-export run and active status in one catalog
transaction. Source replacement commits preview-cache invalidation before publishing
the new source reference. A failed invalidation rolls back; an uncertain catalog commit
retains the complete candidate file rather than risking deletion of a published source.
These are safety changes, not a new cross-store transaction or an orphan-file collector.

Initialization commits the manifest last; publication uses a recoverable index before
changing formal targets. Stable segment identities, consistent export snapshots, and
once-only usage accounting remain contracts across storage backends. See the
[repository guide](../AGENTS.md) for the full constraints and required verification.

`i18n/policy/` provides the pure typed language-operation registry and deterministic resolver. Profiles keep source and target roles separate; plans freeze the consumed prompt resources and selected export capabilities. Agents consume prompt plans, export adapters execute text and writer operations, and pipeline services persist phase identities through Storage. CLI and Web execution share this resolver; only read-only developer CLI diagnostics expose its plans. Policy bindings and options are defined in source, without YAML switches. Semantic checkpoints and export snapshots have separate identities, so layout changes do not invalidate paid translation. See [built-in language policies](configuration.md#built-in-language-policies) for supported behavior and developer diagnostics.
