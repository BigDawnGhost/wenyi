# Web workflow layout

[简体中文](zh/web-interface.md) · [Interface languages](web-i18n.md)

Project navigation lists **Translation overview**, **Manual proofreading**, **Whole-book
review**, **Glossary**, **Style & synopsis**, **Export**, **Project settings & models**,
and **Event log** at the same level, with the current page highlighted. Desktop and
mobile use the same navigation; subtitle projects show
the subtitle editor instead of book proofreading/review and omit glossary/style tools.
Browser interface language stays in global Settings.
Project pages appear at the top of the desktop sidebar; Projects,
Create project, and Settings stay at its bottom. On mobile, these global links follow
the project navigation. Long project menus scroll independently of the global links.

Creating a project requires a nonempty source file. Choose its languages and workflow,
select the file, and optionally check **Prepare before translating** (off by default).
The upload and project metadata are sent together; a failed upload does not create an
empty project. PDF files expose their parser selection before submission. The server
then generates a preview, or generates the preview and prepares the book/glossary when
the checkbox is selected. Preparation uses model tokens and the configured default
models; leave it unchecked to adjust project settings before starting translation.
Subtitles only generate a preview and have no book-preparation checkbox.

Parsing and preparation continue if the page is closed. Reopening the creation URL
restores its filename and progress; the preview appearing does not end preparation.
If queueing fails after the source is saved, the project keeps the source and exposes
**Resume task**. Starting translation still performs any required preparation.

The translation overview owns start, pause, resume, and individual chapter
translation. It keeps translation progress, cumulative recorded runtime, token totals,
and the latest matching workflow event visible. Expand workflow details to inspect the
plan. Accounting always shows usage by model/provider/stage and
run-duration charts. The project report is collapsed by default. Export generation and its options live
on the export page.

Accounting keeps total tokens, request count, cache hit rate, and recorded runtime
visible, with no accounting collapse control. Status badges use pale backgrounds with
colored text. The model view combines records with the same provider/model name across
inference configurations, without rewriting the usage ledger. Usage starts with the
three grouping controls, without a second total-token chart. Each row distinguishes
cached input, uncached input, and output, with counts and a cache hit rate below the bar.
The rate is cached input divided by cached plus uncached input; incomplete cache data
shows a dash and unknown input is separate from cache misses. Switching grouping never
adds the independent views together. Run history lists
the latest runs first, with localized status, start time, and duration; earlier runs
remain available. Timing measures whole runs including waiting and I/O, excludes pauses
between runs, and is saved when a run ends or stops. It does not estimate model or stage
durations from token counts. The charts stack vertically on smaller screens.

Pausing is a normal workflow action: the project and task show a paused status without
an error notice or cancellation diagnostic in the event log. Saved work remains resumable.
Budget limits, request deadlines, and model failures still display their diagnostic messages.

Manual proofreading lists chapters in rows with search, translation-status filtering,
and saved paragraph counts. Opening a chapter shows its source and saved translation
without adding paragraph numbers. Polishing history and review notes expand on demand.
Newly persisted translation batches
refresh automatically. Editing remains disabled during a running project task.

Whole-book review opens the latest run. Search its issue list and expand individual
evidence, suggested changes, publication records, or full technical details. Review
history is collapsed; selecting an older run shows a link back to the latest result.
Autofix is controlled only in project settings and is enabled by default in the standard
workflow. Starting review uses the saved setting, including an explicit opt-out, without
a separate checkbox or temporary override on the review page.

Export keeps format, monolingual/bilingual edition, generation, and downloads visible.
Layout options show their current values in a collapsed summary. Folding controls does
not reset them; a failed export request reopens the advanced options. Subtitles retain
their independent editor and export workflow.

Global Settings owns provider connections, model registration and parameters, default
model tiers/routes, the default creation template, and standard workflow defaults. The
interface language remains a browser preference; model/default settings persist on the
server. Quick draft disables understanding, polishing, review and autofix. New projects
copy defaults at creation; queued/running tasks keep complete execution snapshots.

Project settings select registered models for tiers or individual operations and expose
common workflow switches. They link to global Settings to register new models. Connection
and model-definition editors are absent from projects, including advanced YAML. Complex
fallback routes and budgets remain in project YAML; segmentation/performance and saved
model routes remain collapsed. Validation errors retain drafts and reopen advanced
configuration. The autofix switch stays visible, and the PDF parser appears only for PDF
projects. Clearing an operation override restores the operation's default tier.

Punctuation normalization is an advanced export option for that export request; it does
not overwrite project defaults. Persistent export defaults remain in project YAML.
Export generation waits for those defaults to load successfully.

The creation API accepts `multipart/form-data`: `project` contains JSON matching
`ProjectCreate` (including optional `prepare` and `pdf_backend`), and `file` is the
required original. Its response contains the created project's identity and current
status, including a recoverable queue error if applicable.
