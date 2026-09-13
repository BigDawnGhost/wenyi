# Repository Guidelines

## Project Overview

`trans-novel` is a synchronous Python CLI for translating EPUB, FB2, TXT, and Markdown books into Chinese EPUB/TXT while preserving source structure and terminology consistency. The normal command is `translate`; `resume`, `status`, and `tools` support persisted runs and maintenance. Source lives in `trans_novel/`; both `python -m trans_novel` and the installed `trans-novel` command delegate to `trans_novel.cli:main`.

## Architecture & Data Flow

1. `trans_novel/cli/app.py` loads strict configuration, applies per-run CLI overrides, and calls `Application` in `trans_novel/pipeline/application.py`. `Application` is the production composition root: it constructs and injects clients, agents, nodes, planner, and runner, then drives prepare, translate, and finish phases.
2. Pipeline behavior is split into capabilities: `pipeline/composition/` owns composition helpers, `pipeline/execution/` owns lifecycle execution, `pipeline/nodes/` owns stage behavior, `pipeline/planning/` owns dependency planning, `pipeline/quality/` owns deterministic checks, and `pipeline/state/` owns persistence and resume. `pipeline/contracts.py` contains shared public contracts.
3. `trans_novel/ingest/` parses and segments source books. `trans_novel/assemble/` reconstructs outputs; EPUB publication, verification, and rendering are separate capabilities under `assemble/epub/`.
4. Runtime calls remain synchronous. Chapter work uses a bounded `ThreadPoolExecutor` with at most four workers; ordered commits and state reads/writes are serialized through the runner's lock and main-thread persistence path.
5. `trans_novel/benchmark/` is a separate offline evaluation subsystem exposed under `tools benchmark`. Its `corpus/`, `run/`, `integration/`, `review/`, and `report/` capabilities reuse the production `Application` with isolated state and artifacts; shared helpers remain at the benchmark root. Detailed operating rules live in `docs/benchmark-guide.md`.

For any Python production, test, or architecture change, MUST read and follow `docs/architecture-governance.md`.

## Architecture governance

The executable gate is `uv run python scripts/check_architecture.py`. It scans
UTF-8 Python files under `trans_novel/`, `tests/`, and `scripts/`, counting
physical lines with `str.splitlines()` and AST spans from decorators through
`end_lineno`; nested functions and classes are measured independently. The
hard limits are 800 file lines, 120 function/method lines, and 400 class
lines; 500/80/250 are warning thresholds. Static imports include relative,
local, function-local, and `TYPE_CHECKING` imports. Runtime and type-only
cycles are separate categories. Production modules may not cross-import
underscore-prefixed names; tests may use private production symbols for
necessary white-box contracts. Diagnostics always include rule, path, symbol,
current, limit, and remediation. A file with more than 300 net-new physical
lines emits a non-failing Architecture Delta review warning; no metadata file
is read.

`--update-baseline` is the only baseline-writing mode: an existing baseline may
only lose entries or lower numeric values; initial creation is allowed only when
the file does not exist. For CI and local hooks, `--base BASE --head HEAD`
loads the baseline from `BASE`; `--head WORKTREE` compares the current worktree.
Invalid revisions, malformed historical baselines, and failed diff ranges fail
closed. The pre-commit hook uses `HEAD`/`WORKTREE` so unstaged changes cannot
silently bypass the gate.

### Production dependency matrix

The executable package matrix is:

| Package | Allowed dependencies |
| --- | --- |
| `cli` | `config`, `pipeline`, `benchmark` |
| `pipeline` | `config`, `ingest`, `epub`, `agents`, `glossary`, `llm`, `assemble`, `postprocess`, `model_profiles` |
| `ingest` | `epub` |
| `assemble` | `ingest`, `epub`, `postprocess` |
| `epub` | `postprocess` |
| `agents` | `llm`, `config`, `glossary`, and only `trans_novel.ingest.models` data constants/contracts; not other ingest modules, parsers, or orchestration |
| `glossary` | none |
| `llm` | `config`, `model_profiles` |
| `benchmark` | `config`, `pipeline`, `ingest`, `assemble`, `epub`, `agents`, `glossary`, `llm`, `postprocess`, `model_profiles` |
| `postprocess` | none |
| `config` | `model_profiles` |
| `model_profiles` | none |

### Capability dependency directions

The second-level gate matches a module exactly or by `capability + "."`, chooses the longest match,
allows same-capability imports, and skips the rule when either side is untracked. The explicit
directions are: `pipeline.application` -> composition, contracts, execution, nodes, planning,
quality, state; `pipeline.composition` -> contracts, nodes, state; `pipeline.execution` ->
contracts, planning, state; `pipeline.nodes` -> contracts, planning, quality, state;
`pipeline.planning` -> contracts, state; `pipeline.contracts` -> state; `pipeline.quality` and
`pipeline.state` -> none; `assemble.epub.publication` -> verification;
`assemble.epub.verification` -> rendering; `assemble.epub.rendering` -> none;
`benchmark.run` -> corpus; `benchmark.integration` -> corpus, run; `benchmark.review` -> corpus;
`benchmark.report` -> review; `benchmark.corpus` -> none. Runtime and type-only imports use the
same rule.

## Key Directories

`trans_novel/pipeline/{composition,execution,nodes,planning,quality,state}/`,
`trans_novel/pipeline/application.py`, and `trans_novel/pipeline/contracts.py` contain workflow
capabilities and public contracts. EPUB publication is split across
`trans_novel/assemble/epub/{verification,rendering}/` and
`trans_novel/assemble/epub/publication.py`; benchmark capabilities are
`trans_novel/benchmark/{corpus,run,integration,review,report}/`, with shared helpers at its root.
`trans_novel/ingest/`, `trans_novel/llm/`, `trans_novel/agents/`, `trans_novel/glossary/`, and
`trans_novel/postprocess/` own source parsing, provider boundaries, agent behavior, terminology,
and deterministic postprocessing. Tests mirror capabilities under `tests/`; shared generated-book
and fake-client fixtures live in `tests/fixtures/`.

