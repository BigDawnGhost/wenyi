# Web workflow layout

[简体中文](zh/web-interface.md) · [Interface languages](web-i18n.md)

Project navigation keeps **Progress**, **Manual proofreading**, **Whole-book review**,
and **Export** visible. **Project tools** groups Glossary, Style & synopsis, Project
settings & models, and Event log. Opening a tool URL expands the group and highlights
the current page. Desktop and mobile use the same navigation; subtitle projects show
the subtitle editor instead of book proofreading/review and omit glossary/style tools.
Browser interface language stays in global Settings.

The progress page owns start, pause, resume, preparation, and individual chapter
translation. It keeps translation progress, cumulative recorded runtime, token totals,
and the latest matching workflow event visible. Expand workflow details to inspect the
plan; expand accounting for per-model/provider/stage usage. The project report and
preparation controls are collapsed by default. Export generation and its options live
on the export page.

Manual proofreading lists chapters in rows with search, translation-status filtering,
and saved paragraph counts. Opening a chapter shows its source and saved translation;
polishing history and review notes expand on demand. Newly persisted translation batches
refresh automatically. Editing remains disabled during a running project task.

Whole-book review opens the latest run. Search its issue list and expand individual
evidence, suggested changes, publication records, or full technical details. Review
history is collapsed; selecting an older run shows a link back to the latest result.
The autofix checkbox remains visible because enabling it can update saved translations.

Export keeps format, monolingual/bilingual edition, generation, and downloads visible.
Layout options show their current values in a collapsed summary. Folding controls does
not reset them; a failed export request reopens the advanced options. Subtitles retain
their independent editor and export workflow.

Project settings show the model preset, tier selections, and common workflow switches.
Provider connections/model names, segmentation/performance, YAML, and saved operation
routes are collapsed with summaries. Operation overrides and fallbacks remain editable
through YAML; existing overrides are preserved when editing other fields. Validation
failures reopen advanced configuration so invalid draft values remain accessible.
The autofix switch stays visible. The PDF parser control appears only for PDF projects.

Punctuation normalization is an advanced export option for that export request; it does
not overwrite project defaults. Persistent export defaults remain in project YAML.
Export generation waits for those defaults to load successfully.

These changes organize existing operations without changing model prompts, task state,
review publication rules, or persisted translations.
