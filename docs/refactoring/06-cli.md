# 06 · Split CLI presentation, command groups and invocation context

[Index](README.md) · [简体中文](../zh/refactoring/06-cli.md)

Status: partially implemented; medium priority. Presentation extraction is complete. Baseline: `7471256`.

Implementation: progress lives in `commands/progress.py`, with clock/count tests in
`tests/test_cli_progress.py`. Usage, timing and completion summaries now live in
`commands/presentation.py`, receiving a console and data without accessing stores.
Command/context slices remain pending. No CLI behavior or persistent timing scope changed.

## Evidence

[`cli.py`](../../trans_novel/cli.py) has 953 lines. It combines early configuration creation, the mutable `_CONFIG` selection, Rich progress, usage/timing formatting, input/output checks, subtitle dispatch, workflow commands, status and glossary commands. Its largest function is only 110 lines: the main problem is multiple independently changing concerns rather than one complex algorithm.

`model_commands.py` already registers its commands explicitly. Follow that pattern instead of adding entry-point plugins, automatic module scanning or a second command registry.

## Proposed structure

| Module | Responsibility |
| --- | --- |
| `cli.py` | Construct/register the application and expose the current `main` entry point. |
| `commands/bootstrap.py` | Windows stream setup, early config-path argument detection, missing-default creation before help/errors, root group and version handling. |
| `commands/context.py` | Per-invocation `CommandContext`: config path, preflight policy and console; load configuration after command overrides are known. |
| `commands/progress.py` | Rich columns, description truncation and one-stage-at-a-time progress bridge. |
| `commands/presentation.py` | Usage, timing, completion and Review summaries. |
| `commands/validation.py` | Shared file/format/backend checks and explicit expected-error conversion. |
| `commands/workflows.py` | Register translate, prepare and review; preserve SRT dispatch to its lightweight path. |
| `commands/inspection.py`, `commands/glossary.py` | Register status/report/assemble and glossary actions respectively. |

Keep `model_commands.py` as an independent existing registrar initially. Registrars receive the app and a typed context accessor; none imports `cli.py` for global state. Retain `trans-novel = trans_novel.cli:main` as the real application entry point, not as a compatibility forwarding module.

## Context and errors

Use Typer/Click invocation context for `_CONFIG`'s current responsibilities. Early parsing still needs to determine the selected config before the root callback: moving all initialization into the callback would break help and early-error behavior. An application factory can create isolated instances for tests while using the same root initialization logic.

Load configuration per invocation, apply flags, then validate only reachable model operations. Local commands and help must not require API keys. Keep explicit `typer.Exit`, cancellation and command-specific exit codes; do not replace every handler with a catch-all decorator that hides programming errors.

Progress rendering belongs to the CLI, persisted timing belongs to `timing.py` and runtime/store scopes. Moving the progress bridge must not recreate its overall clock at each stage or create additional persistent timers. Preserve the final timing summary and `status` lookup.

## Slices and acceptance

1. Extract progress and presentation first. Retain all commands and invocation context in place for this first PR.
2. Extract shared validation, then register glossary/inspection groups explicitly.
3. Move workflow commands and replace global config selection with invocation context. Update internal imports and test patch points in the same change; do not keep private aliases.

`tests/test_cli.py`, `tests/test_model_commands.py`, `tests/test_config.py`, `tests/test_timing.py`, `tests/test_srt.py`, `tests/test_bilingual.py`, `tests/test_docx.py` and `tests/test_pdf_export_defaults.py` cover the relevant surfaces. Preserve help command names/options, startup config creation, no-key local commands, error codes, single-stage bars, overall elapsed time, default BabelDOC PDF export and explicit output overrides.

Add a two-invocation test using different configuration files: the second invocation must not inherit the first invocation's path, flags or preflight suppression. Test both `app` invocation and the configured CLI entry point. Restrict import cycles with `commands → domain entry points` and never `commands → cli`.

An initial target is an entry file that can be read in one short pass; reaching a specific line count is not an acceptance criterion. Language-specific model prompts remain under the existing i18n resources and are not moved into command modules.
