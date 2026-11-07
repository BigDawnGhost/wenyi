# Wenyi Desktop

[简体中文](zh/desktop.md) · [Web deployment and development](web.md)

Desktop is a **local translation application**, built with Tauri, React, and the existing Python translation engine. It starts its own private backend and SQLite workspace. It does not require a Web deployment, PostgreSQL, Redis, or Docker. Packaged releases include the Python runtime; users do not need to install Python.

Translation still needs access to the configured model provider. Optional MinerU and BabelDOC services retain their existing requirements; “local application” does not make external model or document services offline.

## Quick start

[![Download Desktop](https://img.shields.io/badge/Desktop-download-D4B56A?style=flat-square&labelColor=00263D)](https://github.com/BigDawnGhost/wenyi/releases)

### 1. Download Desktop

Open [GitHub Releases](https://github.com/BigDawnGhost/wenyi/releases) and choose a `wenyi-desktop-<version>-<platform>-<arch>` asset matching your system:

| Platform | Package |
|---|---|
| Windows x64 | `.exe` installer |
| Linux x64 | `.AppImage`, `.deb`, or `.rpm` |
| macOS Apple Silicon | `.dmg` |
| macOS Intel x64 | `.dmg` |

Desktop assets are distributed directly, without an outer ZIP. For an AppImage, allow execution in the file's permissions before opening it. Packaged releases include the translation engine: you do not need to install Python or deploy a server.

On macOS, choose `macos-arm64.dmg` for Apple Silicon or `macos-x64.dmg` for an
Intel Mac. Each package includes a native application and Python engine for that
architecture; there is no universal macOS package.

Check the release notes for platform requirements and signing status. To build from source instead, follow [Run from source](#run-from-source).

### 2. Connect a model

Open **Settings → Models & connections**, choose a provider, enter the API key in the connection's **API key** field, and set any custom base URL. In each model, click the refresh button next to **Model name** to fetch the provider's model list, then pick a name from the dropdown or type one. Click **Save configuration**; the key is written together with the configuration, with no separate save step.

Click **Test connection** below the models to send one short request to each saved model. The table shows whether each one connected, its latency and the failure reason, such as an invalid API key, unknown model name or unreachable base URL. Each test uses a few tokens; save edits first, because only saved settings are tested.

Desktop uses the system credential store when available. If it is unavailable, the interface explains that the key is kept only for the current session and must be entered again after restart. A local workspace does not mean offline model processing: text is sent to the provider you configure, unless you use a local model service.

### 3. Create a translation project

Use **Create project** in the shared sidebar to open the centered dialog over the project list. The list remains visible but inactive behind it; `/projects/new` opens the same dialog directly. Focus starts in **Project name** and stays inside the dialog with Tab. Escape closes an open dropdown first. **Cancel**, **Close**, the backdrop, or Escape returns to the previous page (the project list for a direct link) without creating a project and restores focus to the sidebar entry. Browser Back also leaves the dialog. Closing releases any selected native source resource; reopening starts a clean form. In short windows the form scrolls while the close header stays visible.

The compact form places languages and translation style on one row, followed by the source file and an inline project-folder picker. The **Three drafts + synthesis** and **Prepare before translating** switches sit side by side and default to off. Narrow windows wrap the controls; footer actions stay visible.

**Project folder** names where the new project's directory is created, and defaults to `projects/` inside the [Desktop data root](#independent-data), not a folder beside the installed application. Choose **Choose folder** to put this project somewhere else; **Use default** returns to the default location. A folder that cannot hold project data is refused with the reason shown beside the control: the application's own program folder, a parent of it, Wenyi's cache and settings folders, a drive or user folder, and system folders. The choice applies to this project only, and existing projects keep the folder they already have.

The uploaded original and project state stay together in that project directory, including when the chosen folder is on another local volume. Saving model settings or restarting Desktop does not forget existing project locations. If the recorded library is unavailable, reconnect it: Desktop does not automatically substitute a same-ID copy in another library.

Drag in a supported book or choose it with **Browse files**, and select the source and target languages. Source-language detection can be automatic. The **Three drafts + synthesis** switch is off by default for standard translation; turn it on to create three drafts and synthesize them at higher model cost. During upload, source replacement and the switch are locked and **Cancel** becomes **Close**; dismissing stays available but is not a task cancellation action. Failed uploads retain the form and source selection; a failed native upload requires re-dropping the file to renew its single-use grant before retrying. Successful creation enters the project overview while parsing or optional preparation continues in the background. Legacy `/projects/new?project=<id>` links retain read-only source preview/resume controls and the saved switch state.

**Translation style** defaults to **Literary**. Choose **Academic** to emphasize
arguments, conceptual distinctions, qualifications and citations. Either style works
with either translation method and applies to preparation, translation, terminology,
polishing and review. The choice is saved with the project and queued tasks, including
native file drops, and survives restart and resume. Project settings show it read-only;
changing it requires a new project. Older projects remain literary.

If **Project name** is empty or contains only whitespace, selecting a valid file by either method fills it with the filename minus its final extension (for example, `book.v2.epub` becomes `book.v2`). If that would leave an empty name, the full filename is used. Existing names are never overwritten when choosing another file, and the suggested name remains editable.

Browser Back can still leave during an upload; it does not cancel the request. The source resource remains available until the request settles, then is released. The project may be created in the background, and completion will not redirect you away from the list.

The source is already uploaded when the project overview opens, so it has no upload entry, including before AI initialization completes.

Once parsing finishes, **Read contents & source** opens the chapter list and original text without waiting for AI preparation. The parsed source remains readable during preparation or after a model failure; title and translation editing require completed initialization. Parsing itself needs no translation-model credentials, although PDF conversion still uses the selected parser service.

Start translation from the project page. Wenyi parses the source, prepares whole-book context, and translates in batches. Progress, usage, and completed chapters are visible in the application; polishing and whole-book review are configurable. A failed task shows the reason the run ended beside the last readable stage. Saved project failures stay inside that project's translation overview with the resume action, never in a global banner above other pages; the project list retains the failed status badge. A request that never receives a response fails after its deadline instead of waiting indefinitely, and a progress socket that stays silent for 60 seconds reconnects while the local engine sends a heartbeat every 30 seconds during idle runs.

**Current workflow** directly shows the task type, status, and reported stage in a flat section. There is no nested step checklist or project-report panel. Completed tasks show only their completion status; paused or failed tasks retain the last reported stage when available. Usage, chapter status, and **Read contents & source** remain available.

The local engine no longer generates project summary reports or exposes the `/report`
endpoint. Historical report data is left untouched, and saved legacy report steps do
not block project resume. Review/Autofix results, usage, timing, book exports, and
application diagnostic exports are unaffected.

Multiple Desktop projects can run at the same time, without a fixed workflow-count limit or a concurrency setting. Start each from its existing project page; navigating to another project or hiding the window does not pause it. Background management is internal: there is no separate task center or batch-control interface. Parsing, preparation, translation, chapter translation and review workflows can overlap across projects, but conflicting workflows within one project remain mutually exclusive.

Pause and resume affect only the selected project and retain its saved checkpoints. A failed project does not cancel other projects. Quitting the engine waits for active workflows to stop safely; installing updates remains blocked until all tasks and cleanup have finished. Exports retain their independent two-worker queue.

No task-count ceiling does not mean unlimited machine or provider capacity. More simultaneous projects can consume more memory and incur model charges together, and provider throttling can slow them down. Configured model budgets/limits remain per invocation, not a shared cross-project account quota. This Desktop behavior does not change Web worker concurrency or CLI execution.

The shared usage display separates cache reads, writes, ordinary input and unknown
classification. **Cache read / all input** includes unknown input in its denominator;
historical misses are not reclassified as ordinary input. Available write TTL subtotals
are shown separately. The cache-write input entry is hidden when its count is zero;
nonzero counts and unknown values (`—`) remain visible.
See [usage accounting](cli.md#usage-and-event-logs) for details.

### 4. Proofread and save

Compare the translation with the source, edit paragraphs, inspect revisions, and review reported issues. Review can publish fixes to the translation; disable automatic fixes when you want a read-only review. Desktop and Web share these workspace pages; see the [interface preview](../README.md#interface-preview) for a screenshot.

Completed and paused projects do not periodically poll their pages, including chapters, glossary and revision history. When a task stops, the interface reads its final saved data; starting/resuming a task restarts active polling. Saving edits and restoring a hidden window still reconcile data without discarding drafts. Large chapters retain native find, cross-paragraph selection and deep links, with one shared paragraph menu and chapter review notes mounted only on first expansion.

Right-clicking ordinary page areas does not open the browser's Back/Forward/Reload menu.
Text fields retain their native editing menu, including copy and paste; proofreading
paragraphs still open their paragraph actions menu. Web browser menus are unchanged.

Choose an export format and, where supported, a bilingual edition. Desktop opens the system save dialog before starting the export; canceling creates no export task. HTML input defaults to a single `.html` file that can be opened directly, without extracting a ZIP.

On the **Glossary** page, choose **Export → CSV file**, **JSON file**, or
**Anki deck (.apkg)** to save the complete glossary through the system save dialog.
Anki export includes every type in separate subdecks, independent of table filters or
selection; see [glossary cards and re-import behavior](web.md#workflow).
Saving shows the destination path;
canceling writes nothing. A failed save reports an error and preserves any existing
destination file. Glossary exports do not create book export tasks.

PDF is offered only for saved BabelDOC projects, using the external bridge to preserve
the original layout. MinerU and other book projects export EPUB, DOCX, HTML, TXT or
Markdown instead. No local PDF rendering engine is installed or selected.

### Continue later

Completed batches are saved in the local workspace. Reopen Desktop, open the same project, and resume from its checkpoints. Save any in-progress proofreading edits before closing. For workspace locations and backup instructions, see [Independent data](#independent-data).

Progress snapshots are saved serially, and flushing waits for in-flight writes before
returning. These display snapshots do not replace committed translation checkpoints.

Pause is accepted only for the current queued or running task. If completion or a new
run wins the race, the request is rejected instead of changing the completed state to
“Pausing”; refresh the project to see the latest state.

## Interface language

The interface defaults to English. Open global **Settings → General → Interface language**
to choose **English** or **简体中文**. The change applies immediately to interface
labels, not the book's source/target languages, content, or model-generated analysis.

## Independent data

Desktop does not adopt or migrate existing Web projects. It also does not read or modify the CLI's `config.yaml`, `state/`, or `output/`. CLI behavior remains unchanged.

Desktop has **one data root**, separate from its program directory:

| Platform | Location |
| --- | --- |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/wenyi/Desktop` |
| macOS | `~/Library/Application Support/Wenyi/Desktop` |
| Windows | `%LOCALAPPDATA%\Wenyi\Desktop` |

Only an absolute `XDG_DATA_HOME` is used; unset, blank or relative values fall
back to `~/.local/share`. Changing it selects a different data root; it
does not relocate existing data. macOS and Windows ignore `XDG_DATA_HOME`.

```text
<data-root>/
  catalog.sqlite3   # project list, jobs, settings and opaque credential references
  projects/         # default project files, originals, state, caches and exports
  logs/             # diagnostic logs
  locks/            # native/engine locks, writer leases and activation endpoint
  runtime/          # disposable plugins and Windows/Linux WebView data
  backups/          # verified pre-update snapshots
```

Settings stay transactionally in SQLite; there is no second `settings.yaml`.
API keys remain in the OS credential vault or session memory, never this database.
`--data-dir <path>` selects this **entire** data root, including settings, plugin
runtime and logs. Two roots do not share a hidden catalog. Individually chosen
external project libraries remain supported and retain their recorded locations.
Windows/Linux WebView caches and presentation preferences also use
`runtime/webview/`; macOS WKWebView storage is managed by the operating system
and cannot be relocated with Tauri's data-directory option. OS credential vaults
likewise remain OS-managed, outside the data root.

**Breaking change:** previous Desktop catalogs, settings, credentials and projects
are not migrated, merged, discovered or repaired automatically. The old
`Wenyi Desktop` workspace, `%USERPROFILE%\Wenyi`, `~/.wenyi/desktop` and
installation-adjacent `Wenyi Desktop Data` directories are left untouched.
The new default starts with an empty library. Renaming an old catalog to
`catalog.sqlite3` does not upgrade it: unidentified or unsupported catalogs are
refused before store initialization. Keep old data if you need to open it with an
old release; this release provides no old-format import or migration command.

**Use local disks** for the data root and project libraries, not network shares,
mapped drives or cloud-synced folders. SQLite/WAL locking, placeholders and
non-atomic synchronization can corrupt or incompletely copy data. Desktop does not
detect those locations. Selecting another root never moves or deletes existing data.

### Windows installation and uninstallation

The default program directory is `%LOCALAPPDATA%\Programs\Wenyi Desktop`.
For example, for user `Alice`:

```text
C:\Users\Alice\AppData\Local\Programs\Wenyi Desktop\wenyi-desktop.exe
C:\Users\Alice\AppData\Local\Wenyi\Desktop\catalog.sqlite3
C:\Users\Alice\AppData\Local\Wenyi\Desktop\projects\<project-id>\
C:\Users\Alice\AppData\Local\Wenyi\Desktop\logs\
```

Changing the program directory does not change the data root or create a sibling
project library. English and Simplified Chinese setup UI are available.
Setup is current-user only and rejects an elevated launch: run normally, not
**Run as administrator**. A shared setup/uninstall mutex prevents concurrent
replacement. Before writing payloads, setup checks both the native executable and
`sidecar\wenyi-engine\wenyi-engine.exe`. Quit through the tray and wait for tasks
to stop if prompted; setup does not force-stop translations. Silent/passive setup
fails instead of bypassing these checks.

Setup refuses volume/share roots, the user profile itself, Windows/Program Files,
overlap with the data root or legacy Wenyi data folders, linked ancestors,
unwritable folders and occupied folders not owned by the current installation.
The application also compares its **actual** executable directory with the
selected data root on every launch, including unpacked and `--data-dir` launches.

Replacing a registered old program removes only that program's recorded files
through its uninstaller, refreshes shortcuts and registers the new location.
It does not recursively remove the old mixed program/data directory, import old
data or retain that directory as the new default. Installing an older version over
a newer one is refused in interactive, silent and passive modes.

A current-format in-place upgrade preserves unrelated files in the registered
program directory; those files do not make the upgrade fail after uninstalling
the previous program. A different or unregistered target must still be empty,
and path, link and writability checks still apply.

Uninstall keeps data by default. Two independent, initially unchecked options are:

1. Delete the catalog, settings, logs, locks and runtime, **not project files**.
2. Permanently delete default project files and upgrade backups.

Both apply only to `%LOCALAPPDATA%\Wenyi\Desktop`. Custom `--data-dir` roots,
external project libraries and all legacy directories are always kept. Directory
links are removed without following their targets. Deleting the catalog loses the
project list even if project files are kept; they are not automatically rediscovered.
Silent uninstall and upgrade replacement never select either deletion option.

Before release, validate on a real Windows standard-user account using temporary
synthetic projects: fresh install, old-program replacement with old files retained,
changing the install folder, English/Chinese UI, simultaneous setup/uninstall,
tray-hidden and orphan-engine refusal, elevated launch, downgrade in every mode,
all four uninstall-option combinations, and nested junction targets remaining
untouched. A cross-compiled installer or browser fixture is not that platform test.

### Backups and current-format imports

An in-app update freezes writes, resolves every registered project's exact
directory, stops the engine and backs up those directories and `catalog.sqlite3`
(including any WAL/SHM files) to `<data-root>/backups/<version>-<timestamp>/`.
It does not copy the whole data root or earlier backups into itself. The manifest,
written last, records original paths, file sizes and SHA-256 hashes. Credential
references are included; OS-vault/session secrets are not.

An unavailable project directory, unsafe link, insufficient space or failed
verification cancels the update before setup runs. Close other copies of the same
program before replacing it, even if they use different data roots. Failed
snapshots reopen the existing application; failed acknowledgement of preparation
cancellation requires quitting and reopening Wenyi rather than resuming writes.
Manual setup does not generate a snapshot or require a historical backup marker:
back up the current data yourself before replacing the program.

For manual backups, stop Desktop first, copy the entire data root and every
registered external project directory, keeping SQLite WAL files beside their
databases. Restore a consistent set, not only project files or only the catalog.
Project databases retain their independent schema-version guard: newer, unsupported
state stays listed as unavailable instead of being silently dropped or modified.

Explicit folder import accepts only current-format projects (`project.json`
schema version 2), with readable state and an original matching its saved SHA-256.
Old/missing markers and ambiguous sources are refused before copying. A successful
import moves the selected project into the current default library after verifying
the copied files; it is not automatic recovery of a lost catalog or old-version data.

Job-status polling reads committed catalog snapshots without reserving a write
lock, so an active catalog writer does not block status lookups.

Deleting from the project list opens an in-app confirmation with Cancel focused.
Prepared projects without active tasks can also be deleted. While a request is
pending, duplicate submissions are disabled and the dismiss action is relabelled
**Close**, because a delete in progress cannot be recalled; failures keep the
dialog open for retry.

**Deleting a Desktop project is permanent.** The confirmation covers its uploaded
originals, translations, history, caches, and exports inside its project directory,
whether in the default or a chosen library folder. Files saved outside that project
directory are not removed. Active tasks or export downloads block deletion.
If file cleanup fails, Desktop reports an error and retains the catalog entry so
you can close files using the project and retry; some files may already be removed.
Historical leftover directories are not bulk-cleaned.

Deletion runs in three steps so that an interruption at any point is recoverable:
the project is first marked as being deleted, then its files are removed, then its
catalog entry is deleted. The mark is what makes the difference. Before it, a crash
leaves a whole project; after it, the project is no longer listed or openable, and
a retry finishes only what is left. Explicit import also refuses a directory
carrying this mark. Startup never scans folders to reconstruct catalog rows.

The native launcher and backend normalize Windows `\\?\` path spellings to the
same data-root identity. One backend owns a data root at a time.

## Run from source

Install Python 3.10+, `uv`, Node 22, pnpm 9, Rust stable, and the [Tauri platform prerequisites](https://v2.tauri.app/start/prerequisites/). These are development/build requirements, not additional Python requirements for packaged releases.

On Debian/Ubuntu, also install `libdbus-1-dev` for the native tray availability
checks (`sudo apt-get install libdbus-1-dev`); other Linux distributions need the
corresponding D-Bus development package.

From the repository root:

```bash
uv sync --locked --package wenyi-desktop --group dev
pnpm install --frozen-lockfile
pnpm desktop
```

For an isolated preview:

```bash
pnpm desktop --data-dir /path/to/desktop-test-workspace
```

The launcher builds the UI and starts the native application. Debug builds use the repository's `.venv`; `WENYI_DESKTOP_PYTHON` can explicitly select another development interpreter. There is no remote `--url` mode or silent fallback to a Web server.

The launcher supports both JavaScript pnpm entry points and standalone native pnpm executables, including extensionless Linux binaries.

Build a native release on the target platform with:

```bash
pnpm desktop:build
```

The build freezes the Python engine as an onedir sidecar, then bundles it with the UI and native executable. Onedir avoids unpacking the entire runtime on every launch. The sidecar bundles `wenyi_locks` from `native/wenyi-locks`; build it first with `cargo build --release --manifest-path native/wenyi-locks/Cargo.toml`. A plain wheel install carries no native library and falls back to the Python lock backend, and `test_locks_contract.py` pins both backends to the same behaviour. Installers, signing, and platform-specific runtime dependencies must be verified for each release; a successful Linux build is not evidence of Windows/macOS validation.

macOS packages are built separately on Apple Silicon and Intel runners. When
building locally, use a native Python interpreter and Rust toolchain matching
the Mac's architecture; cross-compiling only the Rust host does not rebuild the
Python sidecar for another architecture.

Windows release builds open only the application window, not a console. The local
engine runs without a console window while retaining its private communication pipes.
Debug/source launches keep their development console for diagnostics.

Native bundles use PNG, ICO and ICNS artwork for Linux, Windows and macOS respectively.
To regenerate the native icons from the shared emblem, run
`pnpm exec tauri icon packages/ui/src/assets/wenyi-emblem.png --output /path/to/temporary-icons`
from the repository root, then copy only `icon.ico` and `icon.icns` into `apps/desktop/icons`.

### Versions and release downloads

Python metadata continues to use `hatch-vcs`; `scripts/release/release_version.py` uses
the same setuptools-scm Git source to derive the native application and asset
version. For example, `v1.2.3` becomes `1.2.3`, and `v1.2.3rc1` becomes Python
`1.2.3rc1` / native `1.2.3-rc.1`. Untagged commits and tracked local edits keep
an explicit development identity, such as `1.2.4-dev.2+gabc123`. Cargo's private
crate version is not the application version. The native executable's
`--version` option reports `Wenyi Desktop <version>` without starting the UI.

Distribution currently targets stable tags. Development/prerelease identities are
retained for build diagnostics, but Linux package-manager upgrade ordering is not
normalized: for example, Debian considers `1.2.4-rc.1` newer than `1.2.4`.
Do not treat these builds as a supported prerelease update channel.

Use `WENYI_BUILD_TAG=v1.2.3 pnpm desktop:build` on a clean checkout of that tag
(PowerShell: set `$env:WENYI_BUILD_TAG = "v1.2.3"` first). An explicit tag must
exist, point at HEAD, match the Python version, and have no tracked edits;
untracked notes do not change setuptools-scm's dirty status. Version overrides
are rejected. Supported release tags are numeric releases and `a`, `b`, or
`rc` prereleases; epochs, post releases, local/dev release tags, more than three
numeric components, and values beyond `255.255.65535` fail rather than silently
losing identity. macOS's numeric bundle version uses the release triple while
the application version retains prerelease/development information. Windows
uses NSIS `.exe`, not MSI, because MSI cannot faithfully represent these
prerelease/development identities.

The **CLI packages** and **Desktop packages** workflows accept an optional
`tag` through `workflow_dispatch`; leaving it empty builds the selected checkout.
Their run names show the component and tag (or development ref). A published
GitHub release builds its tag and uploads only that component's assets.
Manual runs only produce workflow artifacts; they do not publish a release.

- CLI downloads are `wenyi-cli-<version>-<platform>-<arch>.zip` on every platform.
  ZIP entries preserve Unix executable permissions; use an extractor that honors
  them (or run `chmod +x wenyi` after extraction).
- Desktop downloads are `wenyi-desktop-<version>-<platform>-<arch>.<extension>`:
  Linux AppImage (single-file application), `.deb` and `.rpm`; Windows NSIS
  installer `.exe`; macOS `.dmg`. They are uploaded directly, without an outer
  ZIP. After downloading an AppImage, grant execute permission (`chmod +x <file>.AppImage`).
  The bare Rust executable is not a distributable application.
- CI artifact downloads may be wrapped by GitHub's artifact service; the actual
  GitHub Release assets are the files described above. Checksums are separated
  as `wenyi-cli-SHA256SUMS.txt` and `wenyi-desktop-SHA256SUMS.txt`. Existing release
  assets are not overwritten; a duplicate upload fails.

The sidecar build rebuilds local Python packages instead of reusing their cached
wheels, and checks their metadata against the resolved Git version before freezing.
Installer collection requires the exact version, architecture and complete format
set for its platform; older outputs are left untouched and never relabeled.

These workflows do not perform OS code signing or notarization. Platform signing and end-user
installation checks remain release responsibilities.

The AppImage was launched on KDE Wayland with a temporary workspace and an invalid development-Python path. Its bundled engine started, authenticated loopback requests succeeded, and the previous close-to-exit lifecycle shut down and reaped that engine. Separate frozen-engine checks exercised offline synthetic TXT upload/parse/preview and existing/new-format event reads. The current tray lifecycle was also checked on KDE Wayland using a debug native build, production UI, source Python engine, and temporary empty workspace: minimize/restore and close-to-tray/restore kept the engine alive, and explicit tray exit reaped it. Packaged tray behavior, real file-manager drag/drop, OS save dialogs, Windows/macOS execution, and removable-media behavior still require platform acceptance testing.

Native single-instance behavior was additionally checked on KDE Wayland via
XWayland with a debug build, production UI, source Python engine, and temporary
empty workspace. A second launch during startup exited without initializing its
own GUI or engine, even with an invalid display and Python path. Reopening after
close-to-tray/minimize through a workspace path alias restored the same native
window and engine. Explicit tray exit reaped the engine, and a subsequent launch
succeeded with the discovery files retained. This does not replace packaged
Windows/macOS acceptance testing.

## Optional BabelDOC plugin

Open **Settings → BabelDOC plugin → Enable and download** to install the local
layout-preserving PDF engine. It is off by default: ordinary Desktop installations
do not include BabelDOC, download models, or require Python. Enabling explicitly
downloads a separate **AGPL-3.0-only** runtime and its model/font pack from
`BigDawnGhost/wenyi-babeldoc-bridge` releases. Supported targets are Windows x64,
Linux x64, and macOS Apple Silicon/Intel.

Wait for **Ready**, then choose **BabelDOC** as the PDF parser when creating a
project (or as the workflow default). Enabling the plugin does not change the
default parser from MinerU or convert existing projects. PDFs parsed with BabelDOC
can use the existing layout-preserving PDF export.

Runtime download and model/font download have separate progress bars with actual
downloaded size and percentage. Verification, unpacking, and service startup use
indeterminate progress. **Cancel download** / **Cancel setup** stops setup and
disables the plugin; **Retry** reuses
completed, checksum-verified archives, not partial downloads. On restart, an
installed enabled plugin starts automatically; interrupted installation requires
Retry. Disabling retains packages, models, and PDF sessions. Finish project jobs
before disabling; application update installation also waits for plugin setup.

For an installed plugin, choose **Check for plugin updates**, then **Update plugin**
and confirm the available version. Checks and upgrades are manual: restarting
Desktop, disabling/re-enabling the plugin, or publishing a new bridge release does
not upgrade an existing installation. Stable versions are compared numerically;
an equal or older release is never offered as an upgrade.

The current service remains usable while the new runtime and assets download
and unpack into a separate installation. Switching requires all project jobs,
native exports, and saves to be idle; otherwise the old version stays selected
and the verified downloads can be reused on retry. The selected version changes
only after the new service reports readiness and passes its health check.
**Cancel update** or an installation/startup failure retains the previous version
and restarts it if necessary. A disabled plugin stays disabled after updating.
If the previous runtime cannot restart, Settings reports that failure rather
than claiming it is ready. Old installations and project/session data are not
deleted. Frozen sessions are not automatically migrated: re-parse affected PDFs
when a bridge update changes extraction or session compatibility.

Plugin files live in `<data-root>/runtime/plugins/babeldoc/`:
`plugin.json` records enablement and
the selected installation, `downloads/` contains verified archives, `installed/`
contains complete runtime/cache pairs, and `sessions/` retains frozen PDF
snapshots. Earlier plugin directories are neither reused nor migrated; install
the plugin again in the new data root when needed. No CLI
or Web state is used. The plugin has a separate process, random loopback port,
memory-only bearer token, and an owner pipe so it exits when Desktop closes.
Ports and tokens are not saved in projects; the default
`http://127.0.0.1:8765` bridge address resolves to the managed instance only within
Desktop workers. Explicit custom bridge URLs retain their existing HTTP behavior.

The plugin installer accepts only protocol-1 release manifests from the fixed
bridge repository, downloads version-bound HTTPS asset URLs, checks sizes and
SHA-256 hashes, rejects archive links/path traversal, and activates only complete
installations. This trusts the official GitHub release over HTTPS; it is **not**
the signed whole-application updater described below. There is no arbitrary
plugin URL or marketplace. Installed plugin versions stay pinned until the user
explicitly confirms a manual upgrade.

The bridge release must first publish `desktop-plugin.json`, the platform runtime
ZIPs, and the model/font ZIP using its **Desktop plugin** workflow. Until these
assets exist, enabling reports a retryable installation error; it does not
pretend the plugin is installed. No release is published by a local Desktop build.

## Application updates

The update area is under **Settings → About** and shows the current
Git-derived application version. Check for a newer public release, then use the
separate **View release notes** button to read its notes in a scrollable dialog.
English interfaces show only English notes; Chinese interfaces show only Chinese
notes. Missing or unmarked language sections show a localized empty message, never
the other language. Markdown headings, lists, emphasis, code, quotes, and GFM tables
and task lists are rendered; raw HTML is ignored, and links/images show their text
without external navigation or remote-image requests. There is no release-page button.
Older installations must be upgraded manually once to a release with this updater.
The Desktop workspace is retained; back it up before upgrading.

Downloads use the same theme-aware progress card as plugin/model downloads:
readable KB/MB/GB sizes and a percentage when the total is known, otherwise an
indeterminate bar. Checking and installing never reuse a download percentage.
The bar respects reduced-motion preferences and supports light/dark themes.

Maintainers should keep release notes under `## English` and `## 中文`
(`English`/`en` and `中文`/`简体中文`/`zh-CN` are recognized, case-insensitively).
A heading at the same or higher level or a horizontal divider ends each language
section. Use deeper headings within it; place shared bilingual footers after a
divider. Language-like text inside code blocks is not a section boundary.

Only stable, configured native release builds check automatically at startup,
without blocking launch. Checking never silently downloads, forces an installation,
or restarts the application. Installation requires explicit confirmation; finish
or cancel active tasks and save or discard unsaved editor/settings changes first.
The application checks these guards again before installing and restarting.

Signed Windows NSIS installations, macOS applications on both Apple Silicon and
Intel, and Linux **AppImage** builds support integrated installation. macOS updates
stay on the installed application's architecture. Linux `.deb`/`.rpm` installations
always use manual download/package installation, not AppImage replacement.
Development, local and unconfigured builds still support public-release checks
and manual downloads. Network/check failures remain visible and do not prevent using Desktop.
Updater signatures are mandatory for integrated installation and are **separate
from Windows/macOS OS code signing and notarization**.
Signatures must also bind the advertised application version (`requireSignedVersion`):
an older signed artifact cannot be relabeled as a newer release.

### Maintainer signing setup

No update service is required. The native updater reads
`https://github.com/<repository>/releases/latest/download/latest.json`, and the
repository is a build-time constant: `WENYI_UPDATE_REPO` (default
`BigDawnGhost/wenyi`) is read by `apps/desktop/build.rs`, which rejects a value
that is not exactly one `owner/repository`. The release workflow sets it to
`github.repository`, so a fork that reuses the workflow publishes installers
that update from itself. The same value decides which download links are
trusted, so the manifest a build fetches and the artifacts it accepts always
come from one repository.

Do not generate keys in this repository. From the repository root, generate a
Tauri signer key into a protected location **outside the checkout**:

```bash
pnpm exec tauri signer generate --write-keys /secure/path/outside-checkout/wenyi-updater.key
```

Use the interactive password prompt; never put a real password in command history.
Keep the private key and password in a secure backup. Never commit them, paste them
into issues/logs, or use them in PR workflows. Preserve the key across releases:
installed applications trust the embedded public key, so changing it requires a
planned migration/manual upgrade.

Configure these GitHub Actions repository entries:

| Entry | Kind | Value |
| --- | --- | --- |
| `WENYI_UPDATER_PUBLIC_KEY` | Variable | Full contents of the generated `.pub` file, not its path |
| `TAURI_SIGNING_PRIVATE_KEY` | Secret | Full contents of the private key file |
| `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` | Secret | Key password (empty for an intentionally passwordless key) |

For a local signed build, supply the same three environment variables from a
secure environment/secret manager (the public key is not secret), then run
`WENYI_BUILD_TAG=v1.2.3 pnpm desktop:build` in the matching clean tagged checkout.
On macOS, pass `--bundles dmg,app` to produce the additional updater archive.
The launcher enables Tauri v2 `bundle.createUpdaterArtifacts` only for configured
stable signed builds. Partial configuration, a public-key path/malformed public
key, or signing a development/prerelease version fails explicitly. Password must
be defined locally even when empty. Ordinary `cargo test`, `cargo run`, PR builds
and unsigned `pnpm desktop:build` do not need signing secrets.

The Desktop workflow supplies signing secrets only to stable release builds;
PRs, development/prerelease builds and manual workflow runs remain unsigned.
If all signing configuration is absent, stable releases still produce manual
installers and log that **no `latest.json` is published**. Partially configured
signing fails instead of publishing misleading update metadata.

Signed release assets add `.sig` beside Windows `.exe` and Linux `.AppImage`;
macOS adds `.app.tar.gz` plus `.sig` alongside the existing `.dmg` for each
architecture. There are no extra installer ZIP wrappers. `scripts/release/desktop_updates.py`
validates the complete four-platform set and produces a stable SemVer manifest
with `windows-x86_64`, `linux-x86_64`, `darwin-aarch64` and `darwin-x86_64` entries,
the actual renamed release URLs, and signature contents. All payloads and signatures must build and
upload successfully before `latest.json`, including the release notes, is uploaded
**last**. Release uploads never overwrite existing attachments; retries with existing names fail. Missing
or malformed signatures cannot publish a partial manifest. A release without a
manifest cannot be installed through the integrated updater; use manual download.
Collection and manifest generation require `WENYI_UPDATER_PUBLIC_KEY` and reject
signatures whose key ID differs from it. Tauri's key-mismatch warning is therefore
a publishing failure, not a release that clients cannot install. This release
check is not cryptographic verification; the native updater verifies the downloaded
artifact and signed version before installation.

## API keys

Each settings category uses the full content width, with its name in the page header instead of a duplicate title column. **Segmentation and performance**, **Models by operation**, **Advanced YAML configuration**, and credential **Environment variable** controls are shown directly, without collapsible groups. Project settings retains its titled sections and compact disclosures.

There is no divider above **Back**. The global settings action bar stays flush with the bottom of the content pane, including when scrolling to the end.

Use **Settings** in the shared sidebar footer to open its dedicated page. The sidebar becomes category navigation: General, Models & connections, Default models, Workflow, PDF parsing, BabelDOC plugin, Advanced, Diagnostics and feedback, and About. Only the selected category is shown. **Back** stays pinned to the lower-left corner of the sidebar, including when categories scroll or the sidebar is collapsed; it returns to the page that opened Settings, or to Projects when visiting a settings URL directly. About contains the version information and update controls, retaining the `/settings/updates` URL. Other categories keep reloadable URLs such as `/settings/providers`, `/settings/defaults`, and `/settings/advanced`; `/settings` opens General, and unknown categories return there. Switching categories or using browser history preserves configuration, ID, YAML, and API-key drafts in memory; leaving Settings or reloading discards unsaved drafts. Save, validate, and restore defaults apply to the shared configuration draft and remain in a bar pinned to the bottom. Reminder links open the relevant category.

The project list and projects share one persistent, collapsible sidebar: **Create project**, **Projects**, current/last project navigation, and **Settings** in the footer. Settings replaces these links with its categories and **Back** while retaining the collapse preference. There are no floating dashboard actions. Width and labels transition smoothly over 280 ms. Narrow computer windows keep the desktop icon rail and multi-column workspace, with horizontal content scrolling when needed; resizing preserves open pages and drafts. The shared frontend uses only the user agent to show **Please use a computer** on mobile/tablet browsers; window width, touch support, and platform hints do not block computers. Settings history navigation dismisses open dropdowns without resetting configuration drafts. Notifications stay fixed in the viewport; success lasts one second, errors/warnings ten seconds, and other messages five seconds. Glossary add/edit failures also remain inline in the dialog.

Open **Settings → Models & connections** and type the API key into the connection's **API key** field. It can be entered right away, including for a connection that has not been saved yet. The field is masked with dots; the eye button inside it shows or hides the value. **Save configuration** saves the connection first, then writes the key. If writing a key fails while Settings remains open, the configuration stays saved, the key stays in the field, and saving again retries it. Keys not submitted for saving exist only in memory; leaving Settings or deleting the connection discards them.

Each save captures the keys entered at that time and continues if you navigate away from Settings. Later edits remain a separate draft and require another save. A successful write removes surrounding whitespace from both the stored key and its field, clearing the unsaved-change indicator.

Native credential controls load independently, so entering Settings does not replace the page with a loading screen.

- Desktop automatically uses a supported OS credential store: Keychain, Windows credentials, Secret Service, or KWallet through `keyring`.
- If the store is unavailable or a write fails, the key stays **only in memory for this session**. The interface says that it must be entered again after restart. There is no storage-mode selector or plaintext fallback.
- A saved manual key is loaded back into its field, masked, when Settings opens; the eye button reveals it. Only the authenticated local Desktop UI can read it (`GET /desktop/credentials/{connection}/secret`, sent with `Cache-Control: no-store`). An unchanged field is not rewritten on save; clearing the field and saving removes the stored key.
- The **Environment variable** section supports an environment-variable name. With no manual credential selected, leave the name empty for the provider's default. An explicitly named variable never falls back to a different variable or connection's key. Restart Desktop after changing its inherited environment externally.
- **Use environment variable instead** clears the manual key and reads the environment variable. A missing manual/session key does not silently switch to an environment key.

Credential changes apply to newly created clients. Active tasks retain their configuration and credential snapshot. Renaming a connection moves its credential reference; deleting it prevents a replacement connection from inheriting its key.

SQLite stores only modes and opaque credential references. Keys are not saved in YAML/JSON, project state, or browser storage, and appear in no API response other than the authenticated local reveal endpoint above. Web and CLI retain their environment-variable behavior.

**Recheck** checks local configuration; it does not contact the provider or validate the key with a model request.

### Fetching model names

The refresh button next to **Model name** asks the model's connection for its model list (`POST /settings/models/list`). It uses the connection as currently edited, including an unsaved base URL and the key in its field, so nothing needs saving first. When the field is empty, the stored key is used. Supported providers: OpenAI-compatible endpoints (DeepSeek, OpenAI, OpenRouter, OpenCode Go, OrcaRouter, Ollama, vLLM, custom) via `GET /models`, and Gemini, limited to text-generation models. The request is sent once without retries, times out after 20 seconds, uses no tokens, and the list is capped at 2,000 names. Failures show a reason such as an invalid key or unreachable base URL; responses never include the key. Editing the connection clears a fetched list, and the name can always be typed by hand.

Lists and errors from requests started before a connection edit are ignored.

### MinerU PDF parsing key

The **PDF parsing** category contains the MinerU credential controls. This key authorizes PDF parsing,
not model inference. Its password input includes an eye button to show or hide the saved
manual key. Edits save automatically after a pause or when the field loses focus;
clearing the field removes the manual key. There is no separate save button.
Desktop automatically prefers `MINERU_API_KEY`; when present, the input indicates that
source and both the input and eye button are disabled. Environment keys are never shown.
Saving takes effect independently of model/configuration saves and uses the OS credential
store or, when unavailable, only the current session with a warning.
Provider renames, deletion, or restoring model defaults do not remove this key.
An invalid environment key does not fall back to a manual key; correct it and restart Desktop.

The engine resolves the key at execution time for a new MinerU conversion; cached PDF HTML
and other input formats do not read it. The key is never stored in YAML, project state, task
parameters, browser storage, or parser artifacts. Saving does not upload a document or
make a billable parser request.
Restart Desktop after changing its inherited environment externally.

## Import and save

- Drop a supported file from the file manager into the **Create project** dialog's source area, or use **Browse files**. Dropping only selects the file; **Create project** starts the upload. A native selection is a short-lived, single-use grant, not a general filesystem permission. Re-drop the file after an expired or failed native upload; the form retains its values and disables creation until a fresh file is selected.
- Native drag-event and high-DPI position fixes require restarting the updated native executable. Reloading the frontend alone does not update the Rust event handler.
- Desktop export opens a native destination picker **before** creating an export task. Canceling the picker creates no task and writes no output.
- Completed history entries offer **Save as…**. Saving streams through a temporary file in the destination directory and publishes only after completion. Existing files require confirmation; transfer failures leave the existing destination intact.
- Export history keeps the latest five completed entries. An older entry disappears from history immediately; already-open saves remain readable. File downloads defer deletion until their last stream closes, including on cancellation or disconnect. Legacy multi-file HTML history uses a temporary single-file conversion. Failed cleanup remains retryable on later export publication or restart.
- HTML is saved directly as **HTML**, with a `.html` filename and embedded readable image/media resources; there is no ZIP wrapper or companion assets directory. Existing embedded resources remain unchanged; remote URLs are not downloaded. Legacy HTML-plus-assets history is converted at save time using only that published HTML and its owned assets.
- Desktop refuses internal workspace destinations and source-file identities/paths recorded during the current app session. Source protection is bounded and kept in memory without retaining open source files. Do not replace a destination from another application while saving: an overwrite is an atomic file replacement on one volume when nothing else holds the target, not a cross-process compare-and-swap. The staged temporary file is complete before publication, but replacing an existing file is a plain rename: a process that opens, replaces or locks that destination during the save is not detected afterwards, and on Windows the rename fails rather than retrying if the target is held. Web continues to use browser downloads.
- **Import existing project** on the project list adopts a project folder that already exists — one this installation moved, or one copied here from another computer. The folder is checked before anything changes; a folder without `state.sqlite3`, one whose saved metadata is unreadable, or one with no single recognizable original is refused, and the list names which of those it was. An accepted folder is moved into the workspace and added to the list with the state its saved chapters imply.
- If no original matches the saved checksum, explicit import refuses the folder before copying or moving it. Restore the matching original or create a new project for changed text. Existing translations and checkpoints are not reset or attached to different source content.
- Import requires a current-format project marker agreeing with the project ID. Old-format or unmarked folders are not supported. Startup does not reconstruct the catalog from project folders.
- A project whose folder is not reachable — a removed drive, or an unmounted network share — stays in the list, greyed out with no way to open it. Writes refuse a missing uploaded-project directory rather than creating an empty replacement. Removing its catalog entry still works without recreating the folder or deleting another same-ID copy; data on the disconnected drive remains untouched.
- The Web interface has no folder picker, so **Import existing project** is absent there rather than shown and disabled.

## Diagnostics and feedback

Open **Settings → Diagnostics and feedback → Export diagnostic report** when reporting a Desktop problem. Select **Last 24 hours** (default), **Current launch**, or **Last 7 days**, then choose a destination in the system save dialog. Canceling that dialog creates no archive or book export task; canceling collection or copying preserves the existing destination and is not reported as a failure. Existing files require confirmation and are replaced only after the ZIP is complete and verified. Diagnostic exports do not consume book export history.

The startup error screen and page error fallbacks also offer export. If the Python engine or React UI is unavailable, use **Export diagnostic report…** in the native application/tray menu, or **Ctrl+Shift+D** (**Cmd+Shift+D** on macOS). This native entry does not need a running engine. If native startup itself fails, an available native error dialog offers a memory-only report; a completely unlaunchable executable cannot export itself.

Reports contain `manifest.json`, a readable `report.md`, basic version/OS/architecture information, and available `logs/native.jsonl`, `logs/engine.jsonl` and `logs/frontend.jsonl`. They cover startup/exit, local task boundaries, HTTP failures, safe model retry/call summaries, frontend failures, native saves and update failures. The selected range includes content-free task metadata across this Desktop workspace; project-specific selection and project-event/configuration/usage summaries are not included yet. The ZIP is limited to 25 MiB with a 20 MiB record-selection budget. The manifest records missing sources, losses, malformed records, size omissions and the retained time coverage. `complete` means available sources were collected without a known loss, not that every event in the requested historical window still exists, every error was instrumented, or a root cause has been identified.

Startup failures retain the first safe exception classification, application module/line,
and stage (application assembly, runtime initialization, plugin restoration or listening)
before ASGI can replace the exception with `SystemExit`. Known OS/SQLite error codes and
explicit cause classifications may also be included; exception messages, paths and
tracebacks are not. Native records distinguish spawn failure, invalid/broken protocol,
EOF before readiness, and readiness timeout. `engine_exited` records the observed process
exit code or Unix signal and whether shutdown was requested or forced. Forced cleanup
is not evidence of the original failure. `engine_stopped` reports whether the engine
reached readiness and whether its lifecycle failed; it does not alone prove success.
These records are enabled without detailed logging, including on the startup error screen.
Packaging smoke checks use the same diagnostic protocol and Windows no-console launch,
and verify both normal offline startup and a real, isolated workspace-lock startup failure.

No book text, prompts, model responses, credentials, request bodies, raw exceptions/configuration, project databases or usage ledgers are included. Arbitrary stdout/stderr is not captured. IDs used for task correlation are pseudonymized, but timings and platform metadata may still identify you. **Review the ZIP before sharing it. Nothing is uploaded automatically.** Attach it yourself with reproduction steps, expected/actual behavior and the error ID. **Copy troubleshooting summary** copies the version, latest error ID and log-health information without the log path.

HTTP response request/error IDs match the corresponding diagnostic record.
Desktop generates opaque IDs rather than persisting caller-supplied request IDs.
Native save and drag-and-drop failures retain readable error messages even when
the native command returns a structured error code. Diagnostic dialogs retain
their localized retry guidance rather than displaying raw exceptions.

Normal capture is INFO and above. **Enable detailed logging for 30 minutes** adds safe request/attempt/usage observations, never content; it expires automatically, can be stopped early and resets on restart. Reproduce the issue while it is enabled, then export. Logging settings do not change translation configuration or caches.

The native process is the only file writer. Logs live in `<data-root>/logs/`, rotate at 10 MiB or 24 hours, and have a 200 MiB total limit; files last written over 7 days ago are removed, and a file written in the last minute is left alone so an export that is reading it does not lose half a report. Export additionally filters individual records to the selected time range. If that location is unavailable, only a bounded memory buffer is used; export before exiting. Logs never fall back to a hidden second data directory. A previous active-session marker is reported as an unclean exit, not proof of a crash.

The log folder is an ordinary directory, so a file that another program puts in it is counted toward the 200 MiB limit — otherwise the limit would describe only this application's own output while the disk filled. Those files are never read into a report, never removed by retention and never removed by **Clear application logs**. The settings page reports them separately ("N file(s) in this folder were written by another program, M MiB") and says that Desktop will not delete them, so a large figure can be traced to the right place.

**Open log folder** opens only the managed location. **Clear application logs** asks for native confirmation and removes only owned log files, not projects, translations, checkpoints or usage. Clearing and export cannot run simultaneously; logging can continue. Logs are best-effort and not a backup or a second usage ledger. CLI and Web logging are unchanged.

## Startup, shutdown, and rendering

The UI has quiet startup/closing transitions without loading text. Startup errors remain visible with a retry/reload action. Requests wait for the local engine's ready handshake; the app never falls back to a remote endpoint. The backend listens only on a randomly allocated loopback port and uses a fresh in-memory token on every launch.

### Reopening an already running app

Opening the executable or shortcut again restores the existing window for that
workspace, including when it is hidden in the tray or minimized. It does not
create another window or engine, reload the page, interrupt tasks, or discard
editor drafts and session-only credentials. Repeated launches during startup
are coalesced into one pending restore request.

Instance ownership is per workspace, not per executable path or application
version. Separate `--data-dir` workspaces can run independently; path aliases
to the same directory share one owner. Native admission happens before creating
the WebView or starting Python. The existing backend workspace lock remains a
separate data-safety check.

If the existing app is closing, its previous engine is still shutting down, or
the window cannot be restored, a native message explains what to do instead of
opening a second failing interface. Try **Show Wenyi** in the tray, or wait for
shutdown to finish and reopen. Desktop never force-stops the existing process.

The workspace's `.desktop-instance.lock` is OS-owned. The accompanying
`.desktop-instance.json` only advertises an activation-only loopback port and
generation ID, never an API token or provider credential. These files can remain
after exit or a crash; their presence does not mean the app is running. Do not
delete them to bypass a live owner. Actual process exit releases the OS lock;
normal quit and updater restart release it after engine cleanup.

### Windows engine ownership

On Windows, a debug Python override must name an existing executable path (not a
PATH command or wrapper). For a CPython venv, Desktop reads `pyvenv.cfg` and starts
its base `python.exe` directly with CPython's `__PYVENV_LAUNCHER__` hint. This is
the same mechanism used by CPython's venv redirector: imports and `sys.executable`
still belong to the venv, but the engine itself is the owned child. The ready PID
must still match that child exactly; readiness never authorizes opening or killing
another PID. Invalid venv configuration fails startup rather than falling back to
the redirector. Packaged onedir engines continue to start directly, without Python
or venv discovery. This single-process contract avoids needing a Windows Job
Object and suspended-process assignment just to own a redirector's descendant.

### Background operation

- Minimizing the window keeps local tasks running. Closing the window hides it to the
  system tray instead of exiting. Use **Show Wenyi** in the tray menu to restore it;
  reopening from the macOS Dock also restores the window.
- If the tray cannot be created, closing minimizes the window instead of hiding it.
  The taskbar/Dock remains available.
- Hidden windows and platform-reported minimization pause periodic UI polling,
  progress-event refetches, and live elapsed-time timers. The progress subscription and Python engine remain
  running; already-started requests and native saves are not canceled. Restoring
  the window immediately reconciles saved state, including tasks completed in the
  background. An ordinary loss of focus does not count as minimization.
- Hiding retains the WebView, unsaved editor drafts, and session-only credentials.
  This reduces unnecessary UI work, not the resident memory of the Python engine
  or WebView. It does not keep the computer awake or prevent operating-system sleep.

On Linux/GTK Wayland, compositor-side minimization is not reliably reported to
the application. It still keeps tasks running, but UI polling/timers may continue.
Use close-to-tray for reliably detected background operation; an ordinary blur
is deliberately not treated as minimization. Restoring from the tray remaps the
same native window to handle GTK/Wayland's deiconify limitation, without replacing
the WebView or its drafts.

Linux and Windows hide the native top window menu bar while keeping its menu
events and **Ctrl+Q** quit accelerator registered, even without a usable tray.
The bar stays hidden after repeated tray restores or reopening the running
application; Linux window remapping does not show it again.
macOS retains its system application menu and **Cmd+Q**. Tray **Show Wenyi** and
**Quit Wenyi** remain available when a tray host is present.

Use **Quit Wenyi** in the tray, the macOS application menu, or the quit shortcut
to actually exit.
Explicit exit stops accepting work, checkpoints/cancels local tasks, and shuts
down the owned backend. Saved progress can be resumed after reopening. Save
editor drafts before quitting; an unsaved in-memory draft is not a persisted
checkpoint. Background translation continues to make configured model requests
and can incur provider usage until the task finishes or is paused.

On Linux, native Wayland is preferred when available; X11 is a connection-time fallback, not a global override. For proprietary NVIDIA drivers, Desktop uses a process-local explicit-sync compatibility setting on native Wayland while keeping DMA-BUF enabled. NVIDIA/X11 and NVIDIA/Hyprland use a separate DMA-BUF fallback. Explicit user graphics environment settings take precedence.

If the window still fails, try this diagnostic for one launch rather than setting it globally:

```bash
WEBKIT_DISABLE_DMABUF_RENDERER=1 pnpm desktop
```

The NVIDIA/KDE Wayland `Gdk Error 71` was reproduced with a native GTK/WebKit probe and avoided by the targeted explicit-sync setting. This confirms that compatibility case, not universal GPU performance. [Interface performance measurements](../apps/web/tests/proofreading-performance.md) distinguish Chromium checks from native Tauri/WebKitGTK observations, including remaining large-chapter stalls.

The Rust backend rewrite remains paused. Desktop uses the existing Python engine behind the shared backend interfaces; Rust owns only native application capabilities and process lifecycle.