# Web development

This guide covers the Web application's development environment and backend contracts. Shared browser UI conventions belong in [frontend development](frontend.md); domain checkpoints, Review semantics, and usage accounting belong in [pipeline development](pipeline.md). User workflows are documented once in the [workspace guide](../guide/workspace.md).

<a id="local-development"></a>

## Local development environment

Use Python 3.10+, `uv`, Node 22, pnpm 9, PostgreSQL 16, and Redis 7. Run commands from the repository root. The default Python workspace installation only selects CLI/Core; Web development needs the full workspace:

```bash
uv sync --locked --all-packages --group dev
pnpm install --frozen-lockfile
```

Use `uv run --no-sync` afterward so a command does not resynchronize to the default package selection and remove the API dependencies.

Start dedicated development services, not a production database or Redis. The development overlay publishes only loopback ports:

```bash
test -f deploy/.env || cp .env.example deploy/.env
docker compose -p wenyi-dev \
  -f deploy/docker-compose.yml -f deploy/docker-compose.dev.yml \
  up -d postgres redis
```

`-p wenyi-dev` gives this stack its own named volumes. Ensure ports 5432 and 6379 are free, or adjust the local bindings and URLs. Reuse the project name and both Compose files for later service commands; stopping services must not delete data volumes.

Set the application environment in each backend terminal:

```bash
export DATABASE_URL=postgresql://wenyi:wenyi@localhost:5432/wenyi
export REDIS_URL=redis://localhost:6379/0
export DATA_DIR=./data/web-dev
export WENYI_CONFIG=config.yaml
```

Supply model keys to the API for model tests and to the workflow worker for model
tasks; supply `MINERU_API_KEY` to processes performing MinerU parsing. The export
worker needs the shared services/data and BabelDOC reachability for PDF exports,
not model or MinerU keys. Compose currently shares one `env_file` across its
containers, but does not export it into host shells. Keep `WENYI_API_TOKEN` and
`WENYI_CORS_ORIGINS` consistent with the browser setup for authenticated access.

Start these four processes in separate terminals:

```bash
# API
uv run --no-sync uvicorn wenyi_api.main:app --reload --port 8000

# Workflow queue
uv run --no-sync arq wenyi_api.workers.WorkerSettings

# Export queue
uv run --no-sync arq wenyi_api.workers.ExportWorkerSettings

# Browser frontend
pnpm -C apps/web dev
```

Open <http://localhost:5173>. Vite forwards `/api` to the API with that prefix removed and forwards `/ws` as WebSocket traffic. The API initializes the current database schema; it does not migrate legacy Web state. Workflow and export queues are independent: starting only the ordinary worker leaves exports queued.

<a id="mock-api"></a>

## Mock API development

To inspect the browser UI without API, database, worker, or model services:

```bash
MOCK_API=1 pnpm -C apps/web dev
```

Vite answers `/api` with the shared fixtures in `packages/ui/sandbox/fixtures.ts`, also used by the Playwright suites. Actions operate on mock state, not real projects. The mock does not supply the progress WebSocket; the UI uses its HTTP fallback. Leave `MOCK_API` unset for normal proxied development.

Mock success demonstrates browser behavior, not authentication, storage transactions, queue recovery, parser availability, or translation quality. Use the appropriate backend/integration tests for those guarantees. UI layout, localization, shared fixtures, platform isolation, and chunk-size rules are covered in [frontend development](frontend.md).

<a id="http-contract"></a>

## HTTP contract and generated schema

The shared HTTP application and schemas in `packages/backend/wenyi_backend` define the contract; `apps/api/wenyi_api` supplies PostgreSQL, Redis, and Arq adapters. Core does not depend on HTTP or those Web adapters, and the shared backend does not import a Web or Desktop host.

Direct API routes have no `/api` prefix. The Web proxy presents that prefix to the browser; `/ws/projects/{pid}/progress` remains the WebSocket route. Runtime inspection is available through `/docs`, `/openapi.json`, and `/capabilities`, subject to deployment authentication.

After changing routes or schemas, regenerate the contracts offline:

```bash
pnpm gen:schema
pnpm check:schema
```

Generation needs the installed Python/frontend dependencies, but no running API, PostgreSQL, Redis, or credentials. Schema-only application assembly does not enter the production lifespan or invoke runtime ports. Only the installation-dependent version field is normalized for comparison.

- `packages/shared-schema/src/api.d.ts` comes from `wenyi_backend/schema_export.py` and contains shared routes.
- `packages/shared-schema/src/desktop.d.ts` comes from `wenyi_desktop/schema_export.py` and contains Desktop's additional routes.

Do not hand-edit generated types or add Desktop credential/IPC routes to the shared contract. `check:schema` compares temporary outputs without rewriting tracked files.

### Authentication and errors

