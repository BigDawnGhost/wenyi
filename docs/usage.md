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
- The bilingual edition displays the translation and a visually subdued copy of the source text. Their order is controlled by `output.bilingual_order`.
- EPUB output includes an “About this translation” page by default. Set `output.about_page: false` to disable it.
- Runtime data is stored under `state/`, including chapter intermediates, the SQLite glossary, usage data, and reports.

## Common commands

```bash
# Translate, translate one chapter, or export plain text
uv run trans-novel translate book.epub
uv run trans-novel translate book.epub --chapter 3
uv run trans-novel translate book.epub --format txt
uv run trans-novel translate book.pdf

# Override polishing and whole-book QA settings
uv run trans-novel translate book.epub --polish --qa
uv run trans-novel translate book.epub --no-polish --no-qa

# Produce both editions, or only the bilingual edition
uv run trans-novel translate book.epub --bilingual
uv run trans-novel translate book.epub --no-mono --bilingual
```

## Interrupting and resuming

Every completed batch is written to the state directory. To resume after an interruption, run the same source file again:

```bash
uv run trans-novel resume book.epub
uv run trans-novel status book.epub
```

Changing polishing or review settings does not automatically rerun batches that are already complete. Use a new state directory or remove the corresponding state when you intentionally want a fresh translation.

## Utility commands

```bash
uv run trans-novel tools glossary book.epub list
uv run trans-novel tools glossary book.epub conflicts
uv run trans-novel tools qa book.epub
uv run trans-novel tools report book.epub
uv run trans-novel tools assemble book.epub
```

`qa` and `report` collect problems without modifying translated text. `assemble` rebuilds output from existing state without calling the model again.
