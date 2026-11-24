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
| EPUB / HTML | `markup/` defines the annotation, ruby and segment state contracts. `packages/core/rust/` owns EPUB parsing, template backfill and EPUB 3 package generation. Python HTML handling and cross-format adapters remain in `ingest/` and `assemble/`. |
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

<a id="locks-backend"></a>

### Cross-process file locks

Local stores serialize workflow-critical sections with cross-process advisory file
locks (`packages/core/wenyi_core/storage/locks.py`). Two interchangeable backends
satisfy one contract (`packages/core/tests/test_locks_contract.py`): the optional
`wenyi-locks` native library loaded through ctypes and a pure-Python fallback.
The backend is probed once per process:

- `WENYI_LOCKS_BACKEND` selects `auto` (default), `rust` or `python`. `auto` and
  `rust` prefer the native library; `python` always uses the fallback. Unknown
  values raise `ValueError`.
- `WENYI_LOCKS_LIB` names an explicit shared library (`wenyi_locks.dll`,
  `libwenyi_locks.dylib` or `libwenyi_locks.so`) and is tried first; if it fails
  to load, the override is ignored with an error log and the search continues.
  Without it the loader probes the PyInstaller bundle directory,
  `$CARGO_TARGET_DIR/{release,debug}` and the repository-root
  `target/{release,debug}` — native crates share the root Cargo workspace, so a
  checkout build is found without an override.
- `WENYI_LOCKS_MODE` decides what a failed native load means: `warn` (default)
  logs one warning naming the tried paths and keeps the Python fallback, `error`
  fails lock acquisition with those reasons, and `silent` keeps the fallback
  without the warning. Unknown values raise `ValueError`.

