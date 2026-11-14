# Web deployment and operations

This guide is for people deploying and operating Wenyi. The Web stack runs a browser frontend, an API, separate workflow and export workers, PostgreSQL, and Redis. For creating projects, translating, proofreading, glossary management, Review, and exporting, use the shared [workspace guide](workspace.md).

**The default deployment does not provide individual accounts, roles, or per-user project permissions.** An optional shared token protects the installation, not separate users' data. Keep a local installation on a trusted network; complete the [network security setup](#authentication-and-network) before exposing it publicly.

<a id="quick-start"></a>

## Docker Compose quick start

Install Docker and Compose v2. From a checkout of the release you intend to run:

```bash
# Create the environment file only if it does not already exist.
test -f deploy/.env || cp .env.example deploy/.env
# Edit deploy/.env and supply credentials for the providers you actually use.
cd deploy
docker compose up -d --build
```

The default Compose file starts all services:

| Service | Address or role |
|---|---|
| Web | <http://localhost:8080> |
| API / OpenAPI | <http://localhost:8000> / <http://localhost:8000/docs> |
| `worker` | `wenyi:workflows`: parsing, preparation, translation, and Review |
| `export-worker` | `wenyi:exports`: saved-snapshot exports |
| PostgreSQL 16 / Redis 7 | Container network only; no host ports by default |

The Web and API ports are published on all host interfaces by default, not only loopback. Restrict their bindings or use a firewall for a local-only installation. The supplied database credentials are development defaults; do not expose the database with those credentials.

Run operational commands from `deploy/`, using the same Compose project name and override files throughout:

```bash
docker compose ps
docker compose logs -f api worker export-worker web
docker compose down
```

`down` stops the stack and preserves named volumes. Do not add `-v` or prune volumes containing data you need.

