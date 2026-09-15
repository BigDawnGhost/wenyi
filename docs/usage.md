# CLI usage

[简体中文](zh/usage.md) · [Web UI](web.md) · [Configuration](configuration.md)

## Install and configure

```bash
uv sync --package wenyi-cli
export DEEPSEEK_API_KEY=your-key
uv run trans-novel --help
uv run trans-novel languages
```

The first invocation creates a default configuration if the selected file does not exist. Supply a configuration before the command: `uv run trans-novel --config custom.yaml translate book.epub`. Credentials belong in the environment variable named by each provider configuration.

`language.source` accepts `auto`; `language.target` must be a supported concrete language. Identical source and target languages are rejected. Explicitly setting the source language avoids an initial detection call.

## Book workflow

```bash
# Preparation only: parsing, language, style, initial terms, optional synopsis.
uv run trans-novel prepare book.epub
# Translate pending text, polish, review, publish fixes, report and export.
uv run trans-novel translate book.epub
# Export both target-only and bilingual editions.
uv run trans-novel translate book.epub --bilingual
# Translate only the selected chapter (zero-based).
uv run trans-novel translate book.epub --chapter 0
# Control optional stages for this invocation.
uv run trans-novel translate book.epub --no-polish --no-review
```

The configuration defaults enable whole-book understanding, polishing, final Review and Autofix. Disable `pipeline.book_understanding` in YAML for a faster run without prescan. The Web **快速出稿** preset disables understanding, polishing, Review and Autofix together.

Supported book inputs are EPUB, DOCX, FB2, TXT, Markdown, HTML and PDF. `prepare` can make paid model calls for language detection, analysis and synopsis even though it does not translate the body. Web upload preview is a separate parser task and does not call translation models.

### Interrupt and resume

Stop the command and repeat it with the same source, target language and configuration. Completed translation batches, glossary extraction checkpoints, chapters and context are retained. Polishing saves both the pre-polish translation and the final target. Missing glossary checkpoints are recovered without translating already saved text again.

State is stored under `state/<book>/targets/<language>/`. The source SHA-256 must match: replacing the source with different content requires a fresh run directory. Another target language gets its own directory. Web projects bind a single target language and source identity after initialization; create a new project to change either.

## Review and Autofix

```bash
# Use pipeline.review_autofix (true by default).
uv run trans-novel review book.epub
# Recommendations only; do not publish changes into formal targets.
uv run trans-novel review book.epub --no-autofix
# Explicitly enable publication for this invocation.
uv run trans-novel review book.epub --autofix
```

Every chapter must be translated before whole-book Review. The review engine works on a snapshot and a shadow translation; it checks evidence, arbitrates contradictory proposals, revises the shadow and runs blind rechecks. The separate Autofix stage publishes accepted changes using before/after hashes and a saved publication index.

**Review does not always create a new run.** If translated content, Review configuration/model routes and the glossary fingerprint match, a completed result is reused or an interrupted running review resumes its checkpoints. Changed relevant inputs create a new run. An unfinished Autofix publication is recovered before another review, and changed formal text is preserved rather than overwritten by an obsolete fix.

`review --no-autofix` applies to publication, not to the shadow-review loop: it can still calculate suggested revisions. The run stores issues, suggested changes, actual publication records, failure reasons, evidence, checkpoints and usage. A zero issue count is meaningful only together with the review status and completion result.

## Inspect state and terminology

```bash
uv run trans-novel status book.epub
uv run trans-novel report book.epub
uv run trans-novel glossary list book.epub
uv run trans-novel glossary conflicts book.epub
uv run trans-novel glossary resolve --help
```

Glossary extraction retains established mappings and records conflicting proposals. Resolve conflicts explicitly; terms are stored in insertion order for stable prompt prefixes. Usage accumulates across invocations and timing records describe completed, interrupted or failed runs.

## Export and optional PDF services

```bash
uv run trans-novel assemble book.epub
uv run trans-novel assemble book.epub --format html
uv run trans-novel assemble book.epub --format docx
uv run trans-novel assemble book.epub --format pdf --pdf-engine weasyprint
uv run trans-novel assemble book.epub --format pdf --pdf-engine fpdf2
uv run trans-novel translate manuscript.docx
```

Formats: `epub`, `txt`, `html`, `markdown`, `docx`, `pdf`. A DOCX CLI input defaults to DOCX output. A PDF parsed through BabelDOC defaults to PDF; MinerU and ordinary book inputs default to EPUB unless a format is supplied. Filenames include the target language, and bilingual filenames include `-bi`.

A standalone `assemble` takes a consistent snapshot under a short state lock and renders after releasing that lock. It can export saved progress while translation continues. Export punctuation normalization changes only the export copy. EPUB/HTML preserve supported resources, navigation and annotations; DOCX preserves supported styles, lists and tables. Complex inputs may still need visual inspection.

MinerU PDF parsing requires `MINERU_API_KEY`; conversion results are cached by source/configuration. Set `pipeline.pdf_backend: babeldoc` and a reachable `pipeline.babeldoc_bridge_url` for an independently deployed BabelDOC HTTP bridge. PDF output requires the appropriate extra and system fonts/libraries; see [Web deployment](web.md) for the container defaults.

## Subtitles

```bash
uv run trans-novel translate captions.srt
uv run trans-novel translate captions.srt --bilingual
```

SRT uses overlapping concurrent translation windows, cue state and batch caches in `state/srt/<name>/targets/<language>/`. It preserves cue numbers and timestamps and produces target-only and/or bilingual SRT files. Repeating the command resumes pending work; completed cues and Web manual edits take precedence over old batch responses. Subtitles do not run book understanding, glossary extraction or whole-book Review.

## Model inspection and comparison

```bash
uv run trans-novel models list
uv run trans-novel models explain --operation review.scan
uv run trans-novel models check --workflow translate
uv run trans-novel models compare --operation translation.body \
  --model default_strong --messages messages.json --out comparison.json
```

`messages.json` is a JSON array of `role`/`content` objects. Repeat `--model` to compare named profiles. Comparison explicitly sends these messages to the selected models and records outputs, elapsed time and usage. Route inspection does not send a comparison request.

The supported pipeline no longer includes a separate `qa` command or backtranslation sampling. Old `do_qa`, `autofix_severe`, `review_agent_tier`, `force` and character-budget settings are not part of the current API/configuration; use Review Autofix, operation routes and token budgets.
