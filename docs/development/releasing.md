# Releasing

This page covers Desktop packaging, update publication and the bilingual release
notes contract. See [Desktop development](desktop.md) for local setup and runtime
invariants, [Publishing Python packages](publishing.md) for PyPI, and the
[Desktop guide](../guide/desktop.md) for end-user installation and update actions.

## Release identity

Python package metadata uses `hatch-vcs`.
[The release-version resolver](../../scripts/release/release_version.py#L24)
uses the same setuptools-scm Git source for native application and asset versions:

| Git identity | Python version | Native version |
| --- | --- | --- |
| `v1.2.3` | `1.2.3` | `1.2.3` |
| `v1.2.3rc1` | `1.2.3rc1` | `1.2.3-rc.1` |
| Untagged or tracked-dirty checkout | SCM development identity | Explicit development identity, not a stable release |

Cargo's private crate version is not the application version. The native executable
reports `Wenyi Desktop <version>` with `--version` without starting its UI.

An explicit `WENYI_BUILD_TAG` must exist, point at HEAD, match the Python metadata
and have no tracked edits. Untracked notes do not change SCM dirty status. Version
overrides are rejected. Release tags support numeric versions and `a`/`b`/`rc`
prereleases; epochs, post releases, local/dev tags, more than three release components
and values beyond `255.255.65535` fail rather than losing identity. macOS's numeric
bundle version uses the release triple while the app version retains qualifiers.

Distribution and integrated updates target stable releases. Development/prerelease
identities remain useful for diagnostics, but Linux package-manager ordering is not
normalized into a supported prerelease update channel.

## Build on the target platform

Use the prerequisites and dependency setup in [Desktop development](desktop.md).
Build the native lock library before freezing the engine:

```bash
cargo build --release --manifest-path native/wenyi-locks/Cargo.toml
pnpm desktop:build
```

For a clean checkout of a release tag, use:

```bash
WENYI_BUILD_TAG=v1.2.3 pnpm desktop:build
```

In PowerShell, set `$env:WENYI_BUILD_TAG = "v1.2.3"` before the build command.
Use a real tag matching the checkout, not an invented version override.

The [build launcher](../../scripts/dev/desktop.mjs#L55) builds the frontend, freezes
the engine, then invokes Tauri with the resolved version. The
[sidecar builder](../../scripts/dev/desktop_sidecar.py#L227) creates a temporary,
Desktop-only Python environment and produces a PyInstaller **onedir** engine:

- Rebuild local Core/backend/Desktop packages rather than reuse cached local wheels;
  verify installed metadata against the Git-derived Python version.
- Include prompt/i18n resources, provider modules, tokenizer vocabulary, credential
  backends and the platform's `wenyi_locks` library. A normal Python wheel install
  does not carry that native library and can use the compatible Python lock backend.
- Exclude the CLI, Web API, PostgreSQL, Redis and Arq runtime packages. Keep BabelDOC
  as a separately downloaded, process-isolated AGPL plugin, not part of the sidecar.
- Smoke-test outside the repository with a temporary workspace **and user profile**,
  no development Python path and no external service access. Exercise authenticated
  offline TXT parse/preview, the ready/diagnostic protocol, graceful shutdown and a
  real isolated startup-lock failure.

Onedir avoids unpacking the whole Python runtime on every launch. Distribute the
native bundle with its engine/resources, never the bare Rust executable.
Windows release host/engine launches do not create console windows, while source
debug launches retain their development console and private protocol pipes.

Build macOS Apple Silicon and Intel packages separately using matching native Python
and Rust toolchains. Cross-compiling only the Rust host does not rebuild the sidecar
for the other architecture. A successful build on one OS is not validation of another.

## Artifacts and signing

Desktop release assets use `wenyi-desktop-<version>-<platform>-<arch>.<extension>`.
The platform/architecture suffixes and complete format sets are:

| Target | Install/download artifacts | Signed updater payload |
| --- | --- | --- |
| `linux-x64` | `.AppImage`, `.deb`, `.rpm` | `.AppImage` and its `.sig` |
| `windows-x64` | NSIS `.exe` | `.exe` and its `.sig` |
| `macos-arm64` | `.dmg` | `.app.tar.gz` and its `.sig` |
| `macos-x64` | `.dmg` | `.app.tar.gz` and its `.sig` |

Windows uses NSIS, not MSI, to preserve prerelease/development identity. Installers
are uploaded directly without outer ZIP wrappers. GitHub's workflow artifact
download may add its own wrapper; that is not the Release asset format.

[Installer collection](../../scripts/release/package_release.py#L1) requires the
exact version, architecture and complete platform format set. Old output is not
renamed as a new version. Desktop checksums are `wenyi-desktop-SHA256SUMS.txt`;
CLI has separate `wenyi-cli-<version>-<platform>-<arch>.zip` files and
`wenyi-cli-SHA256SUMS.txt`. CLI ZIP entries preserve Unix executable permissions.

**Updater signing is separate from OS code signing/notarization.** Current packaging
workflows do not perform Windows/macOS OS signing or macOS notarization. Those
requirements and real end-user installation checks remain release responsibilities.

### Updater signing setup

Generate Tauri signing keys in a protected location **outside the checkout**:

```bash
pnpm exec tauri signer generate --write-keys /secure/path/outside-checkout/wenyi-updater.key
```

Use the interactive password prompt; do not put real passwords in shell history.
Keep the private key/password in secure backup, never in source, issues, logs or
PR workflows. Preserve the key across releases: installed builds trust the embedded
public key, so rotation needs a planned trust migration or manual upgrade.

Configure GitHub Actions repository entries:

| Entry | Kind | Value |
| --- | --- | --- |
| `WENYI_UPDATER_PUBLIC_KEY` | Variable | Full generated `.pub` contents, not a path |
| `TAURI_SIGNING_PRIVATE_KEY` | Secret | Full private-key contents |
| `TAURI_SIGNING_PRIVATE_KEY_PASSWORD` | Secret | Password, or empty for an intentionally passwordless key |

For a local signed build, provide all three environment variables through a secure
environment/secret manager, then build the matching clean stable tag. The password
variable must be defined locally even when empty. On macOS, use
`pnpm desktop:build --bundles dmg,app` to include the updater archive.

The launcher enables `bundle.createUpdaterArtifacts` only for configured stable
signed builds. Partial configuration, a malformed public key or public-key path,
and signing development/prerelease identities fail explicitly. Ordinary Cargo tests,
source runs and unsigned builds do not require signing secrets.

## Publication workflow

The [Desktop packages workflow](../../.github/workflows/desktop.yml#L1) and
[CLI packages workflow](../../.github/workflows/build.yml#L1) build the selected
checkout or optional `workflow_dispatch` tag. Manual runs produce workflow artifacts,
not published releases. A published GitHub Release builds its tag and each workflow
uploads only its component's assets. PyPI publication has its own workflow and
[publishing contract](publishing.md).

Desktop PR checks use reduced platform/build tiers; a passing PR is not the full
release packaging matrix. Published releases and manual Desktop builds exercise the
full Linux x64, Windows x64 and two native macOS targets. Push-to-main Desktop work
warms build caches rather than publishing installers.

Signing secrets are supplied only to stable release builds. PRs, manual workflow
runs and development/prerelease builds remain unsigned. With no signing configuration,
a stable release still produces manual installers and explicitly publishes **no
`latest.json`**. Partial signing configuration fails rather than advertising updates
that clients cannot install.

The update repository is a build-time constant:
[`WENYI_UPDATE_REPO`](../../apps/desktop/build.rs#L1), defaulting to
`BigDawnGhost/wenyi`, must be exactly `owner/repository`. The release workflow sets
it to `github.repository`, so a fork using that workflow updates from itself. The
same repository controls manifest lookup and trusted artifact URLs. No separate
update service is needed: builds read
`https://github.com/<repository>/releases/latest/download/latest.json`.

For signed stable publication:

1. Build and collect every installer, updater payload and signature.
2. [Validate updater assets](../../scripts/release/desktop_updates.py#L126):
   require stable SemVer, version-bound signatures, matching configured public-key
   IDs and the complete target set (`windows-x86_64`, `linux-x86_64`,
   `darwin-aarch64`, `darwin-x86_64`).
3. Upload payloads, signatures and component checksums successfully.
4. Generate the manifest with actual renamed Release URLs, signature contents and
   release notes, then upload **`latest.json` last**.

Uploads do not overwrite existing attachments; retrying an existing name fails.
Missing/malformed/mismatched signatures must not yield a partial manifest.
Publication checks of signature structure/key identity are not cryptographic
verification. The native updater verifies the downloaded artifact and its signed
version before installation; keep `requireSignedVersion` enabled so an older signed
artifact cannot be relabeled as a newer one.

Integrated installation is supported for configured stable Windows NSIS, macOS
Apple Silicon/Intel and Linux AppImage builds. macOS retains the installed
architecture. Linux `.deb`/`.rpm` use package installation, not AppImage replacement.
Checks do not silently download, force installation or restart an app; only
configured stable supported builds check automatically at startup.

## In-place upgrade safety

The [native install path](../../apps/desktop/src/updates.rs#L635),
[backend admission guard](../../apps/desktop/backend/wenyi_desktop/updates.py#L26)
and [data-root leases](../../apps/desktop/src/profile.rs#L153) form one safety boundary.
Do not reduce it to “no running job rows.”

1. The frontend requires explicit confirmation and resolved unsaved editor/settings
   changes. Native save admission includes pending dialogs and active copies.
2. Acquire exclusive access through the selected data root's `locks/`, excluding
   other native/standalone engines and surviving writer leases for that root,
   not merely other windows. Other copies of the same executable must also exit
   before its program files can be replaced, even if they use different roots.
3. Close mutation admission, including streamed requests. Refuse active plugin
   operations, queued/running jobs and runtime-owned pending/draining work.
4. Resolve an exact backup plan from the live catalog while writes are frozen:
   `<data-root>/catalog.sqlite3` and every registered project directory, including
   explicitly chosen external libraries. Do not discover additional legacy locations.
5. Stop/reap the engine and verify writer quiescence. Copy and verify the backup
   before invoking the installer; retain the exclusive lease through install/recovery.

[Upgrade snapshots](../../apps/desktop/src/migration/backup.rs#L114) are written under
`<data-root>/backups/<version>-<timestamp>/`. Copy the catalog and selected project
directories, not the entire root, user-selected parent folders or earlier backups.
Include existing catalog WAL/SHM files after shutdown and preserve project database
sidecars; forced process exit does not prove checkpointing. The manifest records
source paths, file sizes and SHA-256, and is written only after the copy succeeds.
Credentials remain references, not exported vault/session secrets. Plugin runtime
and PDF sessions require the separate [manual backup](../guide/desktop.md#data-locations-and-backups).

Missing sources, unsafe links, insufficient space or copy/verification failure abort
before installer execution. Recovery attempts to reopen the existing engine; this
is not a promise of automatic restoration from backup after every installation failure.
If cancellation of engine update preparation is not acknowledged, keep an explicit
unavailable/error state rather than resuming writes speculatively.

### Windows installer safety

Keep [Windows bundle configuration](../../apps/desktop/tauri.windows.conf.json#L1)
connected to the [NSIS template](../../apps/desktop/installer.nsi#L106) and
[directory-admission hooks](../../apps/desktop/installer.nsh#L140):

- `installMode` stays `currentUser`, and elevated launches are refused. Program and
  data defaults are separate, as listed in the [Desktop guide](../guide/desktop.md).
  Keep both English and Simplified Chinese installer UI available.
- Setup and uninstall share a mutex. Check both the native executable and
  `sidecar/wenyi-engine/wenyi-engine.exe` for occupancy before replacement/removal;
  never force-kill translations. Interactive, silent and passive modes must all
  refuse occupied files and downgrades. Project SQLite version refusal is a
  separate guard, not permission for an older installer to proceed.
- Before payload writes, reject drive/share/user roots, Windows/Program Files,
  data or legacy-location overlap, linked ancestors and unwritable targets.
  A new or different target must be empty. Only the registered current-layout
  in-place upgrade may preserve unrelated files in an occupied program directory.
- Runtime layout checks use the actual executable directory, including source,
  unpacked and explicit `--data-dir` launches. Resolve existing ancestors of missing
  destinations so links and Windows short paths cannot conceal overlap. Apply the
  same rule to exact-source backup planning to prevent copying a source into itself.
- A manual installer does not create a snapshot or require a historical
  `MANIFEST.json`. The in-app updater still creates and verifies its snapshot
  before launching `/UPDATE`; these are distinct guarantees.
- Use active Add/Remove Programs registration, not a remembered location alone.
  Replace an old registered program through its recorded uninstaller and known
  program files; do not recursively remove a legacy mixed program/data directory
  or migrate its data.
- Ordinary uninstall offers two independent, initially unchecked options: remove
  catalog/settings/logs/locks/runtime, or remove default projects and upgrade backups.
  They affect only the default Windows data root. Custom roots, external libraries
  and legacy directories remain untouched; recursive cleanup must not follow links.
  Removing the catalog loses the project list even if project files remain.
  Silent/passive uninstall and upgrade replacement must not select data cleanup.

Run acceptance as a standard user on Windows itself: fresh install, same-version
reinstall, upgrade, downgrade/elevation refusal, occupied native/engine files,
concurrent setup/uninstall, each data-deletion choice, and directory/link aliases
including not-yet-created targets. Cross-compilation and browser IPC mocks do not
validate these installer behaviors.

## Release notes

The Desktop [language selector](../../apps/desktop/frontend/src/localizedReleaseNotes.ts#L5)
and [read-only renderer](../../apps/desktop/frontend/src/ReleaseNotes.tsx#L36) consume
the GitHub Release body. Keep this format stable:

1. Start directly with `## English`; the Release title is separate.
2. Write a short overview and applicable `###` categories; use `####` for deeper
   headings. Do not place another level-one/two heading or a horizontal divider
   inside a language section.
3. Put a standalone `---` between languages, followed by exactly `## 中文` and
   equivalent Chinese content.
4. A shared comparison footer, if included, belongs after a final standalone `---`.

Recommended category order, omitting empty categories:

| English | 简体中文 |
| --- | --- |
| New features | 新增功能 |
| Improvements | 改进 |
| Fixes | 问题修复 |
| Development and documentation | 开发与文档 |
| Upgrade notes | 升级说明 |

The parser recognizes `English`/`en` and `中文`/`简体中文`/`zh-CN` case-insensitively.
A heading of equal or higher depth or a horizontal divider ends the section;
language-like text in code blocks does not. Missing/unmarked language content
produces a localized empty message, never the other language.

Markdown headings, lists, emphasis, code, quotes and GFM tables/task lists are
rendered. Raw HTML is ignored, and links/images render as text without external
navigation or remote-image requests. Prefer portable text-only Markdown; do not make
an upgrade instruction depend on a clickable link or image inside the app.

Draft notes from the verified net changes since the previous stable release on the
target's history, not a commit dump or unrelated “latest” tag. State platform scope,
real upgrade actions and caveats; do not claim signing, test execution, performance
or quality improvements without evidence. Keep languages equivalent and process
context outside the copyable release body. The repository
[release-notes skill](../../.agents/skills/release-notes/SKILL.md) supplies the
detailed drafting workflow; drafting does not authorize publishing.

## Release validation

Use the workflow commands as the executable source of truth. Validate version
agreement across Python metadata, native `--version`, installer names and updater
signatures. Exercise sidecar resource/offline smoke checks, native lock contracts,
affected Python/Rust tests, both frontend builds/boundaries and relevant E2E.

Packaged acceptance must use temporary data on each target: first launch without
system Python, native saves/drag-drop, tray and second-instance activation,
graceful/forced engine cleanup, diagnostic export without a working engine,
update admission, verified backup and installer recovery/refusal. Windows also
requires GUI-subsystem/no-console verification; macOS needs both architectures;
Linux AppImage and package-manager installations have different update paths.

Record what actually ran, which signing/notarization steps were performed and which
native platforms remain untested. Browser IPC mocks, a source debug run or a Linux
build alone do not establish release readiness for every platform.