`WENYI_API_TOKEN` enables shared-token authentication, not individual user authorization. HTTP clients use Bearer authentication; the health route remains accessible without a token. CORS configuration applies to HTTP browser access and is not a WebSocket origin authorization mechanism. Public deployments also need the [network controls in the deployment guide](../guide/web.md#authentication-and-network).

For a progress subscription:

1. Connect to `/ws/projects/{pid}/progress`.
2. Send a JSON first message `{"token":"…"}` within ten seconds. Send it even with no configured token; the value may then be empty.
3. The server checks the configured token and project existence before sending snapshots or events. Its authentication-timeout, token-mismatch, and project-not-found branches close the connection with code `1008`.

Coded HTTP errors return `code`, `message`, and optionally `request_id`; they do not repeat the message in `detail`. Branch on `code`, not English text. Uncoded HTTP exceptions keep FastAPI's `{"detail": ...}` shape, which clients must also handle.

HTTP responses carry `X-Request-ID`; server failures also carry `X-Error-ID`. Correlate them with server logs. Docker's logging configuration emits JSON; `WENYI_LOG_FORMAT=json` selects it for other entry points. Internal tracebacks belong in operator logs, not project event responses; legacy event records are filtered on read too.

<a id="progress-contract"></a>

## Progress identity and live statistics

Treat project identity and run identity as part of the event contract:

- A subscription belongs to one project. Run-scoped progress, statistics, state, and error events carry `project_id` and `run_id`; clients must not apply another project's event or an older run's output to the current workflow.
- The initial `snapshot` carries project/chapter state; `heartbeat` is a transport frame, not a task update. Do not require these two frame types to have the same shape as run-scoped events.
- The most recent progress callback is cached in Redis for seven days and includes its run identity. Reconcile it against the latest non-export workflow, not merely the project ID. Stage text and counts come from real callbacks, not inferred completion of enabled settings.
- The server sends a heartbeat after 30 seconds without other frames. The shared client treats 60 seconds of silence as a dead connection, reconnects, and refreshes saved state. HTTP reads provide fallback/reconciliation; a socket is not the durable job ledger.
- Live statistics are temporary Redis snapshots tied to a run. Stale statistics must not keep advancing the displayed clock. Final cumulative usage and timing come from persisted state; transport snapshots and cache counters do not establish cost.

Tests that synthesize events must use the displayed project's actual current run identity. Include reconnects, stale events, and terminal-state reconciliation when changing progress behavior, rather than asserting only that a label changes.

<a id="execution-and-recovery"></a>

## Execution, locks, and recovery

### State and execution ownership

PostgreSQL is the only mutable Web project-state store. `DATA_DIR` holds uploads, parser resources/caches, and export artifacts, not JSON/SQLite copies of chapters, glossary, Review checkpoints, or usage. Storage adapters preserve the shared [pipeline invariants](pipeline.md).

The API admits durable jobs before enqueueing them. Workflow admission captures the effective configuration for that run, and workers execute that saved snapshot. Preserve project/run identity through admission, execution, callbacks, pause/resume, and terminal status; a late completion or duplicate delivery must not overwrite a newer workflow.

`BackendContext` injects application-owned repositories, storage, telemetry, model construction, and queue ports. ASGI requests, WebSocket handlers, worker entry points, and recovery loops bind their own context. `asyncio.to_thread` and AnyIO propagate it; raw threads need an explicit copied context. Do not introduce a process-global “current platform” selector or let a callback use another application's adapters.

Long workflows use a PostgreSQL **session advisory lock** for project execution ownership. State changes use short transactions and transaction-scoped advisory locks. A session lock can remain held during a model request without leaving a database write transaction open. Keep model requests, parsing, and rendering out of short state transactions.

Exports have a durable job/export identity and an independent execution lock. Their saved consistent snapshot allows rendering beside translation without holding the project's long-running write lock. Duplicate delivery must not regenerate a completed export.

Cancellation must drain synchronous execution and checkpoint writes before releasing execution ownership. Publish workflow terminal status only after leaving its run lock; successful completion also follows completion-event persistence and live-statistics cleanup. Seeing a terminal workflow must not leave a previous worker still owning the lock that protects edits.

### Recovery is not a total-runtime timeout

Each worker starts an independent asynchronous recovery loop, so maintenance can run even when all workflow slots are occupied. Every 30 seconds it examines queued/running job rows not updated for more than two minutes:

1. Try the appropriate project or export session lock without blocking. If another worker holds it, leave the job alone.
2. Re-read the job under that lock. For queued jobs, check the matching Redis queue before declaring delivery lost.
3. Mark confirmed orphaned workflows `interrupted`; pause the project only if this is still its latest workflow. Resume continues the original task type and saved progress.
4. Mark orphaned export jobs and export records `error`; the user creates a new export.

The two-minute age is a recovery-candidate threshold, not proof of failure and not a maximum legitimate run duration. Arq execution limits are separate from this recovery rule. Preserve the live-lock check when changing scheduling or recovery; a long model call can be healthy without updating the job row.

<a id="metrics"></a>

## Operational metrics

`GET /metrics` exposes Prometheus text under the same `WENYI_API_TOKEN` authentication as other protected HTTP routes. Configure a scraper's bearer token when authentication is enabled; Wenyi does not install a scraper.

| Metric | Meaning |
|---|---|
| `wenyi_tasks_total{status,code}` | Finished workflow tasks with `done`, `paused`, or `error` outcomes; excludes exports |
| `wenyi_llm_retries_total{provider,reason}` | Actual retry waits, deduplicated by call ID and attempt; exhaustion notifications are not additional retries |
| `wenyi_queue_depth` | Admitted, queued workflow and export jobs from the PostgreSQL job catalog; excludes running jobs |

API and workers must share a Redis database for counter visibility. Counters survive only as long as their Redis keys. Recent task/retry identities use a bounded 20,000-entry deduplication window, not an unlimited exactly-once history. Counter writes are best-effort and must not fail workflows. An unavailable counter store or job catalog returns HTTP 503 on scraping rather than a fabricated zero snapshot.

**Metrics are operational observations, not the durable token-usage or billing ledger.** Missing samples, Redis resets, and bounded deduplication make them unsuitable for reconstructing charges. See [pipeline development](pipeline.md) for durable accounting.

<a id="validation"></a>

## Validation requirements

Use temporary projects and fake model/parser services. Never use private books or real paid model requests as ordinary regression fixtures. For backend changes, start with affected tests and style checks:

```bash
uv run --no-sync ruff check packages/backend apps/api
uv run --no-sync ruff format --check packages/backend apps/api
uv run --no-sync pytest -q -n auto -m "not integration" packages/backend/tests apps/api/tests
git diff --check
```

Expand to the full Python suite for state, lock, resume, or cross-domain changes. Keep `-n auto` for offline tests. Changes to module boundaries require `packages/core/tests/test_architecture_boundaries.py`; pipeline assembly/dependency changes also require `test_orchestrator_contract.py`. Storage adapter changes require `test_storage_injection.py` and the affected real-backend tests.

### PostgreSQL and Redis integration tests

Provision isolated test services and set **both** `WENYI_TEST_DATABASE_URL` and `WENYI_TEST_REDIS_URL`. The PostgreSQL fixture creates and later drops a unique schema, and may create the `pg_trgm` extension. Redis tests write and clean up test keys. These protections do not make a production service an acceptable test target.

For example, after provisioning a disposable database and Redis on separate local ports:

```bash
WENYI_TEST_DATABASE_URL=postgresql://wenyi:wenyi@127.0.0.1:55432/wenyi_test \
WENYI_TEST_REDIS_URL=redis://127.0.0.1:56379/1 \
uv run --no-sync pytest -q -n 2 -m integration apps/api/tests
```

Use two integration workers to limit database connections, as CI does. Missing service variables cause relevant tests to skip: report those skips explicitly, not as successful storage/queue validation.

### Schema and browser checks

For API contract, authentication, or progress changes, check both backend and browser behavior:

```bash
pnpm check:schema
pnpm -C apps/web typecheck
pnpm -C apps/web typecheck:tests
pnpm -C apps/web build
pnpm -C apps/web exec playwright install chromium
pnpm -C apps/web test:e2e
```

Playwright starts a loopback Vite server on port 4173 and uses mocked HTTP/WebSocket responses for browser contracts; this does not replace service integration tests. Shared UI changes also require Desktop typecheck/build/E2E, followed by `pnpm test:frontend-boundaries` after both builds; see [frontend development](frontend.md). Report real-model quality evaluation separately from offline and browser checks.

<a id="docker-builds"></a>

## Docker development builds

Ordinary deployment Dockerfiles do not require Buildx cache mounts. The backend image installs only the Web dependency closure, not the CLI/Desktop runtime. Keep that platform boundary intact when changing packaging.

For optional cached builds, install the Buildx plugin, then run from `deploy/`:

```bash
DOCKER_BUILDKIT=1 COMPOSE_BAKE=true docker compose \
  -f docker-compose.yml -f docker-compose.buildx.yml build &&
  docker compose -f docker-compose.yml -f docker-compose.buildx.yml up -d --no-build
```

The overlay selects cached Dockerfiles and retains service configuration and volumes. Keep ordinary and cached installation steps aligned. Connecting build and startup with `&&` prevents an unsuccessful build from silently starting an old image.

The Web proxy uses Docker DNS to re-resolve the API upstream for HTTP and WebSockets, with a five-second DNS cache. Its runtime needs Nginx 1.27.3 or newer for dynamic upstream resolution. When changing proxy or image configuration, verify that recreating the API container does not require restarting Web.
