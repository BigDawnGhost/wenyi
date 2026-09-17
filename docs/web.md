# Web deployment and development

[简体中文](zh/web.md) · [CLI usage](usage.md) · [Configuration](configuration.md)

The Web stack uses React/Vite, FastAPI, Arq, PostgreSQL, and Redis. Ordinary workflows and exports are consumed by two independent workers. Domain logic comes from `wenyi_core`; the Web path does not copy CLI file-state layouts into the database.

## Fresh Docker deployment

Requires Docker and Compose v2. From the repository root:

```bash
cp .env.example deploy/.env
# Edit deploy/.env and set credentials for providers used in config.yaml.
docker compose -f deploy/docker-compose.yml --profile full up --build
```

| Service | Address / role |
|---|---|
| Web | http://localhost:8080 |
| API / OpenAPI | http://localhost:8000 / http://localhost:8000/docs |
| `worker` | `wenyi:workflows`: parse, prepare, translate, review, SRT |
| `export-worker` | `wenyi:exports`: snapshot export |
| PostgreSQL / Redis | Internal network only by default |

Backend-only mode uses `--profile server` for a custom frontend or API client. Local CLI still runs the core itself and does not call this server.

```bash
docker compose -f deploy/docker-compose.yml --profile server up --build
# Add -d to run in the background. Stop while keeping data:
docker compose -f deploy/docker-compose.yml --profile full down
```

This database schema targets fresh deployments. It does not migrate legacy Web projects, strategies, or databases. When keeping an older deployment, give the new stack a separate Compose project, database, and volumes (for example `-p wenyi-new`). Do not delete volumes that still hold data you need.

## Shared configuration and credentials

API, workflow worker, and export worker all load `deploy/.env`, mount the same read-only `config.yaml`, and share `/data`. Rebuild related containers after provider environment variables change. New tasks pick up updated config defaults; explicit project settings still win.

| Variable | Purpose |
|---|---|
| `DEEPSEEK_API_KEY` and peers | Set credentials only for providers that actual routes use. For custom providers, add the name referenced by `api_key_env` in `.env`. |
| `MINERU_API_KEY` | Required only when MinerU PDF parsing is used. |
| `WENYI_CONFIG` | Core config path. Containers use `/app/config.yaml`; local default is `config.yaml`. |
| `WENYI_CONFIG_FILE` | Host config file for Compose; default `../config.yaml` relative to `deploy/`. |
| `DATA_DIR` | Uploaded originals, parser caches, and export artifacts. Containers use `/data`. |
| `DATABASE_URL` / `REDIS_URL` | Local development URLs; Compose overrides them to internal service addresses. |
| `INSTALL_PDF_OUTPUT` | Docker build arg, default `true`, installs WeasyPrint and fpdf2. Set `false` to skip Python PDF output dependencies. |
| `WENYI_API_TOKEN` | Optional static token. HTTP uses Bearer auth; WebSocket authenticates with the first post-connect `{"token":"…"}` message (the frontend sends it automatically). |
| `WENYI_CORS_ORIGINS` | Allowed origins, comma-separated; default `*`. |

Custom WebSocket clients must send a JSON first packet `{"token":"…"}` within 10 seconds of connecting. Send the packet even when the server has no token configured (the token may be empty). Project snapshots and live events are delivered only after authentication succeeds.

PostgreSQL stores projects, chapters, segments, glossary, review evidence/checkpoints, Autofix publish indexes, subtitle caches, usage, and timing. `DATA_DIR` does not keep JSON/SQLite copies of that mutable state.

### PDF

Backend images include WeasyPrint/fpdf2, required Pango libraries, Noto CJK/generic fonts, and WenQuanYi Zen Hei by default. fpdf2 only uses fonts with TrueType outlines and skips OpenType/CFF faces such as Noto CJK automatically; set `TRANS_NOVEL_PDF_FONT` to force a compatible font. Choosing an incompatible font explicitly returns an error instead of producing a broken file. WeasyPrint can use Noto CJK. MinerU is the default parse backend; parse caches are shared with upload previews.

BabelDOC runs as a separate HTTP bridge. Set `pipeline.pdf_backend` to `babeldoc` and point `pipeline.babeldoc_bridge_url` at an address reachable from the API and both workers. `127.0.0.1` inside the bridge means that container, not the host. This Compose file does not download or start BabelDOC. The UI only exposes PDF features that match installed dependencies and configuration.

## Local development

Requires Python 3.10+, `uv`, Node 22, pnpm 9, PostgreSQL 16, and Redis 7. Start the databases first. The development overlay binds loopback ports on the host:

```bash
cp .env.example deploy/.env
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.dev.yml \
  --profile server up -d postgres redis
uv sync --all-packages --group dev
pnpm install --frozen-lockfile
export DATABASE_URL=postgresql://wenyi:wenyi@localhost:5432/wenyi
export REDIS_URL=redis://localhost:6379/0
export DATA_DIR=./data
export WENYI_CONFIG=config.yaml
export DEEPSEEK_API_KEY=your-key
```

