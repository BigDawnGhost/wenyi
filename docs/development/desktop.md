# Desktop development

This page covers the local engine, native host and their safety contracts.
For installing and using the application, see the [Desktop guide](../guide/desktop.md).
Packaging and upgrades belong in [Releasing](releasing.md); shared UI rules are in
[Frontend development](frontend.md).

## Run from source

Use Python 3.10+, `uv`, Node 22, pnpm 9, Rust stable and the
[Tauri platform prerequisites](https://v2.tauri.app/start/prerequisites/).
Debian/Ubuntu also needs `libdbus-1-dev` for the native tray availability checks.
These are development requirements; packaged applications carry their Python engine.

From the repository root:

```bash
uv sync --locked --package wenyi-desktop --group dev
pnpm install --frozen-lockfile
pnpm desktop
```

To select a separate application data root:

```bash
pnpm desktop --data-dir /path/to/desktop-test-data
```

**`--data-dir` selects the whole data root**, including catalog, settings, logs,
locks and plugin runtime. It does not move or discover existing projects, and there
is no separate shared catalog behind it. Automated tests should inject a temporary
root, temporary external libraries and fake credential storage: the OS credential
vault is not inside the data root. Never use a real project library as disposable data.

The [launcher](../../scripts/dev/desktop.mjs#L55) resolves the Git version, builds
the UI, then starts the native debug application. It uses the repository's `.venv`
unless `WENYI_DESKTOP_PYTHON` selects an explicit development interpreter. There is
no remote `--url` mode or silent fallback to a Web server. A UI-only Vite server
does not provide a native engine or IPC implementation.

Build the frontend before invoking Cargo directly: Tauri embeds its output during
compilation. For example:

```bash
pnpm -C apps/desktop/frontend build
cargo test --locked --manifest-path apps/desktop/Cargo.toml
```

## Platform boundaries and connection identity

| Layer | Responsibility |
| --- | --- |
| Native host | Window/tray lifecycle, workspace activation, owned child process, native file capabilities, diagnostics persistence and application updates |
| Desktop Python adapter | SQLite catalog, project storage, credentials, local task supervision and Desktop-only HTTP endpoints |
| Shared backend | HTTP contracts and application services using explicit repository, storage, telemetry, credential and execution ports |
| Shared Core | Translation and document services through `Storage`/`ArtifactStorage`, without importing Desktop or HTTP code |
| Shared UI | Pages and components using injected platform services, without importing either app |

[Desktop assembly](../../apps/desktop/backend/wenyi_desktop/main.py#L113) creates a
`BackendContext` per application and injects local implementations. Keep that context
bound through HTTP requests, worker entries and thread callbacks. Shared backend
code must not select a platform through process-global flags or import
PostgreSQL/Redis/Arq/keyring adapters. See [Architecture](architecture.md) for the
repository-wide dependency direction.

The engine listens on a random loopback port with a fresh memory-only bearer token.
The [native process protocol](../../apps/desktop/src/process.rs#L65) validates the
`WENYI_READY` protocol version, port and exact owned child PID before publishing a
connection. The UI waits for readiness; startup failure is not permission to try
another endpoint. Do not persist ports or tokens in project data.

Engine startup overlaps WebView creation after native admission. Readiness and
failure must survive a window that has not loaded yet, and setup failure must
close spawn admission and reap the owned child even before the event loop runs.
The loopback listener uses an explicit TCP protocol and `TCP_NODELAY`; test the
accepted connection, not just an in-process ASGI client.

On Windows, a debug Python override must be an existing executable path, not a
PATH command or wrapper. For a CPython venv, the host starts the base interpreter
with `__PYVENV_LAUNCHER__` so imports retain venv identity while the engine remains
the directly owned child. Packaged sidecars start directly. Never authorize killing
an arbitrary PID from a readiness message.

Credentials belong to the Desktop credential adapter: use the system vault when
available and session memory with explicit degraded-state feedback otherwise.
Never store keys in SQLite, YAML/JSON, browser storage or diagnostics. The optional
BabelDOC plugin stays in a separate process and communicates over HTTP; do not add
its AGPL runtime to the main application or Core dependency graph.

## Data-root ownership

One selected root owns the application's local data. Platform defaults and manual
backup steps are documented in the [Desktop guide](../guide/desktop.md#data-locations-and-backups).

| Location | Ownership and contents |
| --- | --- |
| `<data-root>/catalog.sqlite3` | Project/job catalog, settings and opaque credential references |
| `<data-root>/projects/` | Default library of project SQLite state, originals, parse caches and exports |
| `<data-root>/logs/`, `locks/` | Diagnostics, process admission and activation records |
| `<data-root>/runtime/` | Plugin runtime/sessions and Windows/Linux WebView data |
| `<data-root>/backups/` | Verified in-app upgrade snapshots |
| External project libraries | Explicitly selected local locations, recorded per project in the catalog |

Path names are centralized in the
[Python path module](../../apps/desktop/backend/wenyi_desktop/paths.py#L38) and
[native path module](../../apps/desktop/src/paths.rs#L21). Keep their cross-language
contracts aligned. `workspace` in existing APIs means this selected data root,
not a second storage location. The system credential vault and macOS-managed
WKWebView data remain outside it.

Resolve projects through the
[catalog's project-directory resolver](../../apps/desktop/backend/wenyi_desktop/local_backend.py#L157),
not by concatenating the current root and project ID. Changing the default library
does not move registered projects; a readable directory suffix is not stronger
identity evidence than its project marker.

A project-list read scans each required library once and shares that inventory
only within the read. Duplicate identities, conflicting markers and links still
refuse the affected project. Never reuse this inventory for write admission or
across requests: relocation and new duplicate folders must be detected next time.

The data root and libraries must be on local disks. Network shares and cloud-synced
folders are unsupported because SQLite locks, placeholders and independently copied
WAL files can invalidate consistency. The application does not detect every such location.
[Native layout admission](../../apps/desktop/src/paths.rs#L174) checks the actual
executable directory against the data root, including source/unpacked launches and
`--data-dir`. Path comparison resolves existing ancestors even when the destination
does not yet exist, so directory links and Windows short names cannot hide overlap.

Native single-instance admission happens before the WebView or Python engine is
created. Ownership is per canonical data root, including path aliases, not per
executable/version. A second launch activates the existing window without replacing
its WebView. The native `locks/.desktop-instance.lock` and engine
`locks/.desktop.lock` are separate OS-owned safety checks. Discovery-file existence
is not proof of a live owner; never delete lock files to bypass one.

The [catalog transaction helper](../../apps/desktop/backend/wenyi_desktop/local_backend.py#L129)
uses short SQLite transactions, WAL and a bounded busy timeout. Read-only status
queries read committed snapshots without reserving a writer. Do not hold catalog
write transactions during model requests, vault access, compression or file cleanup.
Domain state remains in each project's injected `SqliteStorage`; the catalog
describes scheduling and registered projects, not translation checkpoint authority.

## Catalog admission and project import

[Catalog initialization](../../apps/desktop/backend/wenyi_desktop/catalog.py#L12)
checks SQLite `application_id` and `user_version` before application stores add
tables. Only an empty database may be initialized; an unidentified or unsupported
catalog is refused without adopting its records. The current catalog format is 2.

Startup does not scan legacy locations, migrate catalogs, merge old settings or
rebuild an empty catalog from project folders. Selecting a new root starts with an
empty list while older directories remain untouched. Recovery requires a consistent
backup, not automatic rediscovery.

[Explicit project import](../../apps/desktop/backend/wenyi_desktop/project_import.py#L138)
accepts only a deliberately selected current-format project:

- Require a valid `project.json` with `schema_version: 2`, readable saved state and
  one original matching the saved SHA-256. Missing/old markers are not migrated.
- Refuse linked files, deletion markers, duplicate project identities and occupied
  destinations rather than guessing ownership.
- Copy into staging under `<data-root>/projects/`, verify file hashes, then publish
  the directory and catalog row. Remove the selected original only after both
  succeed. Import is not an old-format migration or restoration of the old catalog.

Project databases separately carry `PRAGMA user_version`.
[Schema checks](../../packages/core/wenyi_core/storage/sqlite.py#L204) reject unknown
newer versions for both reads and writes. Keep the catalog entry visible as
unavailable while other projects remain usable. Project schema 2 uses the compact
chapter statistics written with each chapter; nonblank headings count as segments,
and an empty target counts as translated. Manifest, statistics and Review status
are read from one WAL snapshot, without decoding current-schema chapter bodies.
Writable opens of schema 0/1 atomically rebuild only this derived table and stamp
schema 2; interruption leaves the old version intact. Read-only legacy opens
continue to calculate from chapter bodies until that upgrade, without changing
data. Upgrades never bypass catalog or project-marker admission.

Deletion and recovery share directory ownership rules. Preserve the
[deletion sentinel](../../apps/desktop/backend/wenyi_desktop/local_backend.py#L795):
mark deletion intent, remove owned files, then remove the catalog row. Interrupted
deletion must be retryable; neither startup nor explicit import may resurrect a
directory marked for deletion.

## Concurrent workflows and shutdown

[LocalRuntime](../../apps/desktop/backend/wenyi_desktop/local_runtime.py#L191)
supervises independent parse, preparation, translation, chapter-translation and
Review workflows across projects. Durable job/run identity and the configuration
snapshot are recorded before dispatch; rejected admission is compensated.
Conflicting writes to the same project remain mutually exclusive.

Each invocation owns an async supervisor and a
[private single-thread executor](../../apps/desktop/backend/wenyi_desktop/local_runtime.py#L177).
Copy context into that thread. Do not substitute a shared fixed workflow pool:
the default executor must remain available for control requests and snapshots.
There is no Desktop workflow-count admission cap; this does not promise unlimited
memory, threads, throughput or provider quota. Per-client limits and retry policy
still apply, and shared credentials do not create an account-wide limiter.
Exports retain their separate bounded queue and two consumers; telemetry is bounded.

Runtime ownership spans **accepted/pending, running and cancellation/draining** work,
including supervisors that have not entered the task adapter. A terminal catalog
status alone does not mean the runtime is idle.

1. Register ownership before scheduling; duplicate dispatch of an owned or terminal
   run must not execute it again.
2. Give each run its own client, stop signal and storage. One run's pause/failure,
   including thread-allocation failure, must not cancel sibling projects.
3. On timeout or shutdown, cancel the async worker **once**. The
   [shared execution runner](../../packages/backend/wenyi_backend/execution/runner.py#L207)
   signals cooperative stop and shields the synchronous operation until its
   checkpoint writes finish. Repeated cancellation can interrupt that wait.
4. Stop admission before draining. Accepted work that has not entered execution
   becomes interrupted/resumable without starting model work; handle cancellation
   before the worker's first instruction as well.
5. Await active writers, supervisors, exports and progress persistence before
   releasing the workspace lock. Update admission must also include pending and
   draining ownership, even when catalog rows are already terminal.
6. After a crash, recover orphaned jobs into resumable/error state as appropriate;
   do not automatically restart all interrupted work.

Python threads are not forcibly killed; a thread that has not stopped remains owned.
The native host can terminate an unresponsive child process after its grace period.
That is forced recovery, not evidence that checkpoints were flushed; retain WAL and
apply the usual interrupted-run recovery on the next start.

Hiding/minimizing the window is not shutdown. Background activity suppresses periodic
UI work while leaving the engine and progress subscription alive. Restoring reconciles
saved state without discarding editor drafts; ordinary loss of focus is not minimization.

## Diagnostic contracts

Diagnostics are Desktop-owned, best-effort observations, **not project history, a
usage ledger or a project backup**. Clearing logs must not affect checkpoints,
Review artifacts or accounting. No report is uploaded automatically.

### Safe ingestion and correlation

The [Python projector](../../apps/desktop/backend/wenyi_desktop/diagnostics.py#L135)
and [native validator](../../apps/desktop/src/diagnostics.rs#L216) accept only
registered events, safe enums, bounded numeric fields and opaque identities.
Sanitize before queueing and validate again during export. An allowlisted field
name does not make arbitrary text safe.

Never collect original/translated text, prompts, model responses, glossary/Review
content, credentials, request bodies, raw configuration, raw exception messages,
tracebacks or filesystem paths. Safe failure observations may contain classified
exception/cause types, known OS/SQLite codes and allowlisted application module/line
locations, not exception stringification or traceback dumps. Do not attach arbitrary
stdout/stderr or third-party loggers to the persistent sink.

The native process is the **single file writer**. Python sends bounded
`WENYI_DIAGNOSTIC` frames through the private pipe; `WENYI_READY` stays a separate
protocol frame and arbitrary stderr is discarded. Frontend IPC accepts registered
event names from the trusted window, not arbitrary payloads or file-read requests.
Reuse task/run/model-call identities, explicitly propagate context into threads and
reset it after each operation. Desktop generates request/error IDs; caller-supplied
request IDs are not persisted. Export aliases session/project/run/call references.

`task_finished` means the adapter returned, which can include pause; the durable job
is authoritative. An unclean-session marker is not proof of a crash. Forced cleanup
and an observed engine exit must not be mislabeled as the original failure.

### Bounded persistence and export

Normal capture is INFO and above. Temporary detailed capture lasts up to 30 minutes,
can stop early and resets on restart; it never relaxes privacy or changes translation
configuration/cache identity.

Native persistence uses `<data-root>/logs/`. If disk persistence fails, keep a bounded
memory buffer rather than falling back to another platform directory. Files rotate
at 10 MiB or 24 hours; retention uses a 200 MiB limit and removes
owned files last written over seven days ago, protecting active/recently written
files. Foreign files count toward space usage but are never read into reports or
deleted by retention/clear. Logging failure must not block translation or hide its
original error; report dropped/degraded evidence.

[Native diagnostic saving](../../apps/desktop/src/native_export.rs#L351) must:

1. Select and admit the destination, including overwrite confirmation, **before**
   collecting or creating an archive. Picker cancellation creates no archive or
   book export task.
2. Read only owned diagnostic files up to complete-line watermarks; reject links and
   unsafe destinations. Compression must not hold project locks or catalog writes.
3. Revalidate safe fields, apply bundle-local aliases and disclose missing sources,
   dropped/malformed records, retained coverage and size omissions.
4. Verify the ZIP, write a same-directory temporary file and publish atomically.
   Cancellation/failure preserves an existing destination; cancellation is not an error.

Current reports select **current launch, last 24 hours or last seven days across the
workspace**. There is no project/run filter or custom time range. Reports contain
`manifest.json`, `report.md`, `system.json` and available per-source JSONL logs;
they do not contain project-event/configuration/usage summaries or copied databases.
The record-selection budget is 20 MiB and final ZIP cap is 25 MiB. A `complete`
manifest means no known collection loss from available sources, not full historical
coverage or a proven root cause.

The [native diagnostic controls](../../apps/desktop/src/diagnostics_ui.rs#L152)
must remain usable without the Python engine or React tree. Export and clear exclude
one another while logging can continue. A completely unlaunchable native executable
cannot export itself.

## Verification

Use temporary data roots and external libraries, fake credential stores/clients and
synchronization barriers, not real books or paid model/parser calls. Cover root
isolation, unsupported catalog refusal, current-format import without startup
discovery, concurrent projects, same-project exclusion, context identity, duplicate
delivery, thread-allocation failure, pending/draining shutdown, pause/resume without
duplicate usage, newer-schema refusal and diagnostic privacy/failure paths.

With the full Python development workspace installed:

```bash
uv run --no-sync pytest -q -n auto --dist loadgroup apps/desktop/backend/tests
uv run --no-sync pytest -q -n auto packages/core/tests/test_architecture_boundaries.py packages/core/tests/test_orchestrator_contract.py packages/core/tests/test_storage_injection.py
uv run --no-sync ruff check apps/desktop/backend
uv run --no-sync ruff format --check apps/desktop/backend
git diff --check
```

State, locking and recovery changes also require the full Python regression suite
and affected shared-backend tests. Include Rust tests for native changes and the
two-host checks from [Frontend development](frontend.md) for shared UI changes.
Diagnostic privacy tests share [Desktop fixtures](../../apps/desktop/diagnostic-fixtures.json#L1).
Browser IPC mocks do not validate packaged dialogs, tray behavior, process ownership
or OS-specific installation; perform that acceptance on each supported target and
report unavailable integration services separately.
