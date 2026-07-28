# Wenyi (文译)

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Stars](https://img.shields.io/github/stars/BigDawnGhost/wenyi?style=social)](https://github.com/BigDawnGhost/wenyi)

**English** | [简体中文](docs/zh/README.md)

![Wenyi bilingual EPUB preview](docs/images/bilingual-preview.png)

> A command-line tool for translating long-form novels from multiple languages into Chinese.
> Whole-book analysis, real-time glossary, multi-stage review — one command, from EPUB to a readable Chinese translation.

---

## Table of contents

- [Why Wenyi](#why-wenyi)
- [Core features](#core-features)
- [Quick start](#quick-start)
- [Supported formats](#supported-formats)
- [Translation pipeline](#translation-pipeline)
- [Documentation](#documentation)
- [Limitations](#limitations)
- [Community](#community)
- [Star history](#star-history)
- [License](#license)

---

## Why Wenyi

| Typical approach | Wenyi |
|---|---|
| Segments translated in isolation, unaware of surrounding content | Whole-book prescan with chapter digests and rolling context |
| Glossary managed manually or as an afterthought | Real-time term extraction with conflict detection, fed back into subsequent batches |
| Single-pass translation, fragile to interruptions | Chapter-level state machine: resume any interrupted run with the same command |
| Raw model output, no systematic quality process | Translate → polish → review → backtranslate → consistency QA |

Wenyi is designed for **long-form texts** — novels, memoirs, biographies — where translating a sentence in chapter 3 demands knowledge of chapter 1, and a character's name must remain consistent across 500 pages.

---

## Core features

- **Whole-book understanding** — prescans the source before translation, creating per-chapter digests and a book-level synopsis injected into every batch
- **Real-time glossary** — extracts proper names, terms, and recurring expressions as translation progresses; detects conflicting translations and surfaces them for resolution
- **Multi-stage quality** — optional polishing (strong model), side-by-side review, backtranslation sampling, and cross-chapter consistency QA
- **Resumability** — chapter-level state machine with atomic writes; interrupt at any point and resume with the same command
- **8 LLM providers** — DeepSeek, OpenAI, OpenRouter, Google Gemini, Ollama, vLLM, and generic OpenAI-compatible endpoints
- **Native EPUB preservation** — writes translated text back into the original XHTML templates, preserving styles, images, TOC, and anchors
- **Bilingual output** — optional side-by-side edition with visually subdued source text, including dark mode support

---

## Quick start

### Prerequisites

Wenyi requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

### Installation

```bash
git clone git@github.com:BigDawnGhost/wenyi.git
cd wenyi
uv sync
```

### Configuration

Set your API key and optionally review the generated config file:

```bash
export DEEPSEEK_API_KEY=sk-...
# config.yaml is auto-created on first run if missing
```

### One-command translation

```bash
uv run trans-novel translate book.epub
```

This parses the book, detects the source language, prescans for understanding, translates all chapters, and assembles the output. The monolingual Chinese EPUB is written to `output/book.zh.epub` by default.

### Step-by-step workflow

```bash
# 1. Prepare — parse, analyze, prescan (no body text translated)
uv run trans-novel prepare book.epub

# 2. Translate — resume from the prepared state
uv run trans-novel translate book.epub

# 3. Review — independent final review against the completed glossary
uv run trans-novel review book.epub

# 4. Consistency QA
uv run trans-novel qa book.epub

# 5. Check progress
uv run trans-novel status book.epub
```

### Interrupt and resume

Every completed batch is persisted immediately. If a run is interrupted, execute the same command again:

```bash
uv run trans-novel translate book.epub
```

### Command-line overrides

```bash
uv run trans-novel translate book.epub --polish --review --qa     # enable all quality stages
uv run trans-novel translate book.epub --no-polish                 # disable polishing
uv run trans-novel translate book.epub --bilingual                 # produce both editions
uv run trans-novel translate book.epub --chapter 3                 # translate a single chapter
uv run trans-novel translate book.epub --format txt                # export as plain text
```

---

## Supported formats

| Input | Output |
|---|---|
| EPUB, FB2, TXT, Markdown, HTML, PDF | EPUB (monolingual / bilingual), TXT, HTML, Markdown |

- PDF input requires `MINERU_API_KEY` for the initial conversion; the resulting HTML is cached and reused.
- EPUB output preserves the original book's styles, images, table of contents, and anchors. Vertical layout is converted to horizontal for Chinese reading.
- Source language is auto-detected by default, or fixed to an ISO 639-1 code in `config.yaml`.

---

## Translation pipeline

```
  Input file
    ↓
  Parse chapters + detect language
    ↓
  Whole-book prescan (chapter digests + synopsis)  ← parallel
    ↓
  Style analysis + initial glossary
    ↓
  ┌─ Translate chapter by chapter ──────────────────────┐
  │  Per batch: inject context → translate               │
  │  → extract terms → polish → normalize punctuation    │
  │  → backtranslate sample → persist                    │
  └──────────────────────────────────────────────────────┘
    ↓
  Final review (against completed glossary)    ← optional, parallel
    ↓
  Cross-chapter consistency QA                  ← optional
    ↓
  Generate report + assemble output EPUB
```

The prescan runs in parallel (configurable concurrency) and is idempotent — completed digests are reused across runs. During translation, each batch receives the most recent glossary snapshot and translated context, keeping pronouns, terms, and tone consistent across chapters.

---

## Documentation

- [Usage guide](docs/usage.md) — installation, Windows setup, input/output, resumability, independent stages
- [Configuration](docs/configuration.md) — providers, languages, pipeline switches, segmentation, paths
- [Translation pipeline](docs/pipeline.md) — whole-book analysis, terminology, context, polishing, review
- [Contributing](CONTRIBUTING.md) — development, testing, and contribution guidelines

Translated state directories for public-domain books may be shared through [wenyi-bookcase](https://github.com/BigDawnGhost/wenyi-bookcase). Do not publish copyrighted text, private books, or `state/` directories containing sensitive information without permission.

---

## Limitations

- The translation pipeline is optimized for Simplified Chinese output; other target languages are not supported.
- Polishing and final review are the most expensive stages — they significantly increase token consumption.
- PDF input depends on the MinerU external service; the initial conversion requires an API key.
- Translation quality is bounded by the capabilities of the chosen LLM model.
- Very long books may produce large state directories; storage requirements grow with book length.

---

## Community

- [Discord server](https://discord.gg/Tybfva4HT)
- QQ group: 1055065098
- [GitHub Issues](https://github.com/BigDawnGhost/wenyi/issues) — bug reports and feature requests
- [GitHub Discussions](https://github.com/BigDawnGhost/wenyi/discussions) — ideas and questions

---

## Star history

<a href="https://www.star-history.com/?repos=BigDawnGhost%2FWenyi&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=BigDawnGhost/Wenyi&type=date&theme=dark&legend=top-left&sealed_token=VFuKZdjDh-9e2mG4qlvqeSpCkWCoRf9ZRy0hIDLdaECFQeoNNlQ20QxSD4PuvTZp1RJg7J2s5hr57Eq66paMrhikuuI3kc41uZZCYb-bTqsUafeSB7AVdhw7bmz70NhkVXABHtSIHdw0DROZaInmznYJ651gP2klEeW8OOM8EkfJnXgDld6f0xn8mIJ9" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=BigDawnGhost/Wenyi&type=date&legend=top-left&sealed_token=VFuKZdjDh-9e2mG4qlvqeSpCkWCoRf9ZRy0hIDLdaECFQeoNNlQ20QxSD4PuvTZp1RJg7J2s5hr57Eq66paMrhikuuI3kc41uZZCYb-bTqsUafeSB7AVdhw7bmz70NhkVXABHtSIHdw0DROZaInmznYJ651gP2klEeW8OOM8EkfJnXgDld6f0xn8mIJ9" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=BigDawnGhost/Wenyi&type=date&legend=top-left&sealed_token=VFuKZdjDh-9e2mG4qlvqeSpCkWCoRf9ZRy0hIDLdaECFQeoNNlQ20QxSD4PuvTZp1RJg7J2s5hr57Eq66paMrhikuuI3kc41uZZCYb-bTqsUafeSB7AVdhw7bmz70NhkVXABHtSIHdw0DROZaInmznYJ651gP2klEeW8OOM8EkfJnXgDld6f0xn8mIJ9" />
 </picture>
</a>

---

## License

[MIT](LICENSE)