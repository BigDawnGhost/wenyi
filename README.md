# Wenyi · 文译

Multilingual book and subtitle translation with a Web UI and a local CLI.

**English** | [简体中文](docs/zh/README.md)

Wenyi translates long-form fiction with whole-book understanding, a shared glossary, polishing, evidence-based review and recoverable automatic fixes. This branch keeps the `webui` monorepo architecture and incorporates the supported workflows from `dev`.

![Bilingual EPUB preview](docs/images/bilingual-preview.png)

## Features

- **Languages:** source detection and direct translation between the built-in languages, including Simplified/Traditional Chinese and English/Portuguese variants. Run `trans-novel languages` for the registry.
- **Book workflows:** whole-book synopsis, rolling context, token-budgeted batches, translation followed by polishing in the same conversation where supported, live terminology extraction and chapter-title translation.
- **Review:** whole-book evidence checks, conflict arbitration, shadow revisions and a separate Autofix publisher. Matching completed reviews are reused; interrupted reviews resume their checkpoints. Review and Autofix are enabled by default.
- **Formats:** EPUB, DOCX, FB2, text, Markdown, HTML and PDF input; EPUB, DOCX, HTML, text, Markdown and PDF output. SRT has a separate subtitle workflow with timestamp-preserving mono/bilingual output.
- **Model routing:** provider connections, reusable model profiles, `strong` / `cheap` / `fast` tiers, per-operation routes, explicit fallback models, concurrency limits, request/token budgets and usage by model/provider.
- **Web workspace:** project configuration, progress, terms, manual editing, style guidance, Review history, subtitle editing and downloadable exports. An independent export queue reads consistent snapshots during translation.

## Choose an installation

### Web: Docker Compose

```bash
git clone --branch webui https://github.com/BigDawnGhost/wenyi.git
cd wenyi
cp .env.example deploy/.env
# Set credentials for the providers selected in config.yaml.
docker compose -f deploy/docker-compose.yml --profile full up --build
```

Open [the Web UI](http://localhost:8080) or [API documentation](http://localhost:8000/docs). The `server` profile starts the API, both workers, PostgreSQL and Redis without the frontend.

API and workers read the same `config.yaml`, environment credentials and `/data` volume. PostgreSQL stores mutable Web state; `/data` holds uploaded originals, parser resources and exports. See [Web deployment and development](docs/web.md).

### CLI: no database or Redis required

Install Python 3.10+ and `uv`, then:

```bash
uv sync --all-packages
export DEEPSEEK_API_KEY=your-key
uv run trans-novel translate book.epub
```

The default preset is DeepSeek. Choose another provider in `config.yaml` and set its named environment variable to use a different service. `uv sync --package wenyi-cli` installs only the local CLI and core dependencies.

```bash
uv run trans-novel prepare book.epub
uv run trans-novel translate book.epub --bilingual
uv run trans-novel review book.epub --no-autofix
uv run trans-novel status book.epub
uv run trans-novel assemble book.epub --format docx
uv run trans-novel translate captions.srt --bilingual
```

The default book workflow enables understanding, polishing, Review and Autofix. To reduce work, use Web's **快速出稿** preset or disable the corresponding configuration options. Repeating a command resumes saved work. `review --no-autofix` generates recommendations without publishing them.

CLI state is isolated by target language under `state/<book>/targets/<language>/`; subtitle state uses `state/srt/<name>/targets/<language>/`. Source hashes prevent accidentally continuing with a different input file. CLI exports normally use `output/<title>.<language>.<format>`; bilingual names include `-bi`.

## Formats and optional services

| Input | Notes |
|---|---|
| EPUB / HTML | Preserves supported structure, resources, navigation, ruby text and annotation links; EPUB supports per-paragraph annotation placement. |
| DOCX | Retains supported headings, paragraph/inline styles, lists and tables; output font policy follows the target language. |
| TXT / Markdown / FB2 | Parses local text into chapter/segment state. |
| PDF / MinerU | Default PDF path; requires `MINERU_API_KEY`, caches the parsed representation and supports scanned sources through MinerU. |
| PDF / BabelDOC | Optional external HTTP bridge selected in configuration; stores backend information for later PDF export. |
| SRT | Independent cue/batch state and mono/bilingual subtitle output; no book synopsis or book Review stage. |

For local PDF output, install `uv sync --all-packages --extra pdf-output` (WeasyPrint), or `--extra pdf-output-lite` (fpdf2). The Docker backend image includes both by default and installs Noto fonts; set `INSTALL_PDF_OUTPUT=false` to omit Python PDF output extras. The BabelDOC bridge is deployed separately.

## Architecture

```text
apps/web                    React / Vite
    │ HTTP / WebSocket
apps/api                    FastAPI / Arq
    ├── wenyi:workflows     parsing, preparation, translation, Review, model comparison
    ├── wenyi:exports       independent export workers
    └── PostgreSQL          Web state, terms, Review artifacts, subtitle caches, ledgers
packages/core               shared translation/domain services and storage ports
packages/cli                trans-novel → FileStorage / JSON / SQLite
packages/shared-schema      types generated from OpenAPI
```

The core owns translation behavior. API/CLI select storage; Review, Autofix and subtitle workflows persist through storage ports. Web workflows do not write local JSON or SQLite copies of database state.

This update targets **fresh Web deployments**. It does not automatically migrate old Web databases, project strategies or saved state. Use a separate database/volume for the new schema when retaining an earlier installation.

## Documentation and verification

- [CLI usage](docs/usage.md) · [Configuration](docs/configuration.md) · [Pipeline and persistence](docs/pipeline.md)
- [Web deployment](docs/web.md) · [Synchronization notes](docs/sync-dev-webui.md)
- [Contributing](CONTRIBUTING.md) · [License](LICENSE)

```bash
uv sync --all-packages --group dev
uv run pytest -q
uv run ruff check packages/core packages/cli apps/api
pnpm install --frozen-lockfile
pnpm -C apps/web typecheck
pnpm -C apps/web build
```

Set `WENYI_TEST_DATABASE_URL` to an isolated PostgreSQL database to run real storage/workflow integration tests. CI provides PostgreSQL and Redis, tests Python 3.10/3.12, builds the frontend and runs browser tests. Offline fixtures verify behavior and recovery; they do not measure real-model translation quality.
