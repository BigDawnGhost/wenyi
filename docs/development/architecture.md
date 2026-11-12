# Architecture and module boundaries

This page maps the current owners and dependency boundaries. For user-visible
translation behavior, read the [pipeline guide](../guide/pipeline.md); for state,
checkpoints and recovery, read the [pipeline contracts](pipeline.md).

## Dependency direction

```text
CLI / Web / Desktop
  → Orchestrator
    → Run / Prepare / Translate / Annotate / Review / Autofix / Finalize
      → agents / ingest / glossary / assemble / Storage ports
```

Core does not import CLI or HTTP frameworks. The orchestrator assembles services
and owns routing and lock scopes; it does not call domain functions directly or
own thread pools. Agents do not depend on pipeline orchestration or concrete state
stores. Shared markup and style policy do not depend on LLMs or a particular writer.
BabelDOC remains an external HTTP service, not an AGPL dependency of Core.

## Responsibility map

Core paths below are relative to `packages/core/wenyi_core/`; app and package paths
are relative to the repository root.

| Area | Owner |
|---|---|
| Entry points | `packages/cli/wenyi_cli/cli.py` assembles the CLI; `commands/` registers commands with an invocation context. Web entry points live in `apps/api/wenyi_api/`; the local Desktop entry point lives in `apps/desktop/backend/wenyi_desktop/`. |
| HTTP applications | `packages/backend/wenyi_backend/` owns shared routes, schemas, application services and platform ports. Each app and worker receives its own backend context; platform adapters supply persistence, task scheduling, credentials and telemetry. |
| Backend execution | `packages/backend/wenyi_backend/execution/` separates typed requests, workflow lifecycle, domain operations and export coordination. `bindings.py` supplies explicit project/job ports and existing platform services; `wenyi_backend/workers/tasks.py` retains stable scheduler entry points. |
| User interface | `packages/ui/` owns shared pages, components and translations. `apps/web/` and `apps/desktop/frontend/` provide independent entries and platform services. Only Desktop includes native import/save and credential adapters. |
| Pipeline layout | `pipeline/` groups modules by sub-stage: `run/` (runtime, usage checkpoint, language policies), `prepare/` (source parsing, prescans, book synthesis), `translate/` (batches, terminology, titles, context, precision), `annotate/` (EPUB annotations, DOCX styles), `review/`, `autofix/` and `finalize/`. `pipeline/__init__.py` re-exports every module, so both the flat and the staged import paths resolve. |
| Pipeline routing | `pipeline/orchestrator.py` stays at the top of the package and assembles services, owns routing and lock scopes; domain work stays in the services. |
| Source preparation | `pipeline/prepare/input_preparation.py` owns model-free parsing, source/config identity and source snapshots; CLI `parse`, Backend previews and `PreparationService` share it. `packages/backend/wenyi_backend/source_view.py` projects uninitialized source chapters read-only; `pipeline/prepare/preparation.py` coordinates initialization and commits the manifest last. |
| Book understanding | `pipeline/prepare/book_understanding.py` owns chapter digests, whole-book synopsis, stable-order concurrency and resumable caches. It receives agents/configuration/usage explicitly, not the whole Runtime. |
| Runtime and accounting | `pipeline/run/runtime.py` assembles clients/agents and binds language/timing. `pipeline/run/runtime_usage.py` owns the single usage checkpoint and recoverable book/Review publication through consumer-sized ledger protocols. |
| Translation | `pipeline/translate/translation.py` coordinates translation; `translation_batch.py` returns explicit batch results; `title_translation.py` handles titles. |
| Whole-book review | `pipeline/review/review_workflow.py` coordinates sessions; `review_checkpoint.py`, `review_rounds.py`, `review_chunks.py`, and `review_results.py` separate recovery, decisions, execution, and result handling. |
| Autofix | `pipeline/autofix/review_autofix.py` coordinates the separate publication service; `autofix_candidates.py`, `autofix_plan.py`, and `autofix_publish.py` separate candidates, the recoverable index, and writes to formal translations. |
| Review agents | `agents/review_*.py` owns model interactions; `review/` provides shared evidence, types, and run artifacts without depending on pipeline orchestration. |
| EPUB / HTML | `markup/` owns shared deterministic anchors, annotations, ruby and segment handling; readers and writers remain in `ingest/` and `assemble/`. `assemble/epub_package.py` packages generated EPUB 3 books with local XML serialization and `zipfile`, without EbookLib. |
| DOCX | `document_styles/docx.py` owns pure style policy; DOCX readers and writers own parsing and document emission. |
| Persistence | Domain services use `storage/protocol.py`. CLI uses `storage/file.py`; Desktop injects `storage/sqlite.py` for each project directory resolved through its catalog's registered library location; Web injects `apps/api/wenyi_api/storage_pg.py` for PostgreSQL state. |
| Settings reconstruction | `config_rebuild.py` validates saved fields against the current Core schema; `packages/backend/wenyi_backend/config_rebuild.py` applies global/project scope. Each platform owns backup and replacement. |
| CLI file migration | `storage/file_migrations.py` owns explicit local-ledger discovery, validation, exact-byte backups, run locking and atomic publication. CLI handles arguments/presentation; this does not discover Web/Desktop state. |

## Platform ports

Web and Desktop share behavior, not project state or infrastructure. Web uses PostgreSQL
and Redis/Arq; Desktop owns a SQLite catalog, per-project SQLite stores, and a local runner.
Desktop does not read or migrate CLI/Web state. The shared backend does not import platform
packages, and the shared UI receives native capabilities rather than importing an app.
The Web Docker image installs only Web and shared runtime dependencies; the Desktop
sidecar is built in a separate environment without PostgreSQL, Redis, or Arq libraries.

