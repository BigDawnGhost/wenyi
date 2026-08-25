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

Windows releases provide `wenyi-windows-x64.zip`. Verify the archive against
`SHA256SUMS.txt` before running it.

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

## Linux

Releases provide `wenyi-linux-x64.tar.gz` and `wenyi-linux-arm64.tar.gz`. Download
the archive matching your processor, verify it against `SHA256SUMS.txt`, and run:

```bash
tar -xzf wenyi-linux-arm64.tar.gz  # use wenyi-linux-x64.tar.gz on x64 systems
chmod +x wenyi
export DEEPSEEK_API_KEY=sk-...
./wenyi translate book.epub
```

## macOS

Releases provide separate terminal executables for Apple Silicon (`wenyi-macos-arm64.tar.gz`)
and Intel (`wenyi-macos-x64.tar.gz`) Macs. Download the archive matching your processor,
verify it against `SHA256SUMS.txt`, and run:

```bash
tar -xzf wenyi-macos-arm64.tar.gz  # use wenyi-macos-x64.tar.gz on Intel Macs
chmod +x wenyi
export DEEPSEEK_API_KEY=sk-...
./wenyi translate book.epub
```

These command-line executables are ad-hoc signed by PyInstaller but are not notarized with an
Apple Developer certificate. macOS may quarantine a downloaded build; after verifying the
checksum, approve it in **System Settings → Privacy & Security** if prompted.

## Input and output

