# Web deployment and development

[简体中文](zh/web.md) · [CLI usage](usage.md) · [Configuration](configuration.md)

The Web stack uses React/Vite, FastAPI, Arq, PostgreSQL, and Redis. Ordinary workflows and exports are consumed by two independent workers. Domain logic comes from `wenyi_core`; the Web path does not copy CLI file-state layouts into the database.

## Fresh Docker deployment

Requires Docker and Compose v2. From the repository root:

```bash
cp .env.example deploy/.env
# Edit deploy/.env and set credentials for providers used in config.yaml.
cd deploy
docker compose up -d --build
```

Docker builds exclude `.git`. They pass `WENYI_VERSION` to `hatch-vcs` during
installation so Core, CLI, and API receive the same package version. The default
`0.0.0+docker` identifies a local development build. For a release, set
`WENYI_VERSION` to the corresponding package version in `deploy/.env`
before building. Normal installations from a Git checkout still derive versions
from Git tags.

| Service | Address / role |
|---|---|
| Web | http://localhost:8080 |
| API / OpenAPI | http://localhost:8000 / http://localhost:8000/docs |
| `worker` | `wenyi:workflows`: parse, prepare, translate, review, SRT |
| `export-worker` | `wenyi:exports`: snapshot export |
| PostgreSQL / Redis | Internal network only by default |

All services start by default, with no profile or build environment flags required. Dockerfiles use ordinary build steps without BuildKit cache mounts or an external Dockerfile frontend; unchanged steps still benefit from Docker layer caching. Run subsequent commands from `deploy/`:

```bash
docker compose logs -f
docker compose down
```

`down` preserves data volumes. Debian, PyPI, and npm use official sources by default. To use mirrors, uncomment the desired `DEBIAN_MIRROR`, `UV_DEFAULT_INDEX`, or `NPM_REGISTRY` entries in `deploy/.env`, then run `docker compose up -d --build` again.

This database schema targets fresh deployments. It does not migrate legacy Web projects, strategies, or databases. When keeping an older deployment, give the new stack a separate Compose project, database, and volumes (for example `-p wenyi-new`). Do not delete volumes that still hold data you need.

### Optional Buildx development builds

With the Docker Buildx plugin installed (`docker buildx version`), opt into the cached Dockerfiles from `deploy/`:

```bash
DOCKER_BUILDKIT=1 COMPOSE_BAKE=true docker compose \
  -f docker-compose.yml -f docker-compose.buildx.yml build
docker compose up -d --no-build
```

The overlay changes only Dockerfile selection and preserves the same services, image names, credentials, mirror settings, and volumes. It caches apt, uv, and pnpm downloads between builds; caches belong to the selected builder. Default Dockerfiles remain usable without BuildKit. Keep both variants aligned when changing installation steps. Return to ordinary builds with `docker compose up -d --build`.

