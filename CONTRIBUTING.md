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

## Main-based development

`main` is the only long-lived integration branch. Keep it buildable, tested, and
releasable. Use small, short-lived `feat/*`, `fix/*`, `docs/*`, or `chore/*` branches
and open PRs directly against `main`; do not route work through `dev`, `webui`, or
a separate release integration branch. Split large changes into independently
safe PRs and keep unfinished behavior out of the default user path.

With `origin` pointing to the Wenyi repository, start new work from the latest
trunk:

```bash
git fetch origin
git switch -c feat/short-description origin/main
# Implement, verify, and commit only the intended changes.
git push --set-upstream origin feat/short-description
gh pr create --base main --head feat/short-description
```

Before merging, integrate the latest `origin/main` into the task branch and
resolve conflicts without rewriting published history. Review the complete diff
against `main`, not only the latest commit. Require the full `Tests` workflow
(Python 3.10, Python 3.12, and web) and any applicable CLI/Desktop packaging
checks to pass for the final candidate and current base. Follow applicable
review requirements; do not bypass checks or push changes directly to `main`.
Merge commits remain the default landing method. The repository's `/land`
workflow targets `main` and still requires explicit landing authorization.

### Releases and fixes

Create version tags such as `v1.2.8` or `v1.2.8rc1` at a verified commit on
`main`, then publish the GitHub Release for that tag. Publishing the release
triggers CLI/Desktop packaging and asset uploads. Merging a PR or pushing a tag
alone does not publish installers; manual workflow dispatch remains available
for build validation. Fixes follow the same branch → PR → `main` path and can
then be released with a new tag. Do not move an existing release tag or maintain
a separate long-lived hotfix branch.

### Repository settings and migration

Repository administrators should keep `main` as the default branch, require
PRs and the `Tests` Python 3.10 / Python 3.12 / web checks, require an up-to-date
base, and prevent force-pushes and branch deletion. CLI/Desktop packaging
workflows are path-filtered, so do not make their conditional checks mandatory
for every PR. Written policy does not configure GitHub protections; inspect
the live branch protection and rulesets separately.

When migrating existing work:

1. Review changes still present only on `dev` or another former integration
   branch and bring the intended changes into `main` through a dedicated PR.
   Do not discard them or assume differences in merge history mean all changes
   are missing from `main`.
2. Retarget remaining PRs to `main`, integrate the latest trunk, inspect their
   complete new diffs, and require fresh CI against the new base.
3. Stop using the old integration branches. Delete them only after confirming
   all intended work is preserved and obtaining explicit deletion approval.
   Changing this policy alone does not authorize merging PRs, changing remote
   settings, or deleting branches.

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
