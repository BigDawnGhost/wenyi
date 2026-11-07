# Wenyi Desktop

Desktop runs locally, with its own projects and settings. Release packages include the translation engine; you do not need Python, Docker, or a Web server. Desktop does not read or migrate CLI or Web projects.

Local storage does not mean offline translation. Unless you configure a local model service, text is sent to your selected model provider. PDF parsing may also use an external service.

## Install Desktop

Download a `wenyi-desktop-<version>-<platform>-<arch>` package from [GitHub Releases](https://github.com/BigDawnGhost/wenyi/releases):

| Platform | Package |
| --- | --- |
| Windows x64 | `.exe` installer |
| Linux x64 | `.AppImage`, `.deb`, or `.rpm` |
| macOS Apple Silicon | `macos-arm64.dmg` |
| macOS Intel | `macos-x64.dmg` |

Packages are distributed directly, without an outer ZIP. Allow an AppImage to execute in its file permissions before opening it. Choose the macOS package for your processor; there is no universal package. Check the release notes for platform requirements and signing status.

Keep the program installation separate from your documents. The Windows installer defaults to `%LOCALAPPDATA%\Programs\Wenyi Desktop`; changing this location does not move your data. It refuses unsafe or unrelated occupied folders, including locations overlapping Wenyi's data directories. Choose a dedicated empty program folder or the existing Wenyi installation.

On Windows, run setup as your normal user, not **Run as administrator**. If setup reports that Wenyi or its engine is still running, quit through the tray and wait for tasks to stop; setup does not force-stop translations. Installing an older version over a newer one is refused.

After installation, set up [API keys](#api-keys), then follow the shared [workspace guide](workspace.md) to create, translate, proofread, and export a book. Building from source belongs in the [Desktop development guide](../development/desktop.md).

## API keys

### Model connections

Open **Settings → Models & connections**, add or select a connection, and enter its API key and any custom base URL. **Save configuration** saves the connection and submits the key together; there is no separate model-key save step. An unsaved key is only an in-memory draft and is discarded when you leave Settings.

- Desktop prefers the system credential store: macOS Keychain, Windows credentials, or a supported Linux Secret Service/KWallet store.
- If that store is unavailable or cannot save the key, Desktop keeps it **only for the current session** and shows a warning. Enter it again after restarting. There is no plaintext-file fallback.
- Saved manual keys appear masked; the eye control reveals them. Clearing a model-key field and saving removes its saved key.
- **Use environment variable instead** removes the manual credential and switches to the configured environment variable. With no explicit variable name, the provider's default is used. A missing manual/session key does not silently fall back to an environment key; an explicitly named variable does not fall back to another variable.
- Restart Desktop after changing its inherited environment outside the app. Credential changes affect new clients, not the configuration and credential snapshot of an active task.

Wenyi does not write secrets to YAML/JSON, SQLite, project files, browser storage, or logs. Project backups contain credential references, not the secrets held by the system vault or session memory.

For model registration, fetching names, and **Test connection**, see [Choose models and workflow](workspace.md#choose-models-and-workflow). Fetching names uses no model tokens; **Test connection sends live, potentially billable model requests**. **Validate configuration** checks the current draft without sending a model request.

### MinerU PDF parsing key

Under **Settings → PDF parsing**, enter the MinerU key for PDF conversion. It is separate from model API keys.

**Manual MinerU edits save automatically after typing pauses or the field loses focus. There is no Save button.** Clearing the field removes the manual key. Saving uses the system credential store or, with a warning, session-only memory, independently of model/configuration saves.

`MINERU_API_KEY` takes priority when present. The field then shows the environment source and cannot be edited or revealed. An invalid environment key does not fall back to a manual key: correct it and restart Desktop. Renaming/deleting model connections or restoring model defaults does not remove the MinerU key.

Saving this key does not upload a document or make a billable parsing request. It is needed for a new MinerU conversion, not for other formats or reuse of a matching PDF parse cache.

## Data locations and backups

Originals and project state are kept together in each project's directory. **Project folder** in the creation dialog selects the parent for that new project only; existing projects retain their recorded locations.

Desktop uses one **data root**, separate from its program directory:

| Platform | Default data root |
| --- | --- |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/wenyi/Desktop` |
| macOS | `~/Library/Application Support/Wenyi/Desktop` |
| Windows | `%LOCALAPPDATA%\Wenyi\Desktop` |

The root contains the project list and settings in `catalog.sqlite3`, default project files in `projects/`, logs in `logs/`, locks in `locks/`, plugins in `runtime/`, and upgrade snapshots in `backups/`. API keys remain in the system vault or session memory, not these files.

New projects default to `<data-root>/projects/`, not a folder beside the installed program. A chosen project library may be on another local volume. Protected program, system, settings, and cache locations are refused; existing projects keep their recorded locations.

`--data-dir <path>` selects the entire data root, including its catalog, settings, plugins, and logs. Different roots do not share a hidden catalog. Selecting another root does not move or discover existing projects. On Linux, only an absolute `XDG_DATA_HOME` is used; unset, blank, or relative values fall back to `~/.local/share`. Windows and macOS ignore that variable.

**Older Desktop layouts are not migrated automatically.** Their catalogs, settings, credentials, projects, and plugins remain untouched, and the new default opens with an empty project list. There is no old-format import or migration command; renaming an old catalog does not upgrade it. Keep the old data and an appropriate older release if you need to reopen it.

**Keep the data root and project libraries on local disks.** Network shares, mapped drives, and cloud-synced folders such as OneDrive, Dropbox, iCloud Drive, and Google Drive are unsupported. Database locking, placeholder files, and out-of-order synchronization can damage saves or produce incomplete backups. Desktop does not detect every such location.

For a restorable backup:

1. Save editor/settings drafts, pause or finish all tasks, and **quit every Desktop instance**. Hiding to the tray is not quitting.
2. Copy the **entire data root** to a separate backup location, including database sidecar files such as `-wal` and `-shm`; do not copy just a single SQLite database.
3. Also copy **every registered external project directory**. Backing up the root alone is not enough for projects in custom libraries.
4. Retain the [BabelDOC plugin and PDF sessions](#optional-babeldoc-plugin) under the root's `runtime/`. Keep separately saved exports wherever you normally back up documents.

Restore a consistent set of catalog and project files, not a lone older database mixed into newer data. Unidentified or unsupported catalogs are refused rather than upgraded. Verify restored/imported projects before removing their original backup. System-vault keys are outside this backup and may need to be entered again on another computer. Windows/Linux WebView data lives in `runtime/webview/`; macOS manages WKWebView storage outside this root.

## Import existing projects

Use **Import existing project** on the project list for a Desktop project directory moved on this computer or copied from another computer. This is different from selecting a source book in **Create project**, and is not a CLI/Web migration tool.

Import accepts only **current-format Desktop projects**. It checks saved state, project identity, and the original file before accepting the folder. Old or missing project markers, missing `state.sqlite3`, unreadable metadata, an ambiguous original, a checksum mismatch, or a damaged/conflicting marker are refused. Restore the matching original rather than attaching saved translations to changed source text. Already registered project identities are not imported again.

**An accepted directory is moved into `<data-root>/projects/`**, then added to the project list using its saved progress. Import a copy if you need to retain the original folder untouched. Startup does not scan project folders to rebuild a lost catalog; restore a consistent backup instead.

If a project's recorded drive or folder is unavailable, it stays in the list but cannot be opened. Reconnect it; Desktop does not silently use another same-ID copy or create an empty replacement. Removing an unavailable project's catalog entry does not erase data on the disconnected drive. Web has no project-folder import.

For a new source file, drag it into **Create project** or use **Browse files**. Selection alone does not upload it. After a failed or expired native upload, select/drop the file again before retrying.

## Optional BabelDOC plugin

Use **Settings → BabelDOC plugin → Enable and download** for layout-preserving PDF translation. The plugin is off by default and downloads a separate **AGPL-3.0-only** runtime and model/font pack from the official [BabelDOC bridge releases](https://github.com/BigDawnGhost/wenyi-babeldoc-bridge/releases). Ordinary Desktop installation does not download it.

Wait for **Ready**, then select **BabelDOC** as the parser when creating a PDF project. Enabling the plugin neither changes the default parser from MinerU nor converts existing projects. Only projects parsed with BabelDOC can use layout-preserving PDF export.

- Canceling setup disables the plugin; **Retry** reuses completed, verified downloads. An interrupted installation needs a retry.
- An installed, enabled plugin starts with Desktop. Disabling it retains installed files, models, and PDF sessions; finish project work before disabling.
- **Check for plugin updates** and **Update plugin** are manual. Restarting or re-enabling does not upgrade it automatically.
- Switching versions waits for all project jobs, exports, and native saves to be idle. Cancellation or installation/startup failure retains the previous version. Old installations and sessions are not deleted; a bridge compatibility change may require parsing affected PDFs again.

Plugin files and retained PDF sessions live in `<data-root>/runtime/plugins/babeldoc/`. Plugins from older layouts are neither reused nor migrated; install the plugin again in the new root when needed. Include this directory in your manual backups; application-update snapshots cover the catalog and registered projects, not the plugin runtime and sessions.

## Application updates

Open **Settings → About** to check the current version, check for updates, and read release notes. Configured stable releases also check at startup. A check does not silently download, install, or restart the application; installation requires confirmation.

Before installing, finish or pause active project tasks, wait for exports, saves, and plugin setup to finish, and save or discard unsaved editor/settings drafts. Close other copies of the same program, even if they use different data roots.

Supported release builds offer in-app installation on Windows, macOS, and Linux **AppImage**. macOS updates keep the installed architecture. Linux `.deb`/`.rpm` users update through downloaded packages, not AppImage replacement. Older or unconfigured builds may require a manual update. A network/check failure does not prevent continuing to use Desktop.

An in-app installation first stops writes and the engine, then backs up **all registered project directories** and `catalog.sqlite3` to `<data-root>/backups/<version>-<timestamp>/`. The backup includes any database WAL/SHM files and a `MANIFEST.json` recording original locations and file verification data. It does not copy earlier backups, export credential secrets, or replace your regular backups.

An unavailable project directory, insufficient disk space, unsafe link, or failed copy/verification blocks installation. Reconnect the missing drive or fix the reported problem and retry. If update preparation cannot be canceled cleanly, quit and reopen Desktop instead of assuming writes have resumed.

A manually run installer neither creates this snapshot nor requires a historical backup marker. Back up current data yourself before replacing the program; prefer updating inside Wenyi.

### Windows uninstallation

Uninstall keeps data by default. Its two independent deletion options are initially unchecked:

- Delete the catalog, settings, logs, locks, and runtime, **not project files**.
- Permanently delete default project files and upgrade backups.

Both options apply only to `%LOCALAPPDATA%\Wenyi\Desktop`; custom data roots, external project libraries, and legacy directories are retained. Deleting the catalog also loses the project list, even if project files remain; those files are not automatically rediscovered. Silent uninstall and upgrade replacement do not select either option. For recovery or reinstalling, leave both unchecked and retain a separate backup.

## Close or quit Desktop

- Minimizing or closing the window does **not** pause translation. Close hides it to the system tray; if no tray is available, it minimizes instead. Background work can continue to incur model charges.
- Use **Show Wenyi**, the macOS Dock, or open the executable/shortcut again to restore the same window without losing its in-memory drafts or session credentials.
- Use **Quit Wenyi**, **Ctrl+Q** on Windows/Linux, or **Cmd+Q** on macOS to exit. Desktop stops accepting work and lets tasks stop safely; reopen the project to resume saved progress.
- **Save drafts before quitting.** Unsaved proofreading, style, and settings edits are not translation checkpoints. Hiding a window preserves memory, not a durable backup, and does not prevent computer sleep.

If a previous instance is still closing, wait for it to finish rather than trying to bypass its lock.

## Diagnostics and feedback

For a Desktop problem:

1. Note what you expected, what happened, and the visible error ID. If needed, enable **Settings → Diagnostics and feedback → Enable detailed logging for 30 minutes**, then reproduce the issue. It expires automatically and resets after restart.
2. Choose **Export diagnostic report**, select **Last 24 hours**, **Current launch**, or **Last 7 days**, and save the ZIP. Canceling creates no book export task or replacement file; replacing an existing file requires confirmation.
3. **Inspect the ZIP before sharing it. Nothing is uploaded automatically.** Attach it yourself with reproduction steps and the error ID. **Copy troubleshooting summary** provides the version, latest error ID, and log-health information.

If the main interface or translation engine fails, use **Export diagnostic report…** in the native application/tray menu or **Ctrl+Shift+D** (**Cmd+Shift+D** on macOS). Startup error screens also offer export. A completely unlaunchable executable cannot export its own report.

Reports contain version/platform information and available application logs, not book text, prompts, model responses, credentials, raw configuration, or project databases. Timing and platform metadata can still identify you. Reports are bounded in size and may omit unavailable or older records; they are neither a full historical record nor a project backup. If logging has fallen back to memory only, export before quitting.

**Open log folder** finds the managed log location. **Clear application logs** asks for confirmation and removes only Wenyi-owned logs, not projects, translations, checkpoints, usage, or files written there by other applications.
