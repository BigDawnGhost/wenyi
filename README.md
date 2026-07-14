# Wenyi

**English** | [简体中文](docs/zh/README.md)

![Wenyi bilingual EPUB preview](docs/images/bilingual-preview.png)

Wenyi is a command-line tool for translating EPUB, FB2, and TXT novels from multiple languages into Chinese. It focuses on long-form translation quality through whole-book analysis, rolling context, an evolving glossary, polishing, and review stages.

## Quick start

Wenyi requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
export DEEPSEEK_API_KEY=sk-...
uv run trans-novel translate book.epub
```

By default, Wenyi writes a monolingual Chinese EPUB to the source file's `output/` directory as `book.zh.epub`. A bilingual source-and-translation edition can be enabled when needed. Runtime state, chapter JSON files, the glossary database, and reports are stored under `state/`. To continue an interrupted run:

```bash
uv run trans-novel resume book.epub
uv run trans-novel status book.epub
```

## Supported formats and output

- Input: EPUB, FB2, and TXT.
- Output: monolingual EPUB by default, optional bilingual EPUB, or plain text with `--format txt`.
- EPUB preservation: Wenyi attempts to retain the original styles, images, table of contents, and anchors while converting translated content to horizontal layout.
- Language detection: the source language is detected automatically by default, or it can be fixed to an ISO language code in `config.yaml`.

Select output editions from the command line:

```bash
uv run trans-novel translate book.epub --bilingual           # monolingual and bilingual
uv run trans-novel translate book.epub --no-mono --bilingual # bilingual only
```

The bilingual edition places the translation before the source text by default. Set `output.bilingual_order` to `source_first` in `config.yaml` to reverse the order.

## Documentation

- [Usage guide](docs/usage.md): installation, Windows setup, input and output, resuming, and utility commands.
- [Configuration](docs/configuration.md): providers, languages, pipeline switches, segmentation, and paths.
- [Translation pipeline](docs/pipeline.md): whole-book analysis, terminology, context, polishing, review, and resumability.
- [Contributing](CONTRIBUTING.md): development, testing, and contribution guidelines.

Translated state directories for public-domain books may be shared through [wenyi-bookcase](https://github.com/BigDawnGhost/wenyi-bookcase). Do not publish copyrighted text, private books, or `state/` directories containing sensitive information without permission.

## Project status

Wenyi is an early-stage personal project focused on making long-form machine translation more accurate, consistent, and readable. Reports of inconsistent names, recurring expressions, omissions, formatting problems, and provider compatibility issues are welcome through GitHub Issues and Discussions. Pull requests are also appreciated.

Community:

- [Join the Wenyi Discord server](https://discord.gg/Tybfva4HT)
- QQ group: 1055065098

## Star history

<a href="https://www.star-history.com/?repos=BigDawnGhost%2FWenyi&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=BigDawnGhost/Wenyi&type=date&theme=dark&legend=top-left&sealed_token=VFuKZdjDh-9e2mG4qlvqeSpCkWCoRf9ZRy0hIDLdaECFQeoNNlQ20QxSD4PuvTZp1RJg7J2s5hr57Eq66paMrhikuuI3kc41uZZCYb-bTqsUafeSB7AVdhw7bmz70NhkVXABHtSIHdw0DROZaInmznYJ651gP2klEeW8OOM8EkfJnXgDld6f0xn8mIJ9" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=BigDawnGhost/Wenyi&type=date&legend=top-left&sealed_token=VFuKZdjDh-9e2mG4qlvqeSpCkWCoRf9ZRy0hIDLdaECFQeoNNlQ20QxSD4PuvTZp1RJg7J2s5hr57Eq66paMrhikuuI3kc41uZZCYb-bTqsUafeSB7AVdhw7bmz70NhkVXABHtSIHdw0DROZaInmznYJ651gP2klEeW8OOM8EkfJnXgDld6f0xn8mIJ9" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=BigDawnGhost/Wenyi&type=date&legend=top-left&sealed_token=VFuKZdjDh-9e2mG4qlvqeSpCkWCoRf9ZRy0hIDLdaECFQeoNNlQ20QxSD4PuvTZp1RJg7J2s5hr57Eq66paMrhikuuI3kc41uZZCYb-bTqsUafeSB7AVdhw7bmz70NhkVXABHtSIHdw0DROZaInmznYJ651gP2klEeW8OOM8EkfJnXgDld6f0xn8mIJ9" />
 </picture>
</a>
