# Contributing to Wenyi

**English** | [简体中文](docs/zh/CONTRIBUTING.md)

Thank you for helping improve Wenyi. The project prioritizes the quality and reliability of long-form novel translation.

## What you can contribute

Contributions are welcome in the following areas:

- Input parsing: compatibility improvements and new support for EPUB, FB2, TXT, DOCX, SRT, and related formats.
- Translation pipeline: context handling, terminology, review, polishing, and consistency checks. Subtitle work belongs in the independent `wenyi_core.srt` pipeline.
- Export: EPUB/DOCX output, tables of contents, metadata, layout preservation, and SRT writers.
- Tests: real-world failure cases, regression tests, and offline fake-LLM tests.
- Documentation: usage instructions, configuration explanations, troubleshooting, and translations.

Changes to the core translation pipeline can affect translation quality in subtle ways. Before proposing such a change, test it on a public-domain novel of at least 50,000 words and include a before-and-after comparison that explains the quality impact.

Use standard English for code comments, docstrings, configuration comments, CLI messages, and prompt instructions. Keep language rules and task templates in `packages/core/wenyi_core/i18n/data/`. Generated prose, including glossary notes and analysis descriptions, follows the translation target; original `source` and `aliases` remain unchanged for matching. Preserve language-specific examples and multilingual test fixtures, and keep English and Chinese documentation synchronized.

## Python regression tests

Keep each rule at its owning layer: Core tests cover domain rules and persistence/resume invariants; shared backend tests cover platform-neutral application and HTTP contracts; API/Desktop tests cover adapter wiring, platform storage, queues, and native file safety. Orchestrator tests should retain cross-service workflows rather than duplicate lower-level service matrices. Share fixtures through `conftest.py` and helpers through support modules, not imports from other `test_*` modules.

From the repository root:

```bash
uv sync --locked --all-packages --group dev
uv run --no-sync python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"
uv run --no-sync pytest -q -m "not integration"  # No external test services.
uv run --no-sync pytest -q -m integration        # Isolated service tests.
uv run --no-sync pytest -q                      # Full collection, as before.
```

The vocabulary preparation command downloads `cl100k_base` if it is not cached yet; prepare it while online before running offline tests. Tests keep the real tokenizer rather than replacing token-budget semantics with a fake. CI prepares the vocabulary before regression steps, so cold downloads do not consume local-job test timeouts.

API tests that depend on the `pg_pool` fixture (including indirect dependencies) automatically receive the `integration` marker. Parameters that request PostgreSQL fixtures dynamically must mark that parameter explicitly; file-storage cases remain offline. Local SQLite, subprocess, crash-recovery, and workflow tests stay in the offline suite.

Set `WENYI_TEST_DATABASE_URL` and `WENYI_TEST_REDIS_URL` only to isolated test services. PostgreSQL fixtures create a private schema and drop it afterward. Docker proxy tests are also marked `integration` and additionally require `WENYI_TEST_DOCKER=1`. Missing services produce skips, not successful integration validation. CI runs both selections on Python 3.10 and 3.12 with PostgreSQL and Redis enabled; Desktop packaging retains its platform-specific tests.
