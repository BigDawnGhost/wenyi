# Contributing to Wenyi

**English** | [简体中文](docs/zh/CONTRIBUTING.md)

Thank you for helping improve Wenyi. The project prioritizes the quality and reliability of long-form novel translation.

## What you can contribute

Contributions are welcome in the following areas:

- Input parsing: compatibility improvements and new support for EPUB, FB2, TXT, DOCX, SRT, and related formats.
- Translation pipeline: context handling, terminology, review, polishing, and consistency checks. Subtitle work belongs in `trans_novel.srt`, not the book Orchestrator.
- Export: EPUB/DOCX output, tables of contents, metadata, layout preservation, and SRT writers.
- Tests: real-world failure cases, regression tests, and offline fake-LLM tests.
- Documentation: usage instructions, configuration explanations, troubleshooting, and translations.

Changes to the core translation pipeline can affect translation quality in subtle ways. Before proposing such a change, test it on a public-domain novel of at least 50,000 words and include a before-and-after comparison that explains the quality impact.
