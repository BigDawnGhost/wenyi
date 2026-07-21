# Usage guide

[简体中文](zh/usage.md)

## Installation and first run

Running from source requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
export DEEPSEEK_API_KEY=sk-...
uv run trans-novel translate book.epub
```

Whenever the program starts, it checks for `config.yaml` in the current directory and creates a documented default file when it is missing. Review the model settings before starting a real translation.

## Windows

When using a packaged `wenyi.exe`, set the API key in PowerShell:

```powershell
# Current PowerShell session only
$env:DEEPSEEK_API_KEY = "sk-..."
.\wenyi.exe translate .\book.epub
```

To save the environment variable permanently, run the following command and then open a new PowerShell window:

```powershell
setx DEEPSEEK_API_KEY "sk-..."
```

You may also set `language.source` to a known ISO language code to avoid an additional model call for language detection.

## Input and output

- Input formats: EPUB, FB2, TXT, Markdown, HTML, and PDF.
- Default output: a monolingual `<book-name>.zh.epub` under the source file's `output/` directory. The bilingual `<book-name>.zh-bi.epub` is optional.
- `--format txt|html|markdown`: export the selected format. Every input format still produces EPUB by default.
- The first PDF import requires `MINERU_API_KEY`. Converted HTML is saved at `state/<book>/source/converted.html`, reused on later runs, and may be corrected manually before resuming.
- For EPUB input, Wenyi attempts to write translated text back into the original XHTML templates while preserving styles, images, the table of contents, and anchors.
- The bilingual edition displays the translation and source text together. The source is visually subdued by default; set `output.bilingual_preserve_source_style: true` to inherit the book's normal text style. Their order is controlled by `output.bilingual_order`.
- EPUB output includes an “About this translation” page by default. Set `output.about_page: false` to disable it.
- Runtime data is stored under `state/`, including chapter intermediates, the SQLite glossary, usage data, and reports.

## Common commands

```bash
# Run the complete workflow, translate one chapter, or prepare without translating
uv run trans-novel translate book.epub
uv run trans-novel translate book.epub --chapter 3
uv run trans-novel translate book.epub --format txt
uv run trans-novel prepare book.epub
uv run trans-novel translate book.pdf

# Override polishing, final review, and whole-book QA settings
uv run trans-novel translate book.epub --polish --review --qa
uv run trans-novel translate book.epub --no-polish --no-review --no-qa

# Produce both editions, or only the bilingual edition
uv run trans-novel translate book.epub --bilingual
uv run trans-novel translate book.epub --no-mono --bilingual
```

`prepare` parses the book, detects its language, generates the style guide and initial glossary, and completes the configured whole-book prescan without translating any body text. Run `translate` with the same source file to continue from the saved state.

## Interrupting and resuming

Every completed batch is written to the state directory. To resume after an interruption, run the same source file again:

```bash
uv run trans-novel translate book.epub
uv run trans-novel status book.epub
```

Changing polishing settings does not automatically rerun translation batches that are already complete. Final review has its own persisted state and can be repeated independently with `review --force`; use a new state directory or remove the corresponding state only when you intentionally want a fresh translation.

## Independent stages and glossary management

```bash
uv run trans-novel review book.epub
uv run trans-novel glossary list book.epub
uv run trans-novel glossary conflicts book.epub
uv run trans-novel glossary resolve book.epub "source term" "chosen translation"
uv run trans-novel qa book.epub
uv run trans-novel report book.epub
uv run trans-novel assemble book.epub
```

`review` checks the complete translated book using the final glossary; add `--force` to recheck unchanged chapters or `--fix` to apply validated severe fixes. `qa` and `report` collect problems without modifying translated text. `assemble` rebuilds output from existing state without calling the model again.
