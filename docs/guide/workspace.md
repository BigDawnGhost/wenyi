# Web and Desktop workspace

Web and Desktop share the workflow below, but keep separate projects and configuration. Use the workspace on a computer; mobile/tablet browsers are not supported. For translation-stage details, see [The translation pipeline](pipeline.md).

## Create a project

1. Choose **Create project** at the top right of **My projects**, give the book a name, and select source and target languages. The source language can be detected automatically. Selecting a file suggests its name when the project name is empty.
2. Drag in one nonempty EPUB, DOCX, FB2, TXT, Markdown, HTML, or PDF, or choose **Browse files**. For PDF, select the parser before upload: MinerU is the default; BabelDOC is the optional layout-preserving route. See [format support](../reference/formats.md) and [Desktop's BabelDOC plugin](desktop.md#optional-babeldoc-plugin).
3. Choose **Translation style**: **Literary** is the default; **Academic** emphasizes arguments, conceptual distinctions, qualifications, and citations. The choice applies to preparation, translation, terminology, polishing, and Review.
4. Choose **Three drafts + synthesis**, **Incremental glossary notes**, and **Prepare before translating** independently. All three switches are off by default.
5. Click **Create project**. Desktop also lets you choose a [project folder](desktop.md#data-locations-and-backups).

**Incremental glossary notes** saves source-backed facts after each translated batch. It works with either translation mode, adds model usage, and is fixed at project creation. Leaving it off keeps ordinary glossary extraction.

With **Three drafts + synthesis** off, the project uses standard translation. With it on, Wenyi produces **three independent drafts from the same source and context, then one synthesis of all three**. It does not select a winning draft. This enables the required polishing step and costs more model usage; there is no separate draft-concurrency setting. Translation method and style are saved with the project and cannot be changed later.

**Prepare before translating** requests preparation immediately after creation. Leaving it off postpones AI preparation until you start the workflow; it does not disable the workflow's preparation steps. Preparation builds style guidance and, when enabled, whole-book understanding. **It does not extract or prefill glossary entries or translated character names.** Terms come from completed source/translation pairs during translation or manual entry/import. Repeated preparation preserves existing terms.

Creation opens the project overview while parsing or preparation continues. Once parsing finishes, **Read contents & source** is available even if AI preparation has not finished or a model call failed. Parsing needs no translation-model credentials, although PDF conversion still needs the chosen parser service. Titles and translations remain read-only until initialization completes.

Closing the creation dialog after upload has started does not cancel the request; the project may still be created in the background. After a Desktop native upload failure, reselect the source before retrying. Once initialized, a different source or target language requires a new project.

## Choose models and workflow

Register providers and models in **Settings → Models & connections**. In **Stage models**, select a provider and model directly for each operation. **Project settings** offers the same selections for one project without changing the shared registry.

1. Choose **Add provider** or **Edit** to open a provider's editor. The dialog shows its connection settings and registered models. Desktop accepts keys through its [credential controls](desktop.md#api-keys); Web only accepts the name of a server environment variable.
2. Fetch names with the refresh control beside **Model name**, or type a name manually. There is no separate model ID: each model name is registered once per connection. Fetching contacts the provider's model-list service but **uses no model tokens**. It uses the edited connection, including an unsaved base URL; Desktop can also use the key currently entered. On Web, save a newly chosen environment-variable name before fetching.
3. Choose **Save provider** in the dialog to save that connection and its models immediately. No second save on the Settings page is needed, and unrelated drafts are not submitted. Choose **Cancel** to discard the dialog's edits. Reopen the dialog and choose **Test connection** to see the status, latency, and any failures for the saved models on that connection. The test sends short live requests and **may incur provider charges**. Configuration validation and credential-availability checks do not send model requests.
4. Check the operation models and workflow switches before starting. The **Thinking effort** column shows the configured value where the provider exposes one. Select a level to change it for that stage alone. Changes to one stage, including Review or Autofix, do not affect the others.

Use **Settings → Advanced → Advanced YAML configuration** to change [connection and model parameters](../reference/configuration.md#model-profiles), such as timeouts, retries, output limits, thinking options, and custom request fields. Fallbacks, token budgets, workflow concurrency, annotation alignment, and parser defaults are also configured in YAML. In **Project settings**, the editor is shown below Workflow and Export preferences. PDF parser selection is also available when creating a project.

Validate or save edited YAML before returning to ordinary controls; unvalidated YAML disables them. Ordinary edits preserve advanced values already in the draft. **Validate configuration** checks the current draft without sending a model request.

Built-in workflow defaults enable whole-book understanding, polishing, Review, and Review Autofix. In **Settings → Workflow** or **Project settings → Workflow**, you can also configure **Glossary mode** (`Incremental` or `Compiled Book Bible`), **Polishing mode** (`Full rewrite` or `Sparse semantic edit`), **Chapter concurrency** (available with `Compiled Book Bible`), **Book-wide consistency sweep**, and **Deterministic formatting hygiene**. Existing project overrides still apply. In particular, Review is **not read-only by default**; see [Whole-book Review](#whole-book-review) before starting a run.

New and resumed tasks use saved settings; active tasks keep their captured configuration. Pause a project before changing its configuration. In global Settings, switching categories preserves drafts, but leaving Settings or reloading discards unsaved changes. Restoring defaults creates a draft and takes effect only after saving. Configuration reference: [Models and pipeline options](../reference/configuration.md).

## Translate, pause, and resume

Start translation from the project overview. **Current workflow** shows the actual task type, status, and last reported stage. Saved paragraph counts, chapters, usage, and running time update as work progresses. Model usage appears after the provider reports it; unknown counters show `—`, not zero, and token counts are not an invoice.

Use **Pause** for the current queued/running task and wait until it is paused before editing. **Pausing is a safe-boundary stop, not an instant undo or a guarantee that an in-flight provider request is unbilled.** Wenyi stops work at supported cancellation/checkpoint boundaries and retains committed progress. If the task has already completed or a newer run has replaced it, refresh instead of repeatedly pausing the old run.

Use **Resume** to continue the actual interrupted task type, including preparation or Review, rather than starting the book over. Completed checkpoints are reused; work not committed before interruption may need to run again. A failed task shows its reason in the project's overview: correct the problem, then resume. Failed exports are retried by creating a new export.

When the model provider has a temporary outage (timeouts, dropped connections, HTTP 408/409/429 or 5xx), the task pauses instead of failing and resumes by itself after 1, 2, 4 and then 8 minutes. **Current workflow** in the project overview shows the next retry time and a **Resume now** action. If the provider answers any request between outages, the retry sequence starts again from 1 minute. If about 15 minutes of retries get no answer, the task stops with an error so you can check the provider. Errors that waiting cannot fix, such as an invalid key, a missing model or an exhausted balance (HTTP 400, 401, 402, 403 or 404), stop the task straight away.

While work is queued, running, or stopping, project configuration, paragraphs, titles, glossary, and analysis are read-only. Reading and copying remain available. Conflicting starts or edits are rejected; saved-snapshot exports can run alongside translation. If another client changed an idle project, reopen or refresh it to load the latest state without treating an old page as authoritative.

Navigating away does not pause a run. Closing a Web tab does not stop server work; hiding Desktop does not stop its local engine. Desktop can run multiple projects at once without a fixed task-count ceiling, but they share machine/provider capacity and can incur charges together. Per-run model budgets are not a shared account-wide spending cap. Pause/resume affects only the selected project.

## Proofread translations

Open **Manual proofreading** to compare source and translation. Parsed but uninitialized projects show source only; after preparation, unfinished paragraphs are distinguished from saved translations. An intentionally saved empty translation still counts as complete.

Chapters scroll continuously, without page controls. Only nearby paragraphs are loaded and displayed; paragraph links jump directly to their location. While a task runs, saved translations refresh in place without reloading the chapter. Browser find and select-all cover mounted content only, not the whole chapter.

Pause active work, open a paragraph, edit it, and **save**. Automatic refresh does not turn an unsaved draft into a saved revision. Save important edits before reloading, closing the app, or leaving an editor whose draft you need.

- **Change history** compares polishing and manual revisions, with a full-text view when useful. Loading an old version only loads an editing draft; it does not replace the translation until you save. Older “initial translation → current version” comparisons can include later manual or automatic changes.
- **Precision drafts** shows archived T1/T2/T3 and their synthesis for three-draft projects. T1 is a before-polish comparison, not a selected winner. The archived synthesis and current formal translation are separate because later edits or Review may have changed the latter. Inspection is read-only and does not regenerate missing drafts or call a model.
- **Contents & titles** edits translated chapter titles. Saving also updates linked EPUB table-of-contents entries for future exports; existing exports are unchanged. Body headings are edited separately in proofreading. If another editor changed a title, load the latest value before saving your retained draft.

## Manage the glossary

Use **Glossary** to add or edit source terms, translations, aliases, notes, types, and gender. Manage character names through the **Person** filter rather than a separate character list. Japanese-source projects also expose readings; translating *into* Japanese does not by itself enable that field. Under **Settings → Workflow** (global defaults) or **Project settings → Workflow**, choose **Batch glossary** (`Full glossary` or `Relevant to each batch`) to control whether each translation and polish batch sends the whole glossary or only matching entries; long books benefit from `Relevant to each batch` while whole-book Review still receives the full glossary.

Search covers the whole glossary, including aliases, readings, and notes. Type filters apply to the results. Selecting all affects only the current page; changing pages or filters clears the selection. During a queued/running task, **Term details** remains available for reading and copying, but adding, importing, editing, deleting, and resolving conflicts are disabled.

Import **CSV or JSON**. The file and the parsed glossary data must both fit within **5 MiB**; if either exceeds the limit, no terms are imported. CSV needs `source` and `target` headers. Import checks existing source terms across the complete glossary, not only the visible page or filter. Export CSV/JSON to reuse the glossary; type labels follow the interface language, while stored/exported built-in type IDs remain English.

### Anki export

**Glossary → Export → Anki deck (.apkg)** exports the complete glossary, regardless of filters or selected rows. It creates a `Wenyi` parent deck, a project-specific book deck, and subdecks for all term types. Each card has the source on the front and the book's translation, available reading, notes, and aliases on the back.

These are book-specific translations, not dictionary definitions. Export does not call models, generate readings/examples/audio, modify terms, or include unresolved alternatives. `.apkg` is export-only.

Re-exporting unchanged source terms from the same project keeps note identities stable. Anki can update their contents without duplicating notes or resetting review progress, subject to its import settings and modification-time checks. A renamed source term or another project creates a new note. Deleting a Wenyi term does not delete its Anki card; import is not synchronization.

## Style and synopsis

In **Style & synopsis**, edit genre, tone, narration, pacing, register, dialogue style, rhetoric, and the style guide. **Style overview → Save** saves those style fields together. The whole-book synopsis has its own save action; chapter summaries are edited separately.

Saving one section does not discard drafts in the others. Style/synopsis drafts survive navigation within the same app session and are isolated by project, but **exist only in memory until saved**. Reloading or closing the app discards them. Editors are read-only while a task or analysis save is active.

Style analysis provides descriptive character guidance, not a second name-translation glossary. Model requests take name mappings from the glossary; use that page to maintain them.

## Whole-book Review

Complete all chapter translations before starting **Whole-book Review**. It can run as an enabled workflow step or be started separately from the Review page. Review results distinguish reported issues and recommendations from fixes actually published to saved chapters; historical runs remain available separately.

**`pipeline.review_autofix` defaults to `true`. Review can change the formal translation.** For a new suggestions-only run, explicitly turn off **Project settings → Apply autofixes to the saved translation after review**, save the configuration, and then start Review. In Chinese this option is **审校后自动修复并写回正式译文**.

Turning Autofix off prevents new publication; it does **not** undo previously published fixes or cancel recovery of write-back that has already started. If a previous publication was interrupted while applying fixes, resuming may finish that write-back before checking the current Autofix setting.

Review may reuse a matching completed result or resume interrupted work. Inspect the run's findings and published fixes rather than assuming every recommendation changed the book. Save manual drafts before starting, and re-export after accepting any changed translations.

## Export and delete

### Export books

Choose a format and, where supported, monolingual or bilingual output. Export reads a consistent snapshot of **saved** project data, so it can run beside translation; unsaved drafts and changes made after that snapshot are not included.

You can export as soon as source parsing finishes. Before translation preparation, the book contains the parsed original; exporting does not start translation or require a translation model. Once translation state exists, exports use its saved translations and fall back to the source for untranslated passages.

Configure **Export preferences** in **Settings → General** for global defaults, or in **Project settings** for a project override, then **Save configuration**. These preferences control bilingual order, the optional EPUB “About this translation” page, and preservation of source formatting in bilingual output (not used for PDF). The export page uses the project's effective saved preferences; it does not provide separate overrides for an individual export. Existing project overrides take precedence over global defaults.

EPUB, DOCX, HTML, TXT, and Markdown are available for ordinary book projects. **PDF export is only available for projects saved with the BabelDOC parser** and preserves their PDF layout through the bridge. Changing the current default parser does not turn an existing MinerU project into a BabelDOC project. See [format support](../reference/formats.md).

- **Web:** download the completed export through the browser.
- **Desktop:** choose the destination in the system save dialog before export starts. Canceling creates no export task and writes no output. Completed history entries also offer **Save as…**. Existing destinations require overwrite confirmation; failed transfers preserve the previous file. Avoid the source file and internal project folders, and do not let another application change the destination during saving.
- HTML saves as one `.html` file with readable local image/media resources embedded, not a ZIP with an assets folder. Remote URLs are not downloaded.

**Only the latest five completed book exports are retained per project. Save copies elsewhere for long-term use.** Re-export after edits; old files do not update automatically. Glossary exports have their own CSV/JSON/Anki actions and do not consume book export history.

### Delete a project

**Deleting a project is permanent.** It removes the uploaded original, translations, history, caches, and project export files. Save any books or glossary data you want to keep before confirming. Separately saved downloads and other projects are not deleted.

Active tasks block deletion. Once deletion has started, closing its dialog does not recall the request. A cleanup error can leave some files already removed; keep the project record and retry the reported failure rather than assuming nothing changed. For an unavailable Desktop project folder, removing its catalog entry cannot erase the disconnected copy.

## Interface language

Open **Settings → General → Interface language** and choose **English** or **简体中文**. English is the default, independent of the browser language. The choice takes effect immediately, survives reloads, and is separate from project configuration.

It changes interface labels and number/date formatting, not source/target languages, book text, glossary entries, model configuration, or model-generated analysis. Changing interface language is not a way to retranslate a book.