- Input formats: EPUB, FB2, TXT, Markdown, HTML, PDF, DOCX, and SRT.
- Default book output: a monolingual `<book-name>.zh.epub` under the source file's `output/` directory (`.docx` inputs default to `<book-name>.zh.docx` instead). The bilingual `*.zh-bi.*` edition is optional.
- `--format epub|txt|html|markdown|pdf|docx`: export the selected format for book inputs. When omitted, `.docx` → `docx` and other books → `epub`. This flag does not apply to SRT.
- For EPUB input, Wenyi attempts to write translated text back into the original XHTML templates while preserving styles, images, the table of contents, and anchors.
- The bilingual edition displays the translation and source text together. The source is visually subdued by default; set `output.bilingual_preserve_source_style: true` to inherit the book's normal text style. Their order is controlled by `output.bilingual_order`.
- EPUB output includes an “About this translation” page by default. Set `output.about_page: false` to disable it.
- Book runtime data is stored under `state/<book>/`, including chapter intermediates, the SQLite glossary, usage data, and reports. Subtitle runs use a separate tree under `state/srt/` (see [SRT subtitles](#srt-subtitles)).

### Experimental PDF support

PDF input and PDF output are both experimental.

#### PDF input

The first PDF import requires `MINERU_API_KEY`:

```bash
export MINERU_API_KEY=...
uv run trans-novel translate book.pdf
```

MinerU's converted HTML is saved at
`state/<book>/source/<source-sha256>/converted.html`. The content-addressed
directory prevents an interrupted run from reusing another PDF's conversion.
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

## DOCX (Word)

`translate book.docx` uses the full book Orchestrator (glossary, polish, review, resume under `state/<slug>/`).

**Structure**

- Paragraphs and heading styles (`Heading 1`–`9` / outline levels); level-1 headings start chapters.
- Simple tables are rebuilt cell-by-cell (no merged cells / nested tables in v1).
- Word automatic lists (`numPr`) become List Number / List Bullet groups (restart per source list id).
- Contents-style lines that already include a visible prefix such as `1. Title` are **not** auto-numbered again (avoids double numbering).

**Styles**

- Keeps bold / italic / underline / color / size, paragraph alignment, and shading.
- Uniform runs: apply on export with **no** extra model call.
- Mixed runs: after translate, each meaningful span is positioned alone (EPUB-annotation-style markers); bold/color and other attrs are **inherited from the source item**. Failed spans fall back proportionally without discarding the whole paragraph.
- Font/size-only run splits are ignored for alignment (noise).
- Translated Chinese uses **Song (宋体)**; untranslated source text and bilingual source lines do **not** force Song.
- Default Heading theme blue is neutralized unless the source set an explicit color.

**Output**

- Default: `output/<stem>.zh.docx` (Navigation pane via heading outline). Override with `--format epub` (etc.).

```bash
uv run trans-novel translate book.docx
uv run trans-novel translate book.docx --bilingual
uv run trans-novel translate book.docx --format epub
```

## SRT subtitles

`translate` routes `.srt` files automatically. The subtitle path is intentionally
lighter than the book pipeline:

- sliding windows of 20 cues with overlap 10, up to 100 concurrent strong-tier calls;
- no glossary, polishing, or whole-book review;
- `--chapter`, `--polish`, `--review`, and `--format` are ignored or rejected where they do not apply;
- monolingual `output/<stem>.zh.srt` by default; add `--bilingual` for `.zh-bi.srt`.

```bash
uv run trans-novel translate movie.srt
uv run trans-novel translate movie.srt --bilingual
uv run trans-novel translate movie.srt --no-mono --bilingual
```

Resume by running the same source file again. Cached batches under
`state/srt/<slug>/batches/` are skipped. Layout:

```text
state/srt/<slug>/
  manifest.json    # source identity, cue counts, window settings
  cues.jsonl       # one cue per line: index, timestamp, source, target, status
  batches/         # raw model results for resume
  usage.json       # cumulative token usage across resumes
  events.jsonl     # run events and LLM retry observations
```

There is no `glossary.db` or `reviews/` tree for subtitles. Package code lives in
`trans_novel.srt` (store + translate), with I/O in `ingest.srt_reader` and
`assemble.srt_writer`.

## Per-run metrics

`state/<book>/usage.json` remains the cumulative token total for the book.
`translate`, `prepare`, `review`, `assemble`, and `report` each write an independent
`state/<book>/run_metrics/<run-id>.json` record with:

- the input SHA-256, configuration, package, and Git revision fingerprints;
- invocation options such as a selected chapter, output format, and PDF engine;
- requested stages, completion or failure status, and per-stage wall time;
- only the LLM calls and tokens added by that invocation; and
- ending chapter and segment completion counts.

Every resume creates a new record, so clean runs from different branches can be
compared without mixing their costs. Records omit the full source path and book
text, redact sensitive option values, and store only an exception type on
failure.

New manifests store `source_sha256` instead of an absolute source path. Wenyi
rejects a same-title state directory when its recorded hash does not match the
current input. Manifests created by older versions must be rebuilt.

## Common commands

```bash
# Run the complete workflow, translate one chapter, or prepare without translating
uv run trans-novel translate book.epub
uv run trans-novel translate book.epub --chapter 3
uv run trans-novel translate book.epub --format txt
uv run trans-novel prepare book.epub
uv run trans-novel translate book.pdf
uv run trans-novel translate movie.srt

# Override polishing and final review settings
uv run trans-novel translate book.epub --polish --review
uv run trans-novel translate book.epub --no-polish --no-review

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
are already complete. Review is different: every `review` invocation rechecks the
complete translated book and creates a new timestamped read-only review run.
Use a new state directory or remove the corresponding state only when you
intentionally want a fresh translation.

## Independent stages and glossary management

```bash
uv run trans-novel review book.epub
uv run trans-novel glossary list book.epub
uv run trans-novel glossary conflicts book.epub
uv run trans-novel glossary resolve book.epub "source term" "chosen translation"
uv run trans-novel report book.epub
uv run trans-novel assemble book.epub
```

`review` checks the complete translated book using the final glossary. Its
unchanged initial Reviewer runs over contiguous chunks concurrently; candidates
can then enter a bounded evidence loop, and contradictory cross-chunk consistency
suggestions can receive a final recommendation. Confirmed issues may generate
provisional full-segment replacements in a run-local shadow translation. Every
Fixer in a round reads the same immutable snapshot; the next whole-book pass
blindly reviews the resulting shadow text without receiving prior issue
explanations. Review never writes these replacements to the manifest, chapter
JSON, or glossary. Each run writes one user-facing `result.json`, its model-usage
delta, an event stream, and internal round traces to
`state/<book>/reviews/review-<timestamp>/`. The same usage delta is also added once
to the book's cumulative `usage.json`; `report.json` contains only a compact
read-only review summary.

`report` summarizes the current translation and read-only Review result without
modifying translated text. `assemble` rebuilds output from existing state without
calling the model again. If another terminal is still translating, export uses a
consistent snapshot of the batches already persisted when the command starts; run
it again to include batches completed afterward.
