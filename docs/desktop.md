# Wenyi Desktop

[简体中文](zh/desktop.md) · [Web deployment and development](web.md)

Desktop is a **local translation application**, built with Tauri, React, and the existing Python translation engine. It starts its own private backend and SQLite workspace. It does not require a Web deployment, PostgreSQL, Redis, or Docker. Packaged releases include the Python runtime; users do not need to install Python.

Translation still needs access to the configured model provider. Optional MinerU and BabelDOC services retain their existing requirements; “local application” does not make external model or document services offline.

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

The local Linux x86-64 preview build produced a **50.34 MiB `.deb`**, **50.35 MiB `.rpm`**, and **143.02 MiB AppImage**. The AppImage includes additional Linux runtime libraries; these are compressed package sizes, not memory usage. The native wrapper currently uses the development version `0.0.0`; these unsigned preview bundles are not a published release.

The AppImage was launched on KDE Wayland with a temporary workspace and an invalid development-Python path. Its bundled engine started, authenticated loopback requests succeeded, and closing the native window shut down and reaped that engine. Separate frozen-engine checks exercised offline synthetic TXT upload/parse/preview and existing/new-format event reads. Real file-manager drag/drop, OS save dialogs, Windows/macOS execution, and removable-media behavior still require platform acceptance testing.

## API keys

Open **Settings → API providers & models**, configure the provider/model and optional base URL, then save the connection configuration. Enter the API key in its password field and save it.

- Desktop automatically uses a supported OS credential store: Keychain, Windows credentials, Secret Service, or KWallet through `keyring`.
- If the store is unavailable or a write fails, the key stays **only in memory for this session**. The interface says that it must be entered again after restart. There is no storage-mode selector or plaintext fallback.
- A saved key is never shown again. An empty input does not clear or replace it.
- The advanced section supports an environment-variable name. With no manual credential selected, leave the name empty for the provider's default. An explicitly named variable never falls back to a different variable or connection's key. Restart Desktop after changing its inherited environment externally.
- Use **Clear manual key** to remove it, or the explicit action to clear it and use the environment variable. A missing manual/session key does not silently switch to an environment key.

Credential changes apply to newly created clients. Active tasks retain their configuration and credential snapshot. Renaming a connection moves its credential reference; deleting it prevents a replacement connection from inheriting its key.

SQLite stores only modes and opaque credential references. Keys are not saved in YAML/JSON, project state, browser storage, or API responses. Web and CLI retain their environment-variable behavior.

**Check local availability** checks local configuration; it does not contact the provider or validate the key with a model request.

## Import and save

- Drop a supported file from the file manager onto the new-project page, or use Browse. Dropping only selects the file; **Create** starts the upload. A native selection is a short-lived, single-use grant, not a general filesystem permission. Re-drop the file after an expired or failed native upload.
- Desktop export opens a native destination picker **before** creating an export task. Canceling the picker creates no task and writes no output.
- Completed history entries offer **Save as…**. Saving streams through a temporary file in the destination directory and publishes only after completion. Existing files require confirmation; transfer failures leave the existing destination intact.
- HTML is explicitly saved as **HTML + assets (ZIP)**, with a `.html.zip` filename. Extract the archive before opening the HTML so relative image/media links continue to work. Only that published HTML and its owned assets are included.
- Desktop refuses internal workspace destinations and source-file identities/paths recorded during the current app session. Source protection is bounded and kept in memory without retaining open source files. Do not replace a destination from another application while saving: an overwrite is an atomic file replacement, not a cross-process compare-and-swap. Web continues to use browser downloads.

## Startup, shutdown, and rendering

The UI has quiet startup/closing transitions without loading text. Startup errors remain visible with a retry/reload action. Requests wait for the local engine's ready handshake; the app never falls back to a remote endpoint. The backend listens only on a randomly allocated loopback port and uses a fresh in-memory token on every launch.

Closing Desktop stops accepting work, checkpoints/cancels local tasks, and shuts down its owned backend. Saved progress can be resumed after reopening. Save in-progress editor drafts before closing; an unsaved in-memory draft is not a persisted checkpoint.

On Linux, native Wayland is preferred when available; X11 is a connection-time fallback, not a global override. For proprietary NVIDIA drivers, Desktop uses a process-local explicit-sync compatibility setting on native Wayland while keeping DMA-BUF enabled. NVIDIA/X11 and NVIDIA/Hyprland use a separate DMA-BUF fallback. Explicit user graphics environment settings take precedence.

If the window still fails, try this diagnostic for one launch rather than setting it globally:

```bash
WEBKIT_DISABLE_DMABUF_RENDERER=1 pnpm desktop
```

The NVIDIA/KDE Wayland `Gdk Error 71` was reproduced with a native GTK/WebKit probe and avoided by the targeted explicit-sync setting. This confirms that compatibility case, not universal GPU performance. Interface performance measurements are documented separately from correctness tests.

The Rust backend rewrite remains paused. Desktop uses the existing Python engine behind the shared backend interfaces; Rust owns only native application capabilities and process lifecycle.
