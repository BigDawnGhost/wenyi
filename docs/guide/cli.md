# CLI guide

Use the CLI to translate local files, resume saved work, check terminology, run
whole-book Review, and export editions. This guide covers the installed `wenyi`
command. For graphical workflows, see [Desktop](desktop.md), [Web](web.md), and
the [workspace guide](workspace.md).

- [Configuration reference](../reference/configuration.md): settings, defaults, providers, and routes.
- [Translation workflow](pipeline.md): what preparation, translation, polishing, and Review do.
- [Format reference](../reference/formats.md): supported inputs, output naming, layout, and format limitations.
- [Development](../development/index.md): running from source and contributing.

<a id="installation"></a>

## Install and configure

### Install the CLI

Install the published [wenyi-cli](https://pypi.org/project/wenyi-cli/) package with
Python 3.10+ and [uv](https://docs.astral.sh/uv/); no repository checkout is required:

```bash
uv tool install wenyi-cli
wenyi --version
```

If `wenyi` is not found, run `uv tool update-shell` and open a new terminal.
Upgrade with `uv tool upgrade wenyi-cli`. Alternatively, run
`uv pip install wenyi-cli` in an active virtual environment; that environment also
supports `python -m wenyi_cli`.

Standalone CLI archives do not require Python. Choose the matching release asset
and verify it against `wenyi-cli-SHA256SUMS.txt` before extracting it. In these
example names, replace `1.2.3` with the downloaded version:

| Platform | Archive |
|---|---|
| Windows x64 | `wenyi-cli-1.2.3-windows-x64.zip` |
| Linux x64 / ARM64 | `wenyi-cli-1.2.3-linux-x64.zip` / `wenyi-cli-1.2.3-linux-arm64.zip` |
| macOS Apple Silicon / Intel | `wenyi-cli-1.2.3-macos-arm64.zip` / `wenyi-cli-1.2.3-macos-x64.zip` |

On Windows, invoke the extracted program as `.\wenyi.exe`. On Linux/macOS, run
`chmod +x wenyi` once and invoke it as `./wenyi`. macOS builds are ad-hoc signed,
not Apple-notarized; after verifying the checksum, approve the executable in
**System Settings → Privacy & Security** if macOS blocks it.
`wenyi-desktop-*` assets are separate [Desktop installers](desktop.md).

### Create and inspect configuration

In the directory where you want to work:

```bash
wenyi models list
wenyi languages
```

The CLI creates a documented `config.yaml` if it is missing, including on help
and version invocations. `models list` previews effective model routes without
making requests. Review the model provider, languages, workflow, and output
settings before translating.

Use a different file with the root option `--config` (`-c`), before the command:

```bash
wenyi --config my-config.yaml models list
wenyi -c my-config.yaml models check --for translate
```

`models check` checks reachable credentials locally; it does not prove that a
remote account or model works. See [model commands](#model-commands) for the
optional live probe.

CLI API keys come only from process environment variables. The CLI does not
automatically load a repository-root `.env`. Supply keys locally, or load them
from a private key file outside shared workspaces and sessions. Never put real
keys in YAML, source control, shared logs, or conversations. Examples below use
placeholders, not credentials.

<a id="first-translation"></a>

## Translate your first book

The default configuration uses DeepSeek and translates into Simplified Chinese.
In a Linux/macOS shell:

```bash
export DEEPSEEK_API_KEY="YOUR_DEEPSEEK_API_KEY"
wenyi translate book.epub
```

In Windows PowerShell:

```powershell
$env:DEEPSEEK_API_KEY = "YOUR_DEEPSEEK_API_KEY"
wenyi translate .\book.epub
```

Use `./wenyi` or `.\wenyi.exe` instead when running an extracted executable.
Only submit documents you are authorized to send to the configured providers.
Preparation, translation, polishing, and Review can all make paid model requests.
PDF import may also send the document to the configured parsing service.

For a default EPUB run, the translated edition is written beside the source under
`output/`, normally as `book.zh.epub`. The terminal prints the actual paths,
chapter and term counts, usage, timing, and Review results.

**Defaults include polishing, whole-book Review, and Review Autofix.** After
translation, Review may publish revisions to the saved formal translation.
To retain Review reports without starting new revision publication, set
`pipeline.review_autofix: false` in YAML before `translate`. To omit final Review
entirely for that invocation, pass `--no-review`. The `--no-autofix` flag belongs
to the separate `review` command, not `translate`. Already-started Autofix
publication can still recover on resume; see [Review safety](#review).

### Choose languages

Merge this fragment into your configuration to translate Chinese directly into
English, keeping your existing model settings:

```yaml
language:
  source: zh
  target: en
```

```bash
wenyi --config config.yaml translate book.epub --bilingual
```

`source: auto` detects the source with a model; an explicit source skips that
request. Each invocation selects one direction. Use the same target configuration
for later state, glossary, Review, and export commands.
[Supported language codes](../reference/configuration.md#languages) are
experimental translation targets, not a guarantee of native-language quality.
CLI help and diagnostics remain English.

### Parse or prepare before translating

```bash
wenyi parse book.epub
wenyi prepare book.epub
wenyi translate book.epub
```

- `parse` lists **zero-based** chapter indices, titles, and paragraph counts, and
  caches the parsed source under the printed state directory. It needs no LLM
  credentials, does not detect the language with a model, and does not initialize
  translation state. PDF parsing still requires the configured MinerU/BabelDOC service.
- `prepare` parses, detects the language if needed, analyzes style and character
  voices, and generates chapter digests and a book synopsis when enabled.
  **It neither translates body text nor extracts or pre-fills glossary entries.**
  It can incur model usage.
- `translate` reuses matching parsing and preparation, then translates pending
  work. Ordinary glossary extraction starts from completed source/translation
  pairs during translation.

For an uncached MinerU PDF import, also set `MINERU_API_KEY` in the process
environment. BabelDOC setup, PDF export eligibility, and other format-specific
requirements are in the [format reference](../reference/formats.md).

### Translation options

Options not supplied on the command line retain their YAML values.

| Option | Effect |
|---|---|
| `--chapter N` | Translate and save one zero-based chapter; skip whole-book Review and export. |
| `--polish` / `--no-polish` | Override `pipeline.polish` for pending translation work. |
| `--translation-mode standard\|three_draft_synthesis` | Override the translation method; precision requires polishing. |
| `--review` / `--no-review` | Override automatic final whole-book Review. |
| `--format epub\|txt\|html\|markdown\|pdf\|docx` | Choose an export format; PDF is only available for saved BabelDOC projects. |
| `--out PATH` | Set the monolingual output path; bilingual naming is derived from it. |
| `--mono` / `--no-mono` | Override monolingual output. |
| `--bilingual` / `--no-bilingual` | Override bilingual output. |
| `--progress-json PATH` / `--result-json PATH` | Write optional [machine-readable progress and receipts](#machine-output). |

```bash
wenyi translate book.epub --chapter 3
wenyi translate book.epub --no-polish --no-review
wenyi translate book.epub --bilingual
wenyi translate book.epub --no-mono --bilingual
```

`--chapter 3` means the fourth chapter. Do not combine chapter-only translation
with finalization options: `--out`, Review switches, edition switches, and
non-EPUB `--format` values are rejected. Use `assemble` afterward to export.
Default formats, explicit output names, and bilingual limitations are documented
once in the [format reference](../reference/formats.md).

<a id="precision-translation"></a>

### Three-draft precision

```bash
wenyi translate book.epub --translation-mode three_draft_synthesis --polish
```

This produces **three independent drafts
followed by one source-aware synthesis**, not selection of one winner.
It requires polishing; `--no-polish` is invalid in this mode.
A normal batch uses four model calls rather than the two used by standard
translation with polishing. Quality improvement is not guaranteed.

Keep the same mode and configuration on resume to reuse precision checkpoints.
Changing to `standard` affects pending batches, not completed translations.
See [three-draft precision](pipeline.md#three-draft-precision) for the quality and
cost tradeoffs.

<a id="prompt-genres"></a>

### Translation style

The root option `--prompt-genre literary|academic` selects the translation style
for one CLI invocation; place it before the subcommand. The default is `literary`;
`academic` emphasizes arguments, conceptual distinctions, qualifications, and
citations. CLI, Web and Desktop share the same prompt families. This is not
automatic genre detection or a Core YAML setting.

```bash
wenyi --prompt-genre academic --config academic.yaml prepare book.docx
wenyi --prompt-genre academic --config academic.yaml translate book.docx
wenyi --prompt-genre academic --config academic.yaml review book.docx --no-autofix
```

The former `--experimental-prompt-genre` spelling remains accepted for existing
scripts but is hidden from help. Use `--prompt-genre` in new commands; if both
spellings are supplied, their values must agree.

Pass the same choice on every `prepare`, `translate`, and `review` invocation,
including resumes. It is not restored from saved state. Changing it may refresh
derived analysis and pending work; it does not retranslate completed text.
For comparisons, use separate `paths.state_dir` values, not just different
output names. Both variants incur usage; representative quality evaluation is
still necessary. Web/Desktop creation-time choices are covered in the
[workspace guide](workspace.md).

<a id="terminology-context"></a>

### Experimental terminology evidence

Set `pipeline.terminology_context: true` to enable CLI-only whole-book source
evidence for person entries. Inspect its routes before using it:

```bash
wenyi models explain --operation terminology.evidence
wenyi models check --for translate
wenyi translate book.epub
```

This adds person discovery and evidence work to pending translation batches;
`prepare` still does not extract or seed terms. Other term types keep ordinary
posttranslation extraction. Existing authoritative names and protected manual
fields are retained. The feature adds context, model calls, and initial latency;
it is off by default and is not a per-occurrence word-sense system.
Review may consume saved notes but does not produce this evidence.
See [context and glossary](pipeline.md#context-and-glossary) before enabling it
for a long book.

<a id="resume"></a>

## Resume and inspect saved work

Completed batches are saved under
`<paths.state_dir>/<book>/targets/<target-language>/`. After an interruption,
repeat the original command with the same source and configuration:

```bash
wenyi translate book.epub
wenyi status book.epub
wenyi inspect-run book.epub
```

Completed translations are skipped. Changing models or polishing settings does
not automatically reprocess them. Upgrades or changed prompt/language policies
may rebuild affected preparation before pending work continues; that can incur
model usage without replacing completed translations.

State is bound to source content, not only its filename. Different content with
the same name, a conflicting source language, or an invalid saved source hash
cannot resume. For a deliberately fresh run, choose a new `paths.state_dir`;
do not delete an existing state directory just to try another setting.
CLI, Web, and Desktop do not share project state.

Ctrl+C cooperatively stops queued requests and retries. An in-flight request
may finish or reach its provider timeout. A forced kill can lose the last
unsaved work or usage information; prefer normal interruption.

`status` shows chapter progress, glossary statistics, and saved timing.
`inspect-run` summarizes recorded failures, retries, timing, and cumulative
target usage without calling a model. Add `--verbose` to include the last
recorded traceback; review it for private text and paths before sharing.
Neither command adds execution time.

Token totals span resumes, not just the latest invocation. Cache-read rates use
reported reads divided by all input; unavailable counters mean “unknown,” not
zero. Counters are not currency estimates. Workflow timing excludes downtime
between invocations; forced termination or failure before initialization may
prevent the current duration from being saved.

### Diagnostic logs

The CLI keeps routine application and HTTP-client logs out of terminal progress
output. Each invocation saves INFO-and-higher diagnostics to a separate
`logs/wenyi-cli-<UTC timestamp>-<pid>-<unique suffix>.log` beside the selected
configuration file. This location does not depend on `paths.state_dir`; for
`--config /work/settings.yaml`, look under `/work/logs/`.

Progress, results, and actionable error messages remain visible. On failure the
CLI prints the diagnostic log's absolute path. If the log cannot be initialized,
the command stops with a concise error instead of silently discarding diagnostics.
`WENYI_LOG_FORMAT=json` selects JSON records in the file, not additional terminal
output. Web and Desktop logging are unchanged.

Each file rotates at 5 MiB with up to two `.1` / `.2` backups. Separate invocations
do not share a file or delete older invocation logs. Review logs for private text
and paths before sharing, and remove unneeded logs explicitly.

<a id="proofreading"></a>

## Check terminology and proofread

```bash
wenyi glossary list book.epub
wenyi glossary conflicts book.epub
wenyi glossary resolve book.epub "source term" "chosen translation"
```

`resolve` changes an **existing** term's authoritative translation and closes its
conflicts; it is not a glossary import or a whole-book search-and-replace command.
The selection is available to subsequent translation and Review. Inspect the
exported text and Review results for already completed passages.
There is no standalone `polish` command: polishing runs within translation, and
turning it on later does not polish already completed batches.

<a id="review"></a>

## Run whole-book Review

All chapters must have completed body translations. To generate reports without
starting a new Autofix publication:

```bash
wenyi review book.epub --no-autofix
```

**`wenyi review book.epub` is not read-only by default.** It inherits
`pipeline.review_autofix`, whose built-in default is `true`. Review can therefore
publish revisions to saved chapter translations. `--autofix` and `--no-autofix`
are optional overrides, not a separate default:

```bash
wenyi review book.epub --autofix
```

Review evaluates the whole translated book with its glossary and can perform
temporary revisions and blind rechecks. With `--no-autofix`, reports and
run-local revisions are saved without starting new formal writeback.
**If an earlier Autofix publication was interrupted after its `applying` record
was saved, Review first resumes that writeback even with `--no-autofix`.**
The flag is not a cancellation or rollback of an already-started publication.
Turning off `review_fix_loop` alone does **not** turn off Autofix publication.
The glossary itself is not rewritten by Review Autofix.

Review does **not** necessarily create a fresh run every time. Matching content,
effective Review configuration/models, guidance, and glossary identity can reuse
a completed result or resume unfinished work. Changed relevant identities start
a new run; changing formal text through Autofix can also change the next Review's
input.

The terminal prints the result directory under
`state/<book>/targets/<target-language>/reviews/review-<timestamp>/`.
Read its `result.json` for the consolidated result; when Autofix is used,
`autofix/index.json` records publication decisions and before/after changes.
Model checks are not a replacement for human proofreading.
After publishing revisions, export again with `assemble`.
Concepts and safety boundaries are in [Review and Autofix](pipeline.md#review-and-autofix);
internal checkpoints and protocols belong in [pipeline development](../development/pipeline.md).

<a id="export"></a>

## Export existing translations

```bash
wenyi assemble book.epub
wenyi assemble book.epub --format txt --out output/reading-copy.txt
wenyi assemble book.epub --bilingual
wenyi assemble book.epub --no-mono --bilingual
```

`assemble` reads complete or partial saved translations without calling a model.
It accepts `--format`, `--out`, `--mono/--no-mono`, and
`--bilingual/--no-bilingual`, with the same meanings as `translate`.
Keep the matching original source available.

If another process is translating, export reads a consistent snapshot of already
saved work. Export again to include batches completed later. Output-only setting
changes normally need a fresh export, not retranslation.
BabelDOC PDF export still contacts its external bridge.
See the [format reference](../reference/formats.md) for defaults, bilingual
naming, style preservation, PDF requirements, and unsupported combinations.

<a id="automation"></a>

## Automation and command reference

<a id="command-reference"></a>

### Command map

All book commands take the original source path, not an exported translation.
Root options precede the command; command options follow it.
Use `wenyi --help`, `wenyi COMMAND --help`, or `wenyi models COMMAND --help`
for the installed version's syntax.

| Command | Arguments / options | Role |
|---|---|---|
| `parse` | `INPUT` | Parse and list source chapters; no model calls. |
| `prepare` | `INPUT` | Preparation only; may call models. |
| `translate` | `INPUT`, [translation options](#translation-options) | Full workflow or one chapter; repeat to resume. |
| `review` | `INPUT`, `--autofix/--no-autofix` | Whole-book Review with optional override of publication. |
| `assemble` | `INPUT`, [export options](#export) | Export saved translations; no model calls. |
| `status` | `INPUT` | Chapter progress and glossary statistics. |
| `inspect-run` | `INPUT`, `--verbose` | Saved failures, retries, timing, and usage. |
| `glossary list` | `INPUT` | Established terms and status. |
| `glossary conflicts` | `INPUT` | Unresolved terminology conflicts. |
| `glossary resolve` | `INPUT SOURCE TARGET` | Resolve an existing term. |
| `languages` | No options | List built-in language codes without credentials. |
| `models` | [Subcommands below](#model-commands) | Routing, checks, live probes, and usage-ledger conversion. |
| `language-policy` | Developer diagnostic | See [language-policy diagnostics](../development/pipeline.md#language-policies), not translation settings. |

Root options are `--config/-c PATH` (default `config.yaml`),
`--prompt-genre literary|academic` (default `literary`),
`--version`, and `--help`.
The retired `report` command is unavailable; use `status`, `inspect-run`, glossary
commands, and Review results instead.

### Model commands

| Command | Options | Network behavior |
|---|---|---|
| `models list` | `--json` | Effective routes; no credentials or requests. |
| `models explain` | Required `--operation ID` | JSON description of one route; no credentials or requests. |
| `models check` | `--for prepare\|translate\|review` (default `translate`) | Validate reachable credentials locally; no requests. |
| `models ping` | `--for prepare\|translate\|review`, `--timeout SECONDS` (default `30`, minimum `0.1`), `--json` | Live request per reachable profile, including fallbacks. |
| `models migrate-usage` | `RUN_DIR` | Offline conversion of selected CLI usage ledgers. |

`list --json`, `explain`, and `ping --json` print structured JSON to standard
output. Other tables/progress are human-oriented, not a stable parsing interface.
`check` and `ping` honor YAML workflow switches; `translate` applies its own
stage overrides before validating credentials.

`ping` is opt-in and consumes tokens, even though its usage is not recorded in a
book ledger. It makes a short request without retries, normally with an output
budget of 16 tokens or the provider minimum (for example 4096 for thinking).
An empty or truncated response still proves reachability, not task quality.
Any failed profile makes the command exit with status 1; failures are categorized
without echoing raw provider responses.

<a id="usage-ledgers"></a>

### Usage ledgers

```bash
wenyi models migrate-usage state/BOOK/targets/zh
```

Usage ledgers are run data and are not rebuilt with settings. Nonempty
pre-routing ledgers need this explicit conversion; unknown provider/model history
is marked `unknown`. Schema-2 cache accounting can be read and is persisted in
the current schema on the next normal save, without inventing missing historical
cache classifications.

Stop workflows for the selected target before converting usage. The command
processes that CLI target's ledger and its Review ledgers, preserves totals,
and keeps exact-byte `usage.before-routing-<timestamp>.json` backups beside
changed files. It validates before writing and publishes each converted file
atomically. Repeat the same command after interruption; already converted ledgers
are skipped. It does not migrate source books or Web/Desktop workspaces.

<a id="machine-output"></a>

### Machine progress and output receipts

```bash
wenyi translate book.epub \
  --progress-json /absolute/progress.json \
  --result-json /absolute/result.json
```

Both options are optional and specific to `translate`. They write
protocol-version-1 JSON by atomic replacement in the destination directory;
terminal output remains unchanged. Choose dedicated paths that differ from each
other, the source, the configuration, and all output files. The CLI rejects
collisions with the source, config, and explicit `--out` path.

Each invocation generates a fresh `run_id`. Files include `protocol_version`,
`run_id`, and `updated_at`.

| File / state | Fields and meaning |
|---|---|
| Progress while running | `status: "running"`, `done`, nullable `total`, `label`; counts describe the current stage, not overall percentage. |
| Result before preflight | `status: "running"`, empty `outputs`; replaces a previous receipt. |
| Successful result | `status: "success"`, `source_sha256`, absolute `state_dir`, and `outputs` containing absolute `path`, byte `size`, and `sha256`. |
| Failed result | `status: "failed"`, empty `outputs`, generic `error`; details remain in terminal/log output. |

Chapter-only success has an empty output list because it does not export.
For failures before receipt initialization, or failure to write the receipt, a
fresh receipt may be unavailable. A killed process can leave `running` behind.

A supervisor must check the **process exit status**, match the current `run_id`,
and verify file hashes before publishing attachments. Do not treat progress files
as proof of liveness. These files are not a queue, daemon, or cancellation API.
Paths, reports, and receipts can reveal private document information; inspect
them before sharing.

### Exit status

- `0`: command completed successfully. This does not certify translation quality
  or mean Review found no issues.
- `1`: expected operational/configuration failures, missing state or credentials,
  stopped request budgets, or a failed model connectivity probe.
- `2`: command-line usage/parameter errors, including unsupported output format
  values and out-of-range chapter selection.

For automation, treat any nonzero exit or forced termination as failure rather
than assuming every error belongs to a single category. Read the diagnostic and
receipt together, and resume with the original source/configuration after
addressing the cause.
