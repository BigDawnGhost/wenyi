# Web workflow layout

[简体中文](zh/web-interface.md) · [Interface languages](web-i18n.md)

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

These changes organize existing operations without changing model prompts, task state,
review publication rules, or persisted translations.
