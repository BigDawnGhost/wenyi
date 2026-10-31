# Desktop application logs and diagnostic exports

[简体中文](../zh/design/application-diagnostics.md) · [Implemented architecture](../architecture.md)

## Status and recommendation

**Desktop foundation implemented; extended design remains incremental.** The initial review used `201158c`. See [the user guide](../desktop.md#diagnostics-and-feedback) for shipped behavior. Sections below retain the broader target requirements, not a claim that every planned control or observation is complete.

Implemented: native-owned bounded persistence/rotation and memory fallback; safe native/engine/frontend records; task/call/error correlation; temporary detailed capture; settings and failure-screen export; native menu/shortcut and early-failure export; native destination admission and verified atomic ZIP publication. The native process is the **single writer**: Python submits bounded, separately validated `WENYI_DIAGNOSTIC` frames over the existing private pipe, preserving the `WENYI_READY` frame format and discarding arbitrary stderr. The ZIP separates sources; disk files are mixed-source session files.

Still planned: project/run filtering, arbitrary date ranges, configuration/project/usage summaries, richer environment metadata, phase/checkpoint heartbeat coverage, general repeat aggregation and error-prioritized selection. Current export selects current launch/24 hours/7 days across the Desktop workspace, with a 20 MiB record budget and 25 MiB ZIP cap. Task completion is observed as `task_finished` (return can mean success or pause); the durable job remains authoritative. Raw event files and ledgers are never copied.

Use **local-first, structured, privacy-safe application logs**, with a user-triggered diagnostic ZIP, **for Desktop only**. Cover the native shell, local Python engine and Desktop frontend, including failures before the engine or React UI starts. Do not automatically upload reports.

CLI commands, Web browser reports, server/container log collection and cross-platform diagnostic APIs are out of scope, not later phases of this plan. Keep implementation in Desktop-owned adapters; extend shared code only where Desktop actually needs an optional observation hook, without changing CLI/Web logging behavior.

| Approach | Trade-off | Decision |
|---|---|---|
| Save all console output and ZIP the workspace | Easy collection, but exposes credentials, book content and unrelated data; misses structured correlation | Reject |
| Extend project events to include every application failure | Reuses storage, but cannot cover failures before a project/database exists and confuses retention with durable history | Reject |
| Separate application logs, project events and usage; export safe projections | More explicit boundaries, bounded storage, useful cross-layer diagnosis | **Recommend** |
| Introduce a remote crash-reporting/observability service | Additional privacy, deployment and availability obligations | Not required for this plan |

## 1. Foundations and original gaps

- [Project event API](../../packages/backend/wenyi_backend/routers/events.py#L13) reads stored task events. It is not a process-wide logging service.
- [Desktop process launch](../../apps/desktop/src/process.rs#L112) deliberately discards engine stderr because arbitrary output may contain credentials. [Engine startup](../../apps/desktop/backend/wenyi_desktop/desktop.py#L129) reserves stdout for the ready protocol and redirects other output to stderr. Simply redirecting stderr to a file would remove an existing security boundary.
- Native startup originally lacked an engine-independent export path. [Native diagnostic controls](../../apps/desktop/src/diagnostics_ui.rs#L1) now own that path.
- [LLM routing](../../packages/core/wenyi_core/llm/router.py#L169) already provides `call_id`, operation, attempt, routing and usage events. [Retry reporting](../../packages/core/wenyi_core/llm/retrying.py#L272) already classifies retries without recording request/response bodies. Reuse these identities and classifications.
- Existing credential-scoped error protection is **not universal**: [the unscoped provider path](../../packages/core/wenyi_core/llm/router.py#L260) can still re-raise SDK exceptions, and [task failures](../../packages/backend/wenyi_backend/workers/tasks.py#L292) can persist `str(error)`. Existing events and errors cannot be assumed safe for an archive.
- [Shared HTTP decoding](../../packages/ui/src/lib/http.ts#L2) keeps its existing contract. Desktop adds error-ID presentation in its adapter; UI error boundaries invoke an optional Desktop observation hook without forwarding exception content.
- [Backend context](../../packages/backend/wenyi_backend/context.py#L152) and [UI platform services](../../packages/ui/src/platform.ts#L33) already provide the correct seams for injecting Desktop-only diagnostics without importing Desktop implementations into shared code.

### Three separate records

| Record | Question answered | Authority and lifecycle |
|---|---|---|
| Application diagnostics | Why did startup, UI, I/O, task execution or a dependency fail? | Best-effort, bounded, rotated; never used to resume work |
| Project events | What happened to this project/run? | Existing Storage-owned history; no retention or semantic changes in this feature |
| Usage ledger | How much model usage was actually incurred? | Existing durable, once-only accounting; logs are not a second ledger |

Export only safe projections of the latter two. Clearing application logs must not change any project, checkpoint, Review artifact or usage balance.

## 2. User workflow

### Desktop entry points

Add **Settings → Diagnostics** with four actions:

1. **Export diagnostic report** — normal support path.
2. **Copy troubleshooting summary** — version, platform, error ID, failure category and suggested next step; no filesystem paths or document names.
3. **Enable detailed logging for 30 minutes** — explicit temporary DEBUG capture, countdown and manual stop; resets on restart. The privacy policy is unchanged.
4. **Open log folder / Clear application logs** — clearing needs confirmation, affects only diagnostics, and coordinates with active writers/exports. Show the actual local location and storage usage in the UI, not in copied reports.

Task errors, export errors and startup error screens offer the same export action, preselecting the relevant error/project/run. The project event page links to it rather than becoming the global log viewer. Do not build a full log-search console in the first release.

The export dialog shows time range, scope, estimated size and included/excluded categories. Default to the last 24 hours; allow the current session, previous retained sessions, or a range within retention. A project error selects that project; the general settings entry defaults to application-only. Application-only includes global lifecycle/infrastructure events; project scope adds only the selected project's task records and optional safe project-event/usage summaries. Never silently include other projects' records.

Choose a native destination **before collection or archive creation**. Canceling creates no diagnostic job or temporary archive. After selection, allow cancellation with temporary-file cleanup; publish only a complete ZIP using a same-directory temporary file and atomic replacement. Confirm overwrite. Diagnostic exports are not book export tasks and do not consume book export history.

Show a manifest-backed preview and an explicit warning that version/runtime/timing metadata can still be identifying. Users can inspect the archive locally before attaching it to an issue. Do not automatically open an issue with report content in a URL, send a network request, or upload an attachment.

### Failure paths must work

- Add a native menu/dialog export entry independent of Python, React, the project catalog and workspace locks. It can produce a partial native/engine-log report even when startup fails.
- Register a minimal Python bootstrap logger before importing the application and heavy dependencies. Keep the ready protocol unchanged. Do not capture arbitrary stdout/stderr; unsupported early crashes have native startup/exit metadata only.
- Install frontend error hooks before React mounts. Collect route/root boundary failures, `error` and `unhandledrejection` through a bounded, safe adapter, not a blanket `console.*` recorder.
- Detect a previous session without a clean shutdown marker and label it **unclean exit**, not a proven crash. SIGKILL, power loss, interpreter/native aborts and a destroyed WebView can lose buffered records. No automatic memory/core dumps.
- If all persistent log locations fail, retain a bounded in-memory summary and disclose missing evidence. A completely unlaunchable native executable cannot export itself; document manual retrieval of its existing log directory.

## 3. Log granularity

Severity and detail are separate: INFO is the default capture level, DEBUG temporarily adds detail. Categories select sources; they do not weaken redaction.

| Level | Meaning | Examples |
|---|---|---|
| ERROR | A requested operation failed | Task failure, save failure, exhausted retries |
| WARN | Recoverable degradation or a condition requiring attention | Retry wait, fallback, slow storage, unavailable credential store |
| INFO | Meaningful lifecycle/result, not repetitive progress | Start/stop, accepted task, phase result, completed logical model call |
| DEBUG | Bounded implementation detail without content | Batch boundaries, cache decisions, safe stack frames, normal request timing |
| CRITICAL | Application or persistence service cannot continue | Fatal startup failure, unavailable workspace; best-effort flush |

User cancellation/pause, save-dialog cancellation and expected edit conflicts are not ERROR. Log a failure once at its owning boundary; upstream layers reference its `error_id`, rather than repeating the same stack.

| Area | Default INFO/WARN/ERROR | Additional DEBUG | Never record |
|---|---|---|---|
| Native shell / engine | Session start/end, engine spawn/ready/exit, readiness duration, exit status, tray/graphics/update/plugin outcomes | Safe lifecycle step timings and state transitions | Ready payload/token, raw subprocess output, panic payload |
| UI | Unhandled/render failures, mutation outcome, disconnected/reconnected state | Route identifiers, bounded action breadcrumbs, request timing | DOM, input values, clipboard, screenshots, complete URL |
| HTTP / IPC / WebSocket | Mutating operation result, failed or slow requests, authentication rejection, connection transitions | Successful reads and IPC timings | Headers, query strings, bodies, auth packets, arbitrary IPC arguments |
| Task / phase | Admission/start/pause/resume/completion/failure; phase start/end, duration and counts | Batch/chapter boundaries, cache hit/miss reasons, checkpoint timing | Paragraphs, chapter titles, translations, glossary terms, Review evidence |
| LLM | One terminal summary per logical `call_id`; retry/fallback/exhaustion, duration, attempt count and usage counts | Attempt start/end, quota/queue waits and route decisions | Prompts, messages, model output, SDK exception text or response body |
| Storage / export | Safe migration/recovery result, lock conflict, failed/slow I/O, export format/size/result | Lock wait/hold timing and checkpoint/snapshot identifiers | SQL parameters, database dumps, absolute paths, source/output filenames |
| Credentials / configuration | Validation outcome, key availability/source category, safe config revision summary | Names of changed allowlisted options | Key value, environment dump, raw YAML/JSON, custom headers or URLs |

Concrete volume rules:

- Do not log each token, segment, progress tick, poll or render at INFO. Batch details are DEBUG; phase completion records aggregate counts.
- During active work, emit a content-free summary at most once every 60 seconds: stage, completed count, in-flight count, longest in-flight `call_id`/elapsed time and last checkpoint age. This distinguishes slow requests from stopped execution without enabling DEBUG or making new model requests.
- Default slow threshold: HTTP/IPC/storage operation above 2 seconds produces WARN; log lock wait separately from lock hold time. Long-running task endpoints and model calls use their configured timeout/phase summaries, not a universal 2-second warning.
- Deduplicate repeated identical failures using category/code/safe frame fingerprint and scope. Keep the first event and an aggregate at most once per 60 seconds, including count and first/last times. Never merge different projects or runs. Always attempt to retain a distinct terminal failure.
- Temporary DEBUG does not enable third-party SDK/HTTP body tracing. Bound DEBUG streams; disclose sampling and drops.

## 4. Structured record and correlation

Use UTF-8 JSON Lines, one complete object per line. Human-readable text is generated from the same record, not maintained as a second log stream. Messages/event names are stable English templates; UI labels and summaries may be localized.

Required envelope: `schema_version`, UTC `timestamp`, `level`, `event`, `event_id`, `component`, `app_version`, `session_id`, `process_instance_id`, per-process `seq`, and allowlisted `attributes`. `message` is optional fixed template text. Use monotonic clocks for `duration_ms`, not wall-clock subtraction.

| Context | Rule |
|---|---|
| `session_id` | Desktop shell creates one launch identity and passes it to its engine/frontend. No permanent machine identifier. |
| `process_instance_id`, `seq` | Distinguish restarts and order each process; PID alone is insufficient. Cross-process time order is approximate. |
| `project_id`, `run_id` | Reuse Desktop's existing opaque identities when present, never a title/path-derived name. Do not change resume identity or persist new book metadata for logging. |
| `request_id` | Validated/engine-issued correlation for one loopback HTTP request; return via `X-Request-ID` and expose to the Desktop frontend through CORS. Do not confuse it with a provider request ID. |
| `call_id`, `attempt` | Reuse the existing logical model call identity across retry/fallback. Do not create a competing LLM trace ID. |
| `error_id` | Generated at the failure-owning boundary; shown in Desktop and linked through loopback HTTP/task/native records. A later independent failure gets a new ID. |
| Optional scope | Registered operation, phase, numeric chapter/batch index, export/review identity; never display titles or content |

Carry request context into accepted local-task metadata, restore it at local runner entry, and explicitly copy context into raw threads/executor submissions. The Desktop host binds diagnostic context; Core never imports Backend or Desktop to obtain it. Context is captured **before queueing**, so the listener thread cannot accidentally use another task's context. Reset scope on completion and test simultaneous projects and isolated Desktop workspaces.

Keep the loopback error response's existing user-facing `detail` compatible while adding structured `code`, `error_id` and request correlation where applicable through the Desktop adapter. Reject invalid/unbounded incoming IDs and replace them with generated IDs. Do not trust frontend-supplied engine/run identities without checking them against the authenticated operation. This does not require changing the Web error contract.

## 5. Privacy is an ingestion rule, not a ZIP cleanup step

1. **Allowlist event schemas and field types.** Prefer registered enum values, numbers, booleans and opaque references. Unknown fields are omitted. Arbitrary strings are not safe merely because their field name is allowed.
2. **Sanitize before queues, files, console sinks or crash hooks.** Queue only immutable safe records, never raw `LogRecord` objects retaining `args`, exceptions or request objects. Unknown library messages become a category/count or are omitted; do not attach an unrestricted root logger to the persistent sink.
3. **Safe exceptions preserve diagnosis, not content.** Store allowlisted exception category/code, status, retryability and package-relative frame/function/line locations. Exclude `str(error)`, cause messages, locals and source-code lines unless a specific safe classifier creates fixed text. Normalize JS stack asset paths and strip URLs; omit unrecognized frames. Rust panic payloads are also untrusted.
4. **No content in any mode.** Exclude originals, translations, prompts, model responses, glossary entries, Review evidence, arbitrary progress labels and user-authored config text. Private project/source names and content hashes are not diagnostic identifiers.
5. **Normalize configuration.** Include registered provider kind, public model identifier only when allowlisted, selected operation/routes, timeout/retry/concurrency limits and enabled features. Alias custom connection/model names and endpoints; expose only endpoint class (built-in/custom/local), not host, path or URL. Report credential availability/source category without reading credentials for export.
6. **Pseudonymize export identities.** Map project/run/session/call/custom-label references consistently within one bundle, with a new random bundle scope. Do not include the reverse map or its secret. Provider request IDs are untrusted external strings: retain only an opaque correlation reference, not the raw header. Preserve generated `error_id`/`request_id` for support lookup.
7. **Validate again on export.** Old event payloads, unknown schemas and existing `error` strings are not copied verbatim. Project/usage snapshots are newly constructed allowlisted views, not redacted database/config dumps.

Known-secret replacement is defense in depth, not the primary guarantee; it cannot protect book text or unknown credentials. Use owner-only file/directory permissions where supported and suitable per-user Windows ACLs. Do not claim that pseudonymized logs are anonymous. First release has no “include raw data” toggle; content-specific translation problems require a separately consented minimal reproduction, preferably public-domain text.

## 6. Persistence and failure behavior

Recommended initial limits, to be validated under load:

| Item | Default |
|---|---|
| Desktop location | `<workspace>/diagnostics/`, outside project stores; early bootstrap/fallback uses the native per-user application log directory |
| Local retention | 7 days **or** 200 MiB per log root, whichever removes records first; total includes active files and session markers |
| Rotation | 10 MiB per file; include session/process/component in names; exactly one writer owns each file |
| Detailed mode | 30 minutes, manual stop, resets on restart; same retention/size budget |
| Queue / record | At most 4,096 records per process, 512 slots reserved for WARN+; at most 16 KiB per serialized record |
| Memory fallback | At most 256 sanitized records **and** 1 MiB per process |
| Flush | At most 1 second normal buffering; shutdown/fatal drain has a 2-second deadline; no per-event `fsync` |
| Diagnostic ZIP | At most 100 MiB selected uncompressed data and 25 MiB final archive |

Desktop entry points choose paths and instantiate/close writers explicitly; importing Core does not configure logging or create files. Never allow multiple processes to rotate the same file. One maintenance owner per root coordinates cleanup, readers and writers using a **diagnostics-only** lock. If active files prevent reclamation at the cap, degrade capture rather than grow indefinitely or delete active files. Independent bootstrap and workspace roots each have the stated cap; expose both in storage accounting.

Queue saturation evicts/drops DEBUG then INFO; reserved capacity only improves WARN/ERROR delivery, it does not guarantee it. If high-priority capacity is also exhausted, increment bounded drop counters. Emit a loss summary when healthy again and include counters in the export manifest. Do not block a workflow waiting for a log slot or perform synchronous fallback disk writes while a domain lock is held.

Permission errors, disk-full conditions, encoding problems and writer failure must not stop translation or mask its original error. Switch to the bounded memory buffer, show one nonrecursive degraded-logging notice, and retry storage with bounded backoff. Never log the logging failure through the failing sink in a loop.

## 7. Diagnostic bundle contract

Suggested filename: `wenyi-diagnostics-<UTC>-<short-bundle-id>.zip`; no project title or username.

```text
manifest.json             # schema, scope, windows, completeness and file checksums
report.md                 # readable summary and copy-ready issue checklist
system.json               # app/build, OS/architecture, Python/WebView/plugin versions
config-summary.json       # allowlisted effective options, never the raw config
logs/native.jsonl          # available selected sources only
logs/engine.jsonl
logs/frontend.jsonl
project-summary.json      # optional selected project: format, counts, status, run references
project-events.jsonl      # optional safe event projection
usage-summary.json        # optional counts, not a copied ledger
```

Collect only the selected Desktop workspace/session sources. Missing sources are omitted and listed in the manifest. System metadata excludes hostname, username, environment variables and full filesystem paths. No originals, SQLite/WAL files, source caches, whole project folders, raw manifests/configs, credential stores, exported books, screenshots or crash dumps.

Manifest records:

- Bundle/schema/app versions; selected time window/project scope and active detail mode.
- Per-source capture window and high-watermark; collection start/end times. This is a bounded diagnostic observation, **not a transactionally consistent project backup**.
- Included files, SHA-256, byte/record counts; redacted/omitted fields and unknown event types.
- Missing/unreadable sources, queue losses, retention gaps, malformed/truncated lines, sampling and size truncation; explicit `complete` or `partial` status.
- Aggregated failure categories and generated error IDs; no raw exception messages.

Collection algorithm:

1. After destination confirmation, validate scope and acquire a short diagnostics snapshot lease. Pin relevant files and byte offsets; read complete lines only up to each watermark. Rotation/cleanup must not remove a pinned source. Stream records instead of loading entire files.
2. Obtain optional project summaries through existing read-only Storage/repository APIs with short read scopes. Do not hold project locks/SQLite write transactions during file reading, redaction or compression. If bounded time/query capabilities are missing, add a limited read projection; never read every chapter/database row just to truncate afterwards.
3. Apply schema projection and bundle-local aliases. Inspect only files owned by the diagnostics subsystem; reject symlinks/path traversal, never recursively walk the workspace, and use fixed archive entry names.
4. Prioritize failure records and surrounding context, then lifecycle/phase summaries, then INFO, then DEBUG. Respect both size limits and record omitted windows/counts. Build the readable report from the same filtered data, not independently from private state.
5. Verify entries/checksums and the manifest, flush the temporary ZIP, then atomically publish. Failure leaves no successful-looking partial file; cancellation releases leases and removes only this export's temporary files.

Bound each optional source read to 5 seconds and total collection to 30 seconds. Slow/unavailable sources yield a clearly marked partial bundle. A projection that cannot be proven safe is omitted; a failure to validate the final archive prevents publication. Report checksum verification is integrity evidence, not encryption or proof of anonymity.

`report.md` answers: version/platform, failure/error ID, relevant phase, first/last time, retry/fallback history, last safe progress, missing evidence and suggested next action. Leave reproduction steps, expected/actual behavior and whether detailed logging was enabled as a user-completed issue checklist. Do not infer a root cause from a timeout alone.

## 8. Desktop ownership and interfaces

**Keep the implementation Desktop-owned; do not prebuild a cross-platform logging framework:**

- Native shell: owns bootstrap/session context, native structured writer, panic/child-exit metadata, UI-independent collection, destination capability and atomic ZIP publication. It is the export coordinator even when the engine is unavailable.
- Python engine: Desktop-owned diagnostic modules handle safe event projection, bounded pipe delivery and loopback/local-task context. Reuse existing LLM events; future summaries use read-only local repository/Storage APIs. The native writer owns persistence and rotates mixed-source files; there are no competing engine file writers, changes to book state or database copies.
- Desktop frontend: owns the diagnostics settings section, pre-React error hooks, temporary detail controls, safe breadcrumbs and native export invocation. Do not store diagnostics in WebView browser storage or use browser downloads.
- Shared code: reuse existing Backend context and UI platform injection. Add only the smallest optional hook needed by an actual shared task/error component; when no Desktop diagnostic adapter is configured, retain existing behavior. Core and shared Backend/UI must not import Desktop modules. Do not add a Core diagnostics directory or new package solely for hypothetical reuse.
- Desktop transport: IPC validates event names, field types, size, rate and trusted origin; it accepts no arbitrary file path/read request. Optional engine summary endpoints belong only to the authenticated Desktop loopback application, not shared routers. Rust/Python/TypeScript use common Desktop schema fixtures and privacy tests rather than embedding one runtime in another.

The native fallback collector uses existing sanitized files and can omit Python-generated config/project summaries. It must not bootstrap the translation engine solely to export a startup failure. Reuse native save capability and atomic-publication patterns, not book export job admission.

Keep log settings in Desktop diagnostics configuration, not translation config snapshots: changing verbosity must not invalidate caches or alter semantic fingerprints. No diagnostic command-line surface, Web download endpoint or remote collector is planned.

## 9. Incremental delivery and acceptance

| Phase | Scope | Exit criterion |
|---|---|---|
| P1 — Desktop support MVP | Desktop safe schema/projection; native/Python/frontend capture; bounded rotation; error IDs; default and 30-minute detailed modes; export dialog and native startup-failure fallback | A user can reproduce a failure and export useful, content-free evidence without a console or functioning engine |
| P2 — Desktop diagnostic depth | Complete phase/call/storage observations; safe selected-project projections; context propagation and long-call summaries | Maintainer can connect Desktop UI/loopback request → local task → model retry/storage failure; concurrent projects/workspaces cannot contaminate one another |

P1 should reuse already available task/LLM metadata, not postpone essential privacy or error correlation until P2. Build the happy path and startup/permission failure paths together.

Required acceptance scenarios:

1. Failure before engine readiness, engine unexpected exit, Python import failure, React render/rejection failure, save failure, update failure, and credential-store fallback all leave classified evidence and an export path where the native host can run.
2. Synthetic secrets/private text placed in exceptions, nested payloads, header-like values, provider request IDs, paths, config labels, panic data and legacy events appear in **neither queued records, files, summary, ZIP contents nor ZIP filenames**; default and DEBUG behave identically.
3. LLM 401/429/5xx/timeout/retry/fallback use fake clients; no real paid model or parsing service calls. Safe stack metadata and status/attempt/timing data remain useful.
4. Concurrent projects/apps/threads retain their own context; retry preserves `call_id`; export does not mutate checkpoints, repeat usage, change task status or hold write transactions during compression.
5. Rotate while exporting, disk full, denied permission, full queues, malformed UTF-8/last JSONL line, clock changes, stale session markers and process kill; memory/disk remain bounded and missing evidence is disclosed.
6. Cancel before/after destination selection, overwrite refusal, symlink escape, failed ZIP write, source timeout and archive size limits; no unauthorized file access or partial final artifact.
7. Windows/macOS/Linux native save/bootstrap behavior; a native-menu export works with the engine unavailable. An entirely unusable log directory yields a partial memory/native report or an actionable failure, never silent success.
8. Untrusted WebView origins and unauthenticated loopback clients cannot submit or read diagnostics. Without the Desktop adapter, shared components retain existing behavior and expose no diagnostic UI/endpoints. CLI/Web logging remains unchanged.

Implementation validation must include affected Python tests plus Ruff check/format and `git diff --check`; architecture boundaries and context-isolation tests; Rust tests and packaged native startup/save acceptance; Desktop frontend typecheck/build and affected Playwright E2E. Shared UI changes also require both frontend typechecks/builds, affected E2E and frontend-boundary checks as regression protection, not a Web feature deliverable. Any changes to storage adapters or pipeline wiring trigger the repository's additional storage/orchestrator checks and full high-risk regression suite.

Keep Markdown links, source references and both languages aligned as implementation progresses. Validation results must distinguish offline tests and browser IPC mocks from packaged Windows/macOS/Linux native-dialog acceptance. No real-model quality or load result is implied by the numeric budgets.
