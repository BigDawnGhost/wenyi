# Usage guide

[简体中文](zh/usage.md)

## Installation and first run

Running from source requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
export DEEPSEEK_API_KEY=sk-...
uv run trans-novel --version
uv run trans-novel translate book.epub
```

The displayed version is generated from the repository's Git tags. Tagged builds show the
release version; development builds include their commit distance and hash.

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
- `--format txt|html|markdown|pdf`: export the selected format. Every input format still produces EPUB by default.
- For EPUB input, Wenyi attempts to write translated text back into the original XHTML templates while preserving styles, images, the table of contents, and anchors.
- The bilingual edition displays the translation and source text together. The source is visually subdued by default; set `output.bilingual_preserve_source_style: true` to inherit the book's normal text style. Their order is controlled by `output.bilingual_order`.
- EPUB output includes an “About this translation” page by default. Set `output.about_page: false` to disable it.
- Runtime data is stored under `state/`, including chapter intermediates, the SQLite glossary, usage data, and reports.

### Experimental PDF support

PDF input and PDF output are both experimental.

#### PDF input

The first PDF import requires `MINERU_API_KEY`:

```bash
export MINERU_API_KEY=...
uv run trans-novel translate book.pdf
```

MinerU's converted HTML is saved at `state/<book>/source/converted.html`.
Later runs reuse this file, and you may correct it manually before resuming.

#### PDF output

WeasyPrint is the default PDF engine. Install its optional dependency and omit
`--pdf-engine`:

```bash
uv sync --extra pdf-output
uv run trans-novel assemble book.html --format pdf
```

For a lightweight cross-platform engine without system rendering libraries,
use `fpdf2`:

```bash
uv sync --extra pdf-output-lite
uv run trans-novel assemble book.html --format pdf --pdf-engine fpdf2
```

`fpdf2` supports basic layout and images, but only a limited HTML/CSS subset.
Images mixed with text are placed as separate blocks. It uses a discoverable
CJK system font; if none is found, set `TRANS_NOVEL_PDF_FONT` to a TTF, OTF, or
TTC font file. This option also works on Windows.

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

Changing polishing settings does not automatically rerun translation batches that
are already complete. Experimental Review is different: every `review` invocation
rechecks the complete translated book and creates a new timestamped debug run.
Use a new state directory or remove the corresponding state only when you
intentionally want a fresh translation.

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

`review` checks the complete translated book using the final glossary. Its
unchanged initial Reviewer runs over contiguous chunks concurrently; candidates
can then enter a bounded evidence loop, and contradictory cross-chunk consistency
suggestions can receive a final recommendation. Review never fixes the body and
does not update the manifest, chapter JSON, `report.json`, the formal event log, or
the formal `usage.json`. Each run writes prompts, raw responses, parsed actions,
requested evidence, events, suggestions, and its model-usage delta to
`state/<book>/debug/review-<timestamp>/`.
The debug directory's `usage.json` includes totals plus `by_tier` and `by_stage`
breakdowns and is retained on both success and failure.

`qa` and `report` collect problems without modifying translated text. `assemble`
rebuilds output from existing state without calling the model again.
