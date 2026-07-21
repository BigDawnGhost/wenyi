"""English messages for the command-line interface."""

MESSAGES: dict[str, str] = {
    "app.help": "A multilingual translation workflow for long-form fiction.",
    "glossary.help": "Inspect glossary terms, conflicts, and preferred translations.",
    "panel.workflow": "Workflow",
    "panel.quality": "Quality",
    "panel.output": "Status and output",
    "panel.glossary": "Glossary",
    "panel.options": "Options",
    "panel.arguments": "Arguments",
    "panel.commands": "Commands",
    "option.config": "Configuration file path; created automatically when missing.",
    "option.translate.input": (
        "Book to translate (EPUB / FB2 / TXT / Markdown / HTML / PDF)."
    ),
    "option.translate.chapter": (
        "Translate and save only this zero-based chapter; skip review, QA, report, "
        "and export."
    ),
    "option.translate.format": "Final format: epub / txt / html / markdown.",
    "option.output": (
        "Monolingual output path; defaults to the output directory beside the source."
    ),
    "option.polish": "Override pipeline.polish for post-translation polishing.",
    "option.review": "Override pipeline.review for the final whole-book review.",
    "option.qa": "Override pipeline.consistency_qa for cross-chapter QA.",
    "option.mono": "Override output.mono to generate a monolingual edition.",
    "option.bilingual": "Override output.bilingual to generate a parallel edition.",
    "option.prepare.input": (
        "Book to prepare (EPUB / FB2 / TXT / Markdown / HTML / PDF)."
    ),
    "option.review.input": "Source file whose full text has already been translated.",
    "option.review.force": "Ignore saved review summaries and review every chapter again.",
    "option.review.fix": (
        "Override pipeline.autofix_severe; fix omissions and mistranslations serially."
    ),
    "option.state.input": "Source file with existing translation state.",
    "option.glossary.source": "Source-language term to resolve.",
    "option.glossary.target": "Preferred target-language translation.",
    "option.assemble.input": "Source file with a complete or partial translation.",
    "option.assemble.format": "Export format: epub / txt / html / markdown.",
    "option.qa.input": "Source file with a completed translation.",
    "command.translate": (
        "Prepare, translate, optionally review and run QA, report, and export in one "
        "resumable command."
    ),
    "command.prepare": (
        "Parse the book, detect its language, analyze style and terms, and prescan it "
        "without translating the body."
    ),
    "command.review": (
        "Review the complete translation with the final glossary; results are resumable "
        "and saved per chapter."
    ),
    "command.status": "Show chapter progress and glossary statistics.",
    "command.glossary.list": "List preferred translations and statuses for this book.",
    "command.glossary.conflicts": (
        "List unresolved translation conflicts found during glossary extraction."
    ),
    "command.glossary.resolve": (
        "Set the preferred translation of an existing term and close its conflicts."
    ),
    "command.assemble": (
        "Regenerate output files from existing state without calling a model or "
        "retranslating."
    ),
    "command.qa": (
        "Run model-assisted cross-chapter consistency QA without changing the text."
    ),
    "command.report": (
        "Regenerate report.json from current chapter, review, and glossary state without "
        "calling a model."
    ),
    "error.input_missing": "Input file does not exist: {path}",
    "error.unsupported_format": (
        "Unsupported output format: {format} (choose epub / txt / html / markdown)"
    ),
    "error.manifest_source_missing": "The run manifest is missing source_lang.",
    "error.manifest_target_missing": "The run manifest is missing target_lang.",
    "error.source_mismatch": (
        "Source language does not match the existing run: configured {configured}, "
        "stored {stored}."
    ),
    "error.target_mismatch": (
        "Target language does not match the existing run: configured {configured}, "
        "stored {stored}."
    ),
    "error.state_source_language_missing": (
        "The run state does not contain a resolved source language."
    ),
    "error.state_target_language_missing": (
        "The run state does not contain an explicit target language."
    ),
    "error.identical_source_target": (
        "Source and target languages are the same ({language}); nothing to translate. "
        "Change language.source or language.target in config.yaml."
    ),
    "error.translation_state_missing": (
        "No translation state found. Run translate first."
    ),
    "error.language_detection_failed": (
        "Automatic source-language detection failed. Check the model configuration, "
        "or set language.source in config.yaml to an ISO 639-1 code such as "
        "ja, en, ko, ru, fr, de, or es."
    ),
    "error.chapter_not_found": (
        "Chapter {chapter} does not exist; available range: {range}"
    ),
    "error.review_incomplete": (
        "Whole-book review requires every chapter to be translated first; "
        "pending chapters: {chapters}"
    ),
    "error.translation_array_missing": (
        "The model did not return a translation array."
    ),
    "error.translation_count_mismatch": (
        "Translation count mismatch: expected {expected}, received {actual}."
    ),
    "error.translation_item_invalid": (
        "The model returned an empty or non-string translation."
    ),
    "error.translation_fallback_failed": (
        "Per-segment fallback translation failed at segment {index}."
    ),
    "error.punctuation_segment_count_mismatch": (
        "texts and continuations must contain the same number of items."
    ),
    "error.prefix": "Error: {error}",
    "error.chapter_finish_options": (
        "--chapter translates and saves one chapter only; it cannot be combined with "
        "finalization options: {options}"
    ),
    "progress.preparing": "Preparing…",
    "progress.review_preparing": "Preparing whole-book review…",
    "progress.locating_state": "Locating translation state…",
    "progress.parsing_document": "Parsing document…",
    "progress.detecting_language": "Detecting language…",
    "progress.analyzing_style": "Analyzing book style…",
    "progress.translation_complete": "Translation complete",
    "progress.prescan_chapters": "Prescanning chapter summaries",
    "progress.generating_overview": "Generating book overview…",
    "progress.translating_titles": "Translating chapter titles…",
    "progress.chapter_fallback": "Chapter {chapter}",
    "progress.review_chapter": "Whole-book review: {chapter}",
    "progress.consistency_qa": "Consistency QA…",
    "progress.generating_report": "Generating report…",
    "progress.assembling_translation": "Assembling translation…",
    "result.chapter_done": (
        "[green]Chapter {chapter} translated[/]. State directory: {path}"
    ),
    "result.translation_complete": (
        "[bold green]Complete[/]: {done}/{total} chapters, {reviewed}/{total} reviewed, "
        "{terms} terms, {issues} consistency issues."
    ),
    "result.translation_output": "Translation: [bold]{path}[/]",
    "result.prepare_complete": (
        "[bold green]Preparation complete[/]: {chapters} chapters parsed, "
        "{digests}/{chapters} prescanned, book overview {overview}."
    ),
    "result.overview_generated": "generated",
    "result.overview_missing": "not generated",
    "result.state_directory": "State directory: [bold]{path}[/]",
    "result.prepare_resume": (
        "Run translate with the same source file to continue with the full translation."
    ),
    "usage.total": (
        "Usage (cumulative for this book): {total:,} tok "
        "(prompt {prompt:,} / completion {completion:,}), cache hit rate {rate:.1%} "
        "(hit {hit:,} / miss {miss:,} tok)"
    ),
    "usage.tier": (
        "  · {tier}: {total:,} tok, {calls} calls, cache hit rate {rate:.1%}"
    ),
    "usage.stage": (
        "  · Stage {stage}: {total:,} tok (prompt {prompt:,} / completion "
        "{completion:,}), {calls} calls, cache hit rate {rate:.1%}"
    ),
    "result.review_complete": (
        "[bold green]Whole-book review complete[/]: {issues} issues found{fixed}."
    ),
    "result.review_fixed": "; severe issues were repaired where possible",
    "result.no_progress": "[yellow]No translation state found. Run prepare or translate first.[/]",
    "status.book": "{title} ({format})  {source}→{target}",
    "status.chapter": "Chapter",
    "status.translation": "Translation",
    "status.review": "Review",
    "status.glossary": "Glossary:",
    "glossary.source": "Source",
    "glossary.target": "Translation",
    "glossary.type": "Type",
    "glossary.status": "Status",
    "glossary.no_conflicts": "No unresolved glossary conflicts.",
    "glossary.conflict": (
        "  {source}: existing “{existing}” vs proposed “{proposed}” (chapter {chapter})"
    ),
    "glossary.not_found": "Term not found: {source}",
    "glossary.resolved": "Resolved {source} → {target}",
    "result.assembled": "Translation generated: [bold]{path}[/]",
    "qa.count": "{count} consistency issues:",
    "report.written": "QA report written to {path}",
    "report.summary": (
        "  Chapters {done}/{total}  Terms {terms}  Unresolved conflicts {conflicts}  "
        "Review issues {review}  Back-translation concerns {backtranslation}"
    ),
    "error.pdf_cache_dir_required": ("PDF input requires a run-state cache directory."),
    "error.input_format_unsupported": (
        "Unsupported input format: {format} (supported: {supported})"
    ),
    "error.epub_rootfile_missing": (
        "Invalid EPUB: container.xml has no valid rootfile full-path."
    ),
    "error.pdf_dependencies_missing": (
        "PDF conversion requires additional dependencies. Run:\n"
        "  uv pip install {packages}\n"
        "Alternatively, convert the PDF to HTML manually, save it as {cache_path} "
        "in this book's state directory, and run again."
    ),
    "error.pdf_conversion_failed": "PDF conversion failed: {error}",
    "error.provider_unknown": ("Unknown provider: {provider} (supported: {supported})"),
    "error.json_parse_failed": "Unable to parse JSON: {preview}",
    "error.llm_tier_model_missing": "llm.tiers.{tier}.model must not be empty.",
    "error.llm_strong_model_missing": (
        "Configuration is missing llm.tiers.strong.model."
    ),
    "error.provider_base_url_required": (
        "The {provider} provider requires llm.base_url."
    ),
    "error.openai_sdk_missing": (
        "The OpenAI SDK is required. Run `pip install openai`, or set llm.provider "
        "to fake for offline testing."
    ),
    "error.api_key_missing": (
        "Environment variable {env} is not set ({provider} API key)."
    ),
    "error.mineru_extraction_failed": ("MinerU extraction failed for {file}: {error}"),
    "error.mineru_timeout": "MinerU batch {batch} timed out.",
    "error.mineru_html_missing": ("The MinerU result ZIP contains no HTML file."),
    "error.mineru_api": "MinerU API error: code={code}, message={error}",
    "error.mineru_token_missing": (
        "No MinerU API token was provided and {env} is not set."
    ),
    "value.unknown": "unknown",
    "value.no_translatable_chapters": "no translatable chapters",
    "warning.review_index_invalid": (
        "Ignoring invalid review index {index}; the current review chunk contains "
        "{count} segments."
    ),
    "progress.pdf_pages": "PDF: {pages} page(s)",
    "progress.pdf_splitting": ("Splitting into chunks of at most {max_pages} pages…"),
    "progress.pdf_chunk": "  Chunk {current}/{total}: {pages} page(s)",
    "progress.pdf_uploading": "Uploading and extracting {chunks} chunk(s)…",
    "progress.pdf_chunk_done": (
        "  Chunk {current}/{total} complete ({chars:,} characters)"
    ),
    "progress.pdf_done": "Done → {path} ({chars:,} characters)",
    "pdf.cli_usage": (
        "Usage: uv run python pdf_to_html.py <pdf_path> [output_html_path]"
    ),
    "result.cancelled": "Cancelled.",
}