## Development Commands

```bash
uv sync --locked --group dev
uv run trans-novel --help
uv run trans-novel translate book.epub
uv run python -m unittest tests.pipeline.test_application
uv run python -m unittest tests.llm.test_routing
uv run --no-sync python -m unittest discover -s tests
uv run ruff check --fix .
uv run ruff format .
uv run pre-commit install
uv build
```

The package-installed source command is `trans-novel`; `python -m trans_novel` is an equivalent source entry point. Release workflows build the single-file executable as `wenyi` (`wenyi.exe` on Windows), so do not use that binary name in source-development commands.

## Code Conventions & Common Patterns

- Support Python `>=3.10`; keep public boundaries typed and use absolute imports. Ruff targets Python 3.10, a 100-column line length, import sorting, and the configured `E/W/F/I/B/C4/PIE/RUF/SIM/TID/UP` rules.
- Keep configuration narrow and strict. YAML exposes model lists for the `translator`, `analyst`, `editor`, and `fast` roles plus `quality` and `output`; unknown keys fail validation. CLI flags override configured values. Omitted output fields recover saved selections before code defaults.
- Preserve constructor injection. `Application` is the only production construction site; agents and nodes receive precise dependencies rather than creating hidden clients, stores, or configuration.
- Route agents to ordered model-role candidates through `llm/router.py` and construct transports through `llm/registry.py`. Keep retry/fallback classification centralized in `llm/retrying.py`; do not add a second provider-routing or retry path.
- Keep state writes behind `RunStore` and runner coordination. Preserve identity hashes, fingerprints, node/chapter statuses, advisory locking, ordered checkpoint commits, and idempotent recovery instead of special-casing resume in individual nodes.
- Put stage-specific decisions in `pipeline/nodes/`, deterministic parsing/formatting in their existing subsystems, and external validation in Pydantic models. Reuse existing patterns rather than adding parallel orchestration or configuration layers.
- Tests use `Test...` classes, `test_...` methods, `assertRaises`/`assertRaisesRegex`, and `subTest` for matrices.

## Important Files

- `trans_novel/pipeline/application.py`: `Application` composition root and workflow facade.
- `trans_novel/pipeline/{composition,execution,planning,state}/`: composition, lifecycle, planning, persistence, migration, and recovery contracts.
- `trans_novel/pipeline/nodes/`, `quality/`, and `contracts.py`: stage behavior, deterministic checks, and shared node contracts.
- `trans_novel/config.py`, `config.example.yaml`: strict schema, model roles, defaults, and quality presets.
- `trans_novel/llm/{router,registry,retrying}.py`: role routing, transport construction, and centralized retry/fallback.
- `trans_novel/ingest/`, `trans_novel/assemble/epub/`: parsing, output reconstruction, publication, verification, and rendering boundaries.
- `trans_novel/benchmark/{corpus,run,integration,review,report}/`: benchmark corpus, execution, integration, review, and reporting.
- `tests/{pipeline,assemble,benchmark}/` and `tests/fixtures/`: capability-mirrored contracts and canonical offline fixtures.

## Runtime/Tooling Preferences

- Use `uv` with the committed `uv.lock`. CI installs locked dev dependencies and tests Python 3.10 and 3.12.
- Hatchling builds the wheel; Typer/Rich provide the CLI; release CI uses PyInstaller for five platform/architecture targets and smoke-checks the resulting `wenyi` binaries.
- Keep runtime behavior synchronous. Use the existing bounded executor and main-thread persistence protocol rather than introducing an isolated `asyncio` call graph.
- Provider credentials belong in environment variables such as `OPENCODE_API_KEY`, `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`, and `OPENROUTER_API_KEY`, never plaintext repository configuration.
- Routing, retries, timeouts, and concurrency are internal policy, not general YAML knobs. Unsupported configuration or model capabilities should fail explicitly.
- The changelog gate runs locally and in CI. Changes under `trans_novel/`, `tests/`, `scripts/`, `.github/workflows/`, or to `.pre-commit-config.yaml`, `pyproject.toml`, or `uv.lock` must update `CHANGELOG.md` under `[Unreleased]` in the same change.

## Testing & QA

- Use standard-library `unittest`; the canonical full CI command is `uv run --no-sync python -m unittest discover -s tests`. `pytest` is a dev dependency but is not the repository's canonical runner.
- Keep the suite offline. Inject `FakeClient` from `tests/fixtures/fake_llm.py` or stub transports, inspect call metadata when relevant, and never send provider requests from tests.
- Isolate filesystem state with `tempfile.TemporaryDirectory`. Build TXT/FB2/EPUB inputs with `tests/fixtures/books.py` or focused inline fixtures, then reopen outputs to assert serialized state and ZIP/XML structure.
- Use Typer's `CliRunner` for CLI behavior, normalize ANSI output for stable assertions, and patch only external boundaries.
- Pipeline changes should cover observable planning, fingerprints, lifecycle statuses, resume/idempotency, checkpoint recovery, ordered persistence, and unexpected LLM calls. Routing changes should cover candidate order, retry classification, error identity, and usage attribution. Ingest/assembly changes should cover structural round trips and unsafe/malformed input boundaries.
- There is no numeric coverage threshold. Every behavior change still requires a focused behavioral regression in an existing appropriate module and the full offline suite before submission.