Use the same application environment in four terminals:

```bash
uv run uvicorn wenyi_api.main:app --reload --port 8000
uv run arq wenyi_api.workers.WorkerSettings
uv run arq wenyi_api.workers.ExportWorkerSettings
pnpm -C apps/web dev
```

The web app runs at http://localhost:5173. Vite proxies `/api` and `/ws` to the API. The API initializes a fresh database schema. Starting only the ordinary worker does not consume the export queue.

## Workflow

1. Create a project, choose source/target languages, and upload EPUB, DOCX, FB2, TXT, Markdown, HTML, PDF, or SRT.
2. Parsing runs as a background task after upload and then shows a preview. Matching parse results are reused during preparation.
3. Choose standard, fast-draft, or custom steps, or edit common settings and advanced YAML on the config page. Validate and preview actual model routes before submit.
4. Start the run and watch the progress page. After a safe-boundary pause, resume continues the actual task type.
5. For books, edit glossary, style, and paragraphs, and inspect whole-book review history, suggestions, and published fixes. For SRT, edit subtitle cues and timestamps.
6. Export with format and monolingual/bilingual options. An independent worker reads a saved snapshot. Each export has its own file location and can be downloaded when done.

The event log displays the newest entries first and refreshes every 5 seconds.

Standard mode enables pre-understanding, polishing, review, and Autofix by default. Fast draft turns those four off. Matching review fingerprints reuse a completed result or resume an interrupted run. Turning Autofix off keeps suggestions without publishing them to formal chapters.

Writes in the same project are exclusive: duplicate starts or conflicting edits while a run is active return clear errors. Exports use a short consistent snapshot and can run beside translation. After a project is initialized, changing target language or source content means creating a new project.

### Recovery after a worker exits abnormally

Workers start an independent async recovery loop that checks every 30 seconds for queued/running jobs that have not been updated for more than 2 minutes. Recovery first tries the matching session lock to confirm no live worker still holds it; a healthy long-running job is not marked lost merely because it has been running for a long time. Queued jobs are also checked against Redis queue state.

After a job is confirmed orphaned, ordinary workflows are marked interrupted and the project's latest workflow status becomes `paused`, so **Resume** can continue the original task type and saved progress. Export jobs and export records become `error`; create a new export to retry. Exports keep a durable job identity and an independent execution lock so duplicate deliveries do not regenerate files and so recovery can restore status after a crash.

## APIs, checks, and troubleshooting

The live `/openapi.json` is the source of API types. `GET /capabilities` reports languages, formats, providers, and registered operations. Generate frontend types with:

```bash
pnpm gen:schema  # API already running on localhost:8000
uv run ruff check packages/core packages/cli apps/api
uv run pytest -q
pnpm -C apps/web typecheck
pnpm -C apps/web build
pnpm -C apps/web exec playwright install chromium
pnpm -C apps/web test:e2e
```

Set `WENYI_TEST_DATABASE_URL` to run real PostgreSQL integration tests. Those tests create an isolated schema and drop it afterward; use a dedicated test database. CI also provides Redis.

- Jobs stay queued: confirm ordinary/export workers share the API Redis and listen to the matching queues.
- PDF parse fails: check credentials, bridge URL, and service health for the selected backend.
- Missing model credentials: validate provider environment variables for the operations that are actually routed; unused providers need not be filled.
- Local database unreachable: use `docker-compose.dev.yml` to publish loopback ports, or point at a separately installed database.
- Errors from retired config fields: remove old QA, back-translation, or character-budget fields according to the config-page validator. Current Web does not auto-migrate legacy projects.

Inspect task failures with `docker compose -f deploy/docker-compose.yml logs -f api worker export-worker`. Record offline/browser verification separately from real-model translation quality evaluation.

### Provider settings and workflow view

Open a project and choose **Project config & models → API providers & models** to edit provider connections, base URLs, API-key environment variable names, request timeouts, model names, and strong/cheap/fast tier assignments. Extra connections and models can be added in the form. Changing provider protocol clears incompatible model options; update model names for the new provider before saving. Advanced YAML still supports operation-specific routes and fallbacks.

Credentials remain server environment variables. The form stores their names, not raw API keys. Configuration checks validate routing and credential availability without sending a model request. Save before checking the saved model configuration. Running projects must be paused before editing; new and resumed tasks capture the saved settings.

The progress page includes **Current translation plan**. It shows enabled and disabled steps for the latest non-export task using that task's configuration snapshot, with separate plans for books, subtitles, preparation, and review. Before the first task it shows the project's configured translation plan. Step cards describe the plan, not individual completion checkpoints; polishing still runs inside translation batches. The latest progress callback is cached in Redis for seven days and associated with the run ID, so reloading restores progress without showing an older run. Export jobs remain on the export page.

## Related notes

- [WebUI / `dev` sync notes](sync-dev-webui.md)
- [Validation record](validation.md)
