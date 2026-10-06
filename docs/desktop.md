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

Desktop assets are distributed directly, without an outer ZIP. For an AppImage, allow execution in the file's permissions before opening it. Packaged releases include the translation engine: you do not need to install Python or deploy a server.

Check the release notes for platform requirements and signing status. To build from source instead, follow [Run from source](#run-from-source).

### 2. Connect a model

Open **Settings → API providers & models**, choose a provider, configure models and any custom base URL, and save the connection. Enter and save the API key in its password field.

Desktop uses the system credential store when available. If it is unavailable, the interface explains that the key is kept only for the current session and must be entered again after restart. A local workspace does not mean offline model processing: text is sent to the provider you configure, unless you use a local model service.

### 3. Create a translation project

Use **Create project** in the shared sidebar to open the centered dialog over the project list. The list remains visible but inactive behind it; `/projects/new` opens the same dialog directly. Focus starts in **Project name** and stays inside the dialog with Tab. Escape closes an open dropdown first. **Cancel**, **Close**, the backdrop, or Escape returns to the previous page (the project list for a direct link) without creating a project and restores focus to the sidebar entry. Browser Back also leaves the dialog. Closing releases any selected native source resource; reopening starts a clean form. On narrow screens the form scrolls while the close header stays visible.

Drag in a supported book or choose it with **Browse files**, and select the source and target languages. Source-language detection can be automatic. Choose **Standard** translation or, for books, **Three drafts + synthesis**, which creates three drafts and synthesizes them at higher model cost. During upload, source replacement and dialog dismissal are disabled; this is not a task cancellation action. Failed uploads retain the form and source selection; a failed native upload requires re-dropping the file to renew its single-use grant before retrying. Successful creation enters the project overview while parsing or optional preparation continues in the background. Legacy `/projects/new?project=<id>` links retain read-only source preview/resume controls.

Browser Back can still leave during an upload; it does not cancel the request. The source resource remains available until the request settles, then is released. The project may be created in the background, and completion will not redirect you away from the list.

Start translation from the project page. Wenyi parses the source, prepares whole-book context, and translates in batches. Progress, usage, and completed chapters are visible in the application; polishing and whole-book review are configurable.

### 4. Proofread and save

Compare the translation with the source, edit paragraphs, inspect revisions, and review reported issues. Review can publish fixes to the translation; disable automatic fixes when you want a read-only review. Desktop and Web share these workspace pages; see the [interface preview](../README.md#interface-preview) for a screenshot.

Choose an export format and, where supported, a bilingual edition. Desktop opens the system save dialog before starting the export; canceling creates no export task. HTML is saved as an HTML-and-assets ZIP, which should be extracted before reading.

### Continue later

Completed batches are saved in the local workspace. Reopen Desktop, open the same project, and resume from its checkpoints. Save any in-progress proofreading edits before closing. For workspace locations and backup instructions, see [Independent data](#independent-data).

## Interface language

The interface defaults to English. Open global **Settings → Interface language**
to choose **English** or **简体中文**. The change applies immediately to interface
labels, not the book's source/target languages, content, or model-generated analysis.

## Independent data

Desktop does not adopt or migrate existing Web projects. It also does not read or modify the CLI's `config.yaml`, `state/`, or `output/`. CLI behavior remains unchanged.

The default Desktop workspace is:

| Platform | Location |
| --- | --- |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/Wenyi Desktop` |
| macOS | `~/Library/Application Support/Wenyi Desktop` |
| Windows | `%LOCALAPPDATA%/Wenyi Desktop` |

The workspace contains a SQLite catalog, per-project SQLite state, uploaded originals, parse caches, and generated exports. Keep the entire workspace when backing up; stop Desktop before taking a filesystem backup. Do not copy only a live SQLite database while omitting its WAL.

Use `--data-dir <path>` to select a separate workspace, for example for testing. One backend process owns a workspace at a time. Choosing another workspace neither imports nor deletes the old one.

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

Build a native release on the target platform with:

```bash
pnpm desktop:build
```

The build freezes the Python engine as an onedir sidecar, then bundles it with the UI and native executable. Onedir avoids unpacking the entire runtime on every launch. Installers, signing, and platform-specific runtime dependencies must be verified for each release; a successful Linux build is not evidence of Windows/macOS validation.

Windows release builds open only the application window, not a console. The local
engine runs without a console window while retaining its private communication pipes.
Debug/source launches keep their development console for diagnostics.

Native bundles use PNG, ICO and ICNS artwork for Linux, Windows and macOS respectively.
To regenerate the native icons from the shared emblem, run
`pnpm exec tauri icon packages/ui/src/assets/wenyi-emblem.png --output /path/to/temporary-icons`
from the repository root, then copy only `icon.ico` and `icon.icns` into `apps/desktop/icons`.

### Versions and release downloads

Python metadata continues to use `hatch-vcs`; `scripts/release_version.py` uses
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

These workflows do not sign or notarize packages. Platform signing and end-user
installation checks remain release responsibilities.

The AppImage was launched on KDE Wayland with a temporary workspace and an invalid development-Python path. Its bundled engine started, authenticated loopback requests succeeded, and the previous close-to-exit lifecycle shut down and reaped that engine. Separate frozen-engine checks exercised offline synthetic TXT upload/parse/preview and existing/new-format event reads. The current tray lifecycle was also checked on KDE Wayland using a debug native build, production UI, source Python engine, and temporary empty workspace: minimize/restore and close-to-tray/restore kept the engine alive, and explicit tray exit reaped it. Packaged tray behavior, real file-manager drag/drop, OS save dialogs, Windows/macOS execution, and removable-media behavior still require platform acceptance testing.

## API keys

Use **Settings** in the shared sidebar footer. `/settings` contains Interface language, API providers & models, New project defaults, and Advanced YAML configuration together, with save, validate, and restore defaults. There is no separate settings layout or category navigation. Legacy `/settings/providers`, `/settings/defaults`, and `/settings/advanced` links redirect to `/settings`.

The project list, projects, and Settings share one persistent, collapsible sidebar: **Create project**, **Projects**, current/last project navigation, and **Settings** in the footer. There are no floating dashboard actions. Same-page history navigation dismisses open dropdowns without resetting configuration drafts. Notifications stay fixed in the viewport; success lasts one second, errors/warnings ten seconds, and other messages five seconds. Glossary add/edit failures also remain inline in the dialog.

Open **Settings → API providers & models**, configure the provider/model and optional base URL, then save the connection configuration. Enter the API key in its password field and save it.

Native credential controls load independently, so entering Settings does not replace the page with a loading screen.

- Desktop automatically uses a supported OS credential store: Keychain, Windows credentials, Secret Service, or KWallet through `keyring`.
- If the store is unavailable or a write fails, the key stays **only in memory for this session**. The interface says that it must be entered again after restart. There is no storage-mode selector or plaintext fallback.
- A saved key is never shown again. An empty input does not clear or replace it.
- The advanced section supports an environment-variable name. With no manual credential selected, leave the name empty for the provider's default. An explicitly named variable never falls back to a different variable or connection's key. Restart Desktop after changing its inherited environment externally.
- Use **Clear manual key** to remove it, or the explicit action to clear it and use the environment variable. A missing manual/session key does not silently switch to an environment key.

Credential changes apply to newly created clients. Active tasks retain their configuration and credential snapshot. Renaming a connection moves its credential reference; deleting it prevents a replacement connection from inheriting its key.

SQLite stores only modes and opaque credential references. Keys are not saved in YAML/JSON, project state, browser storage, or API responses. Web and CLI retain their environment-variable behavior.

**Check local availability** checks local configuration; it does not contact the provider or validate the key with a model request.

## Import and save

- Drop a supported file from the file manager into the **Create project** dialog's source area, or use **Browse files**. Dropping only selects the file; **Create project** starts the upload. A native selection is a short-lived, single-use grant, not a general filesystem permission. Re-drop the file after an expired or failed native upload; the form retains its values and disables creation until a fresh file is selected.
- Native drag-event and high-DPI position fixes require restarting the updated native executable. Reloading the frontend alone does not update the Rust event handler.
- Desktop export opens a native destination picker **before** creating an export task. Canceling the picker creates no task and writes no output.
- Completed history entries offer **Save as…**. Saving streams through a temporary file in the destination directory and publishes only after completion. Existing files require confirmation; transfer failures leave the existing destination intact.
- Export history keeps the latest five completed entries. An older entry disappears from history immediately; already-open saves remain readable. File downloads defer deletion until their last stream closes, including on cancellation or disconnect; an HTML ZIP uses its own temporary archive. Failed cleanup remains retryable on later export publication or restart.
- HTML is explicitly saved as **HTML + assets (ZIP)**, with a `.html.zip` filename. Extract the archive before opening the HTML so relative image/media links continue to work. Only that published HTML and its owned assets are included.
- Desktop refuses internal workspace destinations and source-file identities/paths recorded during the current app session. Source protection is bounded and kept in memory without retaining open source files. Do not replace a destination from another application while saving: an overwrite is an atomic file replacement, not a cross-process compare-and-swap. Web continues to use browser downloads.

## Startup, shutdown, and rendering

The UI has quiet startup/closing transitions without loading text. Startup errors remain visible with a retry/reload action. Requests wait for the local engine's ready handshake; the app never falls back to a remote endpoint. The backend listens only on a randomly allocated loopback port and uses a fresh in-memory token on every launch.

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

The NVIDIA/KDE Wayland `Gdk Error 71` was reproduced with a native GTK/WebKit probe and avoided by the targeted explicit-sync setting. This confirms that compatibility case, not universal GPU performance. Interface performance measurements are documented separately from correctness tests.

The Rust backend rewrite remains paused. Desktop uses the existing Python engine behind the shared backend interfaces; Rust owns only native application capabilities and process lifecycle.