Every app, worker and thread callback retains its own backend context. HTTP routes and
scheduler/composition bindings resolve that context; `routers/dependencies.py` binds HTTP
requests to explicit service inputs. Execution, project, job-admission and source-view
services do not discover dependencies through ambient context or dynamic DAL forwarding.
Admission and compensation are complete port operations with platform-owned transactions.

UI hosts use explicit `@wenyi/ui` exports and configure platform services before mounting.
Shared schema generation is offline and does not start production services. See
[frontend conventions](frontend.md) for imports, routes, loading and cross-host tests, and
[Web development](web.md) for schema generation and transport contracts.

Application diagnostics belong to Desktop. Shared components provide optional observation
hooks, not a global logging backend. Collection, privacy filtering and native publication
are covered in [Desktop development](desktop.md); user instructions live in the
[Desktop guide](../guide/desktop.md#diagnostics-and-feedback).

## Storage boundary

Domain services persist through `Storage` and `ArtifactStorage`, not by opening local
SQLite databases or writing file-backed state directly. Artifact keys are run-relative,
use `/` on every platform and support bounded prefix queries. `storage/run_store.py`
owns the file-backed ledger; it does not import pipeline services. File adapters expose
explicit members rather than forwarding arbitrary attributes to the ledger.

The `Storage` properties `run_dir`, `source_dir` and `reviews_dir` remain a compatibility
gap for path-based consumers. They are not permission for new domain-level filesystem
writes. Prefer identity-based storage operations for new work; changing an existing path
consumer requires coordinated adapter and contract-test changes. Do not assume every
adapter supports file-specific helpers outside the declared protocol.

The creator of a per-operation Storage instance closes it on success and failure.
Read routes use explicit close scopes; runners register cleanup immediately after each
resource acquisition, including failures while starting telemetry.

### Saved settings reconstruction

Reconstruction is separate from book-state or ledger migration. It runs when CLI
configuration is loaded and when each Web repository or Desktop catalog starts.
The pure reconstruction functions preserve valid supplied fields, discard invalid
ones, and validate the resulting effective configuration without aliasing retired
names. Unsupported default templates and invalid saved strategies reset to current
defaults instead of being interpreted on read. New setting writes use strict validation.

CLI takes a per-file upgrade lock, rechecks the current bytes, publishes an exact
content-addressed backup, then atomically replaces the YAML. Web and Desktop
insert the original document and discarded paths into `configuration_backups`
in the same transaction as the replacement. A null `project_id` identifies global
settings, including their revision and default-template metadata; a project ID
identifies a backup containing that project's `config` and `strategy`.
Unchanged documents produce no writes.
Backup or replacement failure preserves the original document; each project's
transaction is independent, so a later failure does not undo earlier upgrades.

Web uses the settings advisory lock and project row locks. Desktop uses short
catalog write transactions, with no credential access, model requests or filesystem
work inside them. Neither path modifies project state, usage or frozen jobs.
Backup locations and user-visible fallback behavior are documented in the
[configuration reference](../reference/configuration.md#saved-settings-reconstruction).

## Execution and publication

The workflow runner owns durable job rechecks, cancellation and resource cleanup. Its
run lock covers recovery and domain work; successful catalog status writes follow the
live-statistics scope and run-lock exit. Async cancellation drains synchronous checkpoint
writes before releasing the scheduler slot. Export coordination holds one export lock
through claim, render, publication and terminal status; rendering never publishes.

Pause admission checks the latest non-export run and active status in one catalog
transaction. Source replacement commits preview-cache invalidation before publishing
the new source reference. A failed invalidation rolls back; an uncertain catalog commit
retains the complete candidate file rather than risking deletion of a published source.
These boundaries do not provide a cross-store transaction or automatic orphan-file cleanup.

Initialization commits the manifest last; publication uses a recoverable index before
changing formal targets. Stable segment identities, consistent export snapshots, and
once-only usage accounting apply across storage backends. Detailed checkpoint, language
policy and prompt-resource rules live in [pipeline development](pipeline.md).

### EPUB CPU workers

EPUB readers and template exporters use `resource_pool.py` for physical-XHTML work:
at least 12 resources and 2 MiB of uncompressed XHTML enable up to six spawned workers,
capped by available CPU count and affinity. Smaller inputs, daemon processes and
non-importable interactive entry points run the same functions serially. These internal
thresholds are independent of LLM concurrency and apply to CLI, Web and Desktop.

One admission slot per parent process prevents concurrent EPUB operations from multiplying
their pools; separate application/worker processes retain independent budgets. Each pool
admits at most two pending resources per worker, yields in source order and drains/cancels
its work before releasing admission. Workers receive explicit data, never Storage,
database connections, credentials or platform context.

Parsing annotates resources, then resolves note contexts by target XHTML before constructing
logical chapters. Bilingual export prepares all source-link destinations before rendering,
sending each resource only its outgoing link mapping. EPUB presentation rewrites run in the
resource task; ZIP reading/writing, package coordination and atomic publication stay in the
parent. Frozen CLI/Desktop entry points dispatch multiprocessing workers before importing
their applications.

## Boundary verification

- Run `packages/core/tests/test_architecture_boundaries.py` for dependency changes and
  `test_orchestrator_contract.py` for pipeline assembly changes.
- For storage changes, also run `test_storage_injection.py` and the affected adapter tests.
- For shared UI changes, verify both hosts and run `pnpm test:frontend-boundaries` after
  both builds. Schema changes need `pnpm check:schema`.

Use the [repository guide](../../AGENTS.md) for the complete safety constraints and the
[contribution guide](../../CONTRIBUTING.md#python-regression-tests) for test commands.