Build the library with `cargo build -p wenyi-locks` (or `--release`); how release
packaging bundles it is described in [Desktop releases](releasing.md).

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
run lock covers recovery and domain work. The synchronous writer returns only after
checkpoint writes, live statistics and storage cleanup; then the runner rechecks run
identity and publishes the catalog outcome. See the [shared cancellation contract](web.md#workflow-cancellation)
for how cancellation retains scheduler ownership.

Export coordination holds one export lock through claim, render and publication; rendering
never publishes. The catalog commits the published artifact and its job's successful
status together. Retired history is hidden at that commit; reclaiming its files afterward
cannot change publication status.

Pause admission checks the latest non-export run and active status in one catalog
transaction. Source replacement commits preview-cache invalidation before publishing
the new source reference. A failed invalidation rolls back; an uncertain catalog commit
retains the complete candidate file rather than risking deletion of a published source.
These boundaries do not provide a cross-store transaction or automatic orphan-file cleanup.

Initialization commits the manifest last; publication uses a recoverable index before
changing formal targets. Stable segment identities, consistent export snapshots, and
once-only usage accounting apply across storage backends. Detailed checkpoint, language
policy and prompt-resource rules live in [pipeline development](pipeline.md).

### Native EPUB engine

The crate lives in `packages/core/rust/`. `src/lib.rs` is the PyO3 boundary;
`src/epub/` contains the engine, with `ingest.rs` and `extract.rs` for reading,
`export.rs`, `generate.rs` and `html.rs` for writing, and shared modules for the
archive, DOM, tokenizers, links and worker budget.

Core's private PyO3 extension opens the ZIP, reads OPF and NAV/NCX, builds XHTML
trees, extracts paragraphs and notes, and groups them into logical chapters. The
XHTML and tolerant HTML tokenizers both run in Rust. The engine returns the same
chapter, anchor and annotation state consumed by translation and resume.

Template export checks saved anchors and source text against the original resource.
It reuses the in-memory tree for translations, note placement and presentation.
When rendering an original EPUB from a frozen snapshot, the writer consumes one
independent copy of each chapter without caching another in the export view.
Chapters with no saved targets skip translation-only processing, not source validation.
Bilingual export allocates source IDs for all pages before rewriting cross-file
links; it does not serialize and reparse the trees. The writer copies unchanged
members in compressed form and inserts the optional about page in the same pass.
For EPUB-to-HTML export, Rust extracts the rendered bodies and embeds referenced
media using ZIP-only lookup; Python supplies the common HTML shell. For other
source formats, Python supplies rendered chapters and resolved assets to Rust to
generate the EPUB 3 package.

The extension releases the Python GIL. A process-wide admission slot bounds
simultaneous book-sized trees, and an ordered pool of up to six threads processes
books with at least twelve resources. Python still owns storage snapshots, language
policies and atomic replacement; native workers never access application state.

The same extension hosts `text.rs` (plain-text and Markdown sections, paragraphs
and export formatting) and `fb2.rs` (FictionBook XML and resource references).
The Python adapters keep the strict legacy-encoding policy.

`docx/` handles WordprocessingML import and reconstruction, including the package
parts, paragraph and run properties, table merges and list numbering. Python
applies export language policy and supplies one snapshot chapter at a time. Rust
projects style ranges, merges continuations and orders bilingual paragraphs.
`python-docx` is used for independent test fixtures, not by the installed engine.

For standalone HTML with an explicit canonical shell and text-only paragraphs
and headings, `html/ingest.rs` builds chapters, anchors and templates in one pass.
Other inputs retain the tolerant HTML parser and the same persisted identities.
`html.rs` plans prose regions, normalizes annotation offsets, backfills text
and attributes, updates canonical document
shells and rewrites local media references. For canonical HTML, Rust can also
emit both bilingual orders and restore simple annotations with verified offsets.
Complex link rewrites, nested or stale placements and noncanonical inputs use
the Python compatibility renderer.
Large MinerU PDFs are split in a private directory using `pdf/`, with pypdf page
cloning for unsupported page features. Python reads the Content List V2 result
ZIPs in chunk order and publishes one `converted.epub`. New runs use the regular
EPUB reader and writer. The manifest binds the original PDF hash and the derived
EPUB hash; `fmt` remains `pdf` for the project and its previews. Template-aware
operations use the derived EPUB, not the uploaded file. Exports never request
another conversion from MinerU.

Core reports MinerU failures using local categories and controlled fields, such
as the request stage, HTTP status, retry count, or missing field name. Never
copy service messages, response excerpts, batch IDs, or signed URLs into
exceptions or logs: task history and live progress reuse the error message.
HTTP client traces are suppressed only during MinerU requests. Local guidance,
such as adding an OCR text layer to an image-only PDF, remains visible.

To inspect an existing MinerU result bundle, use the same renderer offline:

```bash
uv run --no-sync wenyi mineru-epub path/to/mineru-result.zip path/to/source.epub
```

The converter uses only Content List V2 and referenced local assets, not the
Markdown or HTML export. It retains the recorded block order, page footnotes,
figures, captions and reference entries, while omitting running page headers,
footers and numbers. It emits MathML with visible equation numbers and uses an
available formula crop when display math cannot be rendered. In mixed prose and
math paragraphs it fills missing spaces next to Latin letters and digits and
uses start alignment to avoid stretching the words before wrapped formulas.
Neither adjustment changes the LaTeX. Code listing text keeps its whitespace;
algorithm prose and captions are translatable, with inline formulas protected.
Generated source editions mark each explicit TOC heading as a logical chapter
boundary, including nested headings. The NAV document is not in the reading
order. This preserves the TOC tree without changing chapter selection for
ordinary EPUBs. Only links present in the JSON are carried over.

PDF layout extraction and fillback remain in the separate BabelDOC bridge. For
image-only detection and bookmarks, Core first inspects the PDF natively; ambiguous
text, named destinations and unusual outlines use a shared pypdf reader. Core
projects translations, row ranges and annotation placements from the same saved
paragraphs. It streams the HTTP response to a private file and uses `pdf/` to
compose bilingual spreads and remap links before atomic publication. Supported page
content is written into the spread itself, so ordinary page text stays visible to
pypdf's layout extractor. The Python composer remains the fallback for unsupported
features such as encrypted inputs or non-link annotations.

## Boundary verification

- Run `packages/core/tests/test_architecture_boundaries.py` for dependency changes and
  `test_orchestrator_contract.py` for pipeline assembly changes.
- For storage changes, also run `test_storage_injection.py` and the affected adapter tests.
- For shared UI changes, verify both hosts and run `pnpm test:frontend-boundaries` after
  both builds. Schema changes need `pnpm check:schema`.

Use the [repository guide](../../AGENTS.md) for the complete safety constraints and the
[contribution guide](../../CONTRIBUTING.md#python-regression-tests) for test commands.