Compose can delegate builds to [Buildx Bake](https://docs.docker.com/guides/compose-bake/). This optional path requires a working Buildx/BuildKit installation; it is not required for deployment.

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
| `WENYI_VERSION` | Package version supplied during Docker builds; default `0.0.0+docker`. Set the release version explicitly for release images. |
| `DEBIAN_MIRROR` / `UV_DEFAULT_INDEX` / `UV_IMAGE` / `NPM_REGISTRY` | Optional build mirrors and the official `uv` image tag. Official sources are the default; uncomment the corresponding entries in `deploy/.env` to use mirrors. |
| `WENYI_API_TOKEN` | Optional static token. HTTP uses Bearer auth; WebSocket authenticates with the first post-connect `{"token":"…"}` message (the frontend sends it automatically). |
| `WENYI_CORS_ORIGINS` | Allowed origins, comma-separated; default `*`. Allowed-origin preflights work with token authentication, and authentication errors retain CORS headers. |

Custom WebSocket clients must send a JSON first packet `{"token":"…"}` within 10 seconds of connecting. Send the packet even when the server has no token configured (the token may be empty). Project snapshots and live events are delivered only after authentication succeeds.

PostgreSQL stores projects, chapters, segments, glossary, review evidence/checkpoints, Autofix publish indexes, subtitle caches, usage, and timing. `DATA_DIR` does not keep JSON/SQLite copies of that mutable state.

### PDF

Backend images copy `uv`/`uvx` from the official Astral image, run a frozen `uv sync` for all workspace packages (plus PDF extras by default), and keep the venv on `PATH`, so containers start without installing extra Python dependencies. They also include WeasyPrint/fpdf2, required Pango libraries, Noto CJK/generic fonts, and WenQuanYi Zen Hei by default. fpdf2 only uses fonts with TrueType outlines and skips OpenType/CFF faces such as Noto CJK automatically; set `TRANS_NOVEL_PDF_FONT` to force a compatible font. Choosing an incompatible font explicitly returns an error instead of producing a broken file. WeasyPrint can use Noto CJK. MinerU is the default parse backend; parse caches are shared with upload previews.

BabelDOC runs as a separate HTTP bridge. Set `pipeline.pdf_backend` to `babeldoc` and point `pipeline.babeldoc_bridge_url` at an address reachable from the API and both workers. `127.0.0.1` inside the bridge means that container, not the host. This Compose file does not download or start BabelDOC. The UI only exposes PDF features that match installed dependencies and configuration.

## Local development

Requires Python 3.10+, `uv`, Node 22, pnpm 9, PostgreSQL 16, and Redis 7. Start the databases first. The development overlay binds loopback ports on the host:

```bash
cp .env.example deploy/.env
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.dev.yml \
  up -d postgres redis
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

1. Choose source/target languages and a nonempty EPUB, DOCX, FB2, TXT, Markdown, HTML, PDF, or SRT file before creating the project. Drag one file into the source area or use **Browse files**, then click **Create project** to upload it. Both methods use the same format and empty-file checks; multiple-file drops are rejected. Source selection is locked during upload and after project creation. Optionally select **Prepare before translating** for books; PDF parser selection is available before upload.
2. Parsing runs as a background task after upload and then shows a preview. Matching parse results are reused during preparation.
3. The project inherits the workflow defaults from global **Settings**; creation has no workflow selector. Use **Project settings** to adjust steps and select already registered models, and validate actual routes before starting translation.
4. Start the run and watch the progress page. After a safe-boundary pause, resume continues the actual task type.
5. For books, edit glossary, style, and paragraphs, and inspect whole-book review history, suggestions, and published fixes. For SRT, edit subtitle cues and timestamps.
6. Export with format and monolingual/bilingual options. An independent worker reads a saved snapshot. Each export has its own file location and can be downloaded when done.

**Manual proofreading** has its own navigation entry, separate from whole-book review. Its chapter list includes unfinished chapters. The chapter view refreshes saved paragraphs every 3 seconds, so each persisted translation batch is visible before the chapter finishes. Pending paragraphs show “Waiting for translation”; an intentionally saved empty translation still counts as complete. A running task allows viewing; pause it before editing saved paragraphs. Refreshes preserve an open edit draft.

Use **Collapse sidebar** beside the logo to make more room for the page. Desktop navigation becomes an icon rail with named hover hints; on mobile, the navigation links hide while the expand button stays visible. The browser remembers this preference across pages and reloads. Project links keep their order, and global settings remain available from the navigation.

Long chapter titles wrap without squeezing status labels or actions; on narrow screens, the overview's chapter table scrolls horizontally within its card. Sections without a title show “Untitled chapter” in the overview and proofreading views. Source and translation appear side by side without a header row on wide screens and stack with labels on narrower screens. Long text and references wrap within the reading and editing views.

The glossary keeps the same compact rows and column widths across type filters. Long terms and readings use a single-line preview; hover over the text or open the term editor to read the full value. On narrow screens, the table scrolls horizontally within its card.

**Contents & titles** lists the existing chapter titles beside their translations. Search either column, open a chapter in proofreading, or edit a translated title with the pencil button. Saving persists the title on the server for every browser and updates the linked EPUB TOC node, including equivalent NAV/NCX entries with the same original title and destination. Future exports use the saved titles; existing export files remain unchanged. Body headings are edited separately in proofreading. Pause running tasks before editing. If another editor changes the same title, your input is retained and saving requires loading the latest title. The page currently lists chapters; editing the complete EPUB hierarchy and automatic title alignment are not included.

In **Style & synopsis → Chapter summaries**, long titles and summaries wrap in separate columns on wide screens, with more space for the summary. On narrower screens, each title appears above its summary, and the tabs wrap to fit. Untitled sections use the same “Untitled chapter” label. Summaries remain editable when the project is idle and read-only while a task runs.

The review page distinguishes recommendations from actual write-back; historical runs do not borrow current-task progress. The server retains the latest five completed export files per project.

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
uv run --no-sync ruff check packages/core packages/cli apps/api
uv run --no-sync pytest -q
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

From `deploy/`, inspect task failures with `docker compose logs -f api worker export-worker`. Record offline/browser verification separately from real-model translation quality evaluation.

### Provider settings and workflow view

Global **Settings** owns provider connections, model registration, default tiers and operation routes, and the default workflow template. Connection/model IDs can be renamed; referenced entries cannot be deleted. Restoring defaults loads a draft and takes effect only after saving. **Project settings** selects already registered models and adjusts project workflow options; it does not register providers or models. Advanced YAML supports operation-specific routes and fallbacks.

Credentials remain server environment variables. The form stores their names, not raw API keys. Configuration checks validate routing and credential availability without sending a model request. Save before checking the saved model configuration. Running projects must be paused before editing; new and resumed tasks capture the saved settings.

In **Translation overview**, expand **Workflow details** to see **Current workflow**. It shows enabled and disabled steps for the latest non-export task using that task's configuration snapshot, with separate plans for books, subtitles, preparation, and review. Before the first task it shows the project's configured translation plan. Step cards describe the plan, not individual completion checkpoints; polishing still runs inside translation batches. The latest progress callback is cached in Redis for seven days and associated with the run ID, so reloading restores progress without showing an older run. Export jobs remain on the export page.

## Related notes

- [Interface languages](web-i18n.md)