Docker builds do not include Git metadata. Set `WENYI_VERSION` in `deploy/.env` to the intended package version when building release images; the default `0.0.0+docker` identifies a development build. Official package sources are used by default. Optional mirror settings are listed below; cached development builds are described in [Web development](../development/web.md#docker-builds).

<a id="configuration-and-dependencies"></a>

## Configuration and external dependencies

The API and both workers share database/Redis connections, the configuration mount,
and the file-data volume. The supplied Compose file injects the same `deploy/.env`
into all three containers, mounts the configuration read-only at `/app/config.yaml`,
and shares `/data`. This is a deployment convenience, not a requirement to give
every process all credentials: model calls and MinerU parsing need their respective
keys; the export worker needs neither model nor MinerU keys.

| Setting | Meaning |
|---|---|
| Provider variables such as `DEEPSEEK_API_KEY` | Credentials for the providers actually selected by model routes. For a custom connection, supply the variable named by `api_key_env`. |
| `MINERU_API_KEY` | Separate credential for MinerU PDF parsing; not a model-provider key. |
| `WENYI_CONFIG_FILE` | Host configuration file mounted by Compose; default `../config.yaml`, relative to `deploy/`. |
| `WENYI_CONFIG` | Configuration path inside an application process. Compose sets `/app/config.yaml`; local processes default to `config.yaml`. |
| `DATABASE_URL` / `REDIS_URL` | Application service connections. Compose explicitly overrides these with internal service addresses, so changing only the corresponding `.env` values does not redirect the container services. |
| `DATA_DIR` | Uploaded originals, parser caches, and export files. Compose sets `/data` on the shared `data` volume. |
| `WENYI_API_TOKEN` / `WENYI_CORS_ORIGINS` | Deployment authentication and allowed browser origins; see [network security](#authentication-and-network). |
| `WENYI_VERSION` | Version supplied during image builds without Git metadata. |
| `DEBIAN_MIRROR`, `UV_DEFAULT_INDEX`, `NPM_REGISTRY`, `UV_IMAGE` | Optional package mirrors and official `uv` image override. |

Provider keys remain in the server environment. Web settings store variable names, not raw keys; there is no Desktop-style manual credential storage in Web. Keep environment files private and out of version control.

After changing environment variables, recreate all three backend services:

```bash
docker compose up -d --force-recreate api worker export-worker
```

A plain container restart does not reload Compose's environment file. Coordinate recreation with running work as described under [upgrades](#upgrades).

The configuration file supplies defaults when no global settings have been saved, and supplies the defaults offered by **Restore defaults**. Once global settings are saved in Web, PostgreSQL holds the active settings document: editing the mounted YAML does not overwrite it. Project-specific settings still apply, and starting or resuming a task captures its effective configuration. See the [configuration reference](../reference/configuration.md) and [workspace settings guidance](workspace.md).

Fetching a provider's model list does not consume model tokens. **Test connection** sends live model requests and can incur charges; the workspace guide explains these actions.

### PDF services

- **MinerU** is the default PDF parser. Supply `MINERU_API_KEY` to processes performing PDF parsing and ensure they can reach the service. The supplied Compose file distributes this through its shared `.env`; export does not call MinerU. Parsing caches are shared with source previews.
- **BabelDOC** requires a separately deployed HTTP bridge. Set `pipeline.pdf_backend` to `babeldoc` and `pipeline.babeldoc_bridge_url` to an address reachable from the API and both workers. **The supplied Compose stack does not download or start this bridge.**
- In a container, `127.0.0.1` refers to that container, not the host or another service. Use a reachable service name or a host gateway configured for your Docker environment; do not assume a host's loopback URL works inside a worker.
- The Web backend image does not provide a local PDF rendering engine. PDF export is available for projects saved with the BabelDOC backend and uses bridge fillback. Changing the current parser setting does not convert existing projects. MinerU and other book projects use the other supported export formats; see [formats](../reference/formats.md).

<a id="authentication-and-network"></a>

## Authentication, CORS, and TLS

Before making the service publicly reachable:

1. Set a strong, private `WENYI_API_TOKEN`. HTTP clients send `Authorization: Bearer …`; the Web UI also sends the token in its WebSocket authentication message. Everyone with this token shares access to the installation.
2. Replace `WENYI_CORS_ORIGINS=*` with the exact browser origins that should access the API, including scheme and any nondefault port, for example `https://wenyi.example.com`. Separate multiple origins with commas, without spaces or URL paths. CORS is a browser policy, not a substitute for authentication or project permissions.
3. Terminate TLS at a reverse proxy and serve the browser over HTTPS, including secure WebSocket forwarding. Proxy both `/api` and `/ws`; do not expose an unprotected alternate route to API port 8000.
4. Restrict direct API, PostgreSQL, and Redis access with network policy. Keep the development port overlay out of public deployments.

The supplied Compose stack does not configure TLS or a multi-user authorization system. `WENYI_API_TOKEN` defaults to empty and `WENYI_CORS_ORIGINS` defaults to `*`; those defaults are not a public-service security policy. Allowed-origin preflight requests and authentication failures retain the expected CORS behavior. Custom clients should follow the [HTTP and WebSocket contracts](../development/web.md#http-contract).

<a id="backup-and-restore"></a>

## Back up PostgreSQL and file data together

| Data | Role |
|---|---|
| PostgreSQL / `pgdata` | Authoritative mutable state: projects and settings, chapters and segments, glossary, Review evidence/checkpoints, Autofix publish indexes, jobs, usage, and timing |
| `DATA_DIR` / `data` | Uploaded originals, parsing resources/caches, and export artifacts |
| Redis / `redisdata` | Queues, live progress/statistics, and operational counters; not the translation or usage ledger |

Backing up only exported books or only `DATA_DIR` does not back up a project. Web does not keep a second JSON/SQLite copy of mutable state in the file-data directory.

The following is an **operator-managed maintenance procedure**, not a built-in atomic backup feature:

1. Prevent new work, pause workflows at a safe boundary, and let active exports finish.
2. Stop the Web, API, and both workers so that the database and file tree remain unchanged while you capture them. Leave PostgreSQL running for a logical dump.
3. Save a PostgreSQL dump and an archive of the complete `data` volume to a new, protected directory outside the checkout. From `deploy/`, for the default Compose service names:

   ```bash
   umask 077
   backup_dir="/absolute/path/to/new-private-backup"
   mkdir -p "$backup_dir"
   docker compose stop web api worker export-worker
   docker compose exec -T postgres pg_dump -U wenyi -d wenyi -Fc \
     > "$backup_dir/postgres.dump" &&
     docker compose run --rm --no-deps -T --entrypoint tar api \
       -C /data -czf - . > "$backup_dir/data.tar.gz"
   ```

4. Verify both commands succeeded and retain the application version, configuration file, and Compose overrides with the backup. Store credentials separately using your protected secret-backup process. Retain Redis persistence if your operational backup policy requires queue/counter continuity.
5. Restart the stopped services with `docker compose start api worker export-worker web`. Test restoration into an isolated deployment before relying on the backup.

Restore the database and file archive as a matching pair. Use a compatible Wenyi version and verify source access and exports before allowing new writes. A restored or lost queue may require workflow **Resume** or a new export; preserving Redis is not a replacement for the PostgreSQL and file backups.

<a id="upgrades"></a>

## Upgrades and older deployments

Back up first, read the target release's compatibility notes, and finish or pause active work before recreating services. For a compatible deployment, rebuild and start the selected release with `docker compose up -d --build`, then check service health, project access, and both queues.

**The current PostgreSQL schema does not migrate the legacy Web architecture's projects, strategies, or state databases.** Do not point a new installation at old data and assume startup will convert it. Preserve the old stack and its volumes; use a separate Compose project, database, and volumes for the new installation, for example:

```bash
COMPOSE_PROJECT_NAME=wenyi-new docker compose up -d --build
```

Use that project name for subsequent commands too. If both stacks must run at once, assign different published ports; otherwise stop the old stack without deleting its volumes. Re-importing an original into a new project is not migration of its translations or history. Keep old data until you have independently verified what has been retained.

## Troubleshooting

| Symptom | What to check |
|---|---|
| Work stays queued | Confirm `worker` is running and consumes `wenyi:workflows`. API and workers must use the same PostgreSQL and Redis database. Inspect `docker compose logs api worker`. |
| Exports stay queued | Start `export-worker` on `wenyi:exports`; the workflow worker cannot consume exports. Check its environment and shared `/data` volume too. |
| Missing model credentials | Check the selected providers' variables in the API for connection tests and in the workflow worker for translation. After editing Compose's `.env`, recreate the affected containers. Unused providers and the export worker do not need model keys. |
| PDF parsing or export fails | Check the selected backend, its separate credentials, and network reachability from containers. BabelDOC must be deployed independently. |
| Browser gets `401` or a CORS error | Check the deployment token, the exact browser origin, and reverse-proxy forwarding. Changing a provider key does not change API authentication. |
| Web proxy returns `502` | Compare `http://localhost:8000/health` with `http://localhost:8080/api/health`, then inspect `docker compose logs web api`. |
| A package mirror returns `403` during build | Check mirror access; for PyPI, try `UV_DEFAULT_INDEX=https://pypi.org/simple`. Cache clearing does not repair denied registry access. Do not start with `--no-build` after a failed build and assume the image was updated. |
| Configuration is rejected | Use the current configuration validator/reference. Do not assume retired fields or legacy projects are migrated automatically. |

### Interrupted work

After an abnormal worker exit, restore the services and wait for saved jobs to be
reconciled. A long model request does not by itself mean a task is lost.
Do not delete locks or reset project state just because elapsed time is large.

Once the interface confirms an interrupted workflow is paused, use **Resume** to
continue from saved progress. Create a new export for an interrupted export.
Recovery has no fixed completion deadline; database or Redis outages can delay it.
The detection rules belong to the [developer recovery contract](../development/web.md#execution-and-recovery).

When reporting failures, retain the project/run identity and response `X-Request-ID` (and `X-Error-ID` for server errors), plus relevant server logs without credentials or private book contents. [Operational metrics](../development/web.md#metrics) can help monitor the service, but they are not token usage or a billing ledger.
