//! Signed whole-application updates, owned by the native host rather than a page.
use std::{
    io::Read,
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    time::Duration,
};

use semver::Version;
use serde::Serialize;
use tauri::Manager;
use tauri_plugin_updater::{Update, UpdaterExt};

use crate::{
    ipc_error::IpcError, local_connection::Connection, migration, mutex::lock,
    native_export::Saves, paths, Engine, State,
};

/// The repository the updater publishes to and reads from.
///
/// Resolved by `build.rs` from `WENYI_UPDATE_REPO`, defaulting to this project.
/// It is one constant rather than two literals because the URL that is fetched
/// and the URL prefix that is trusted have to name the same repository: a build
/// that changed one and not the other would download releases it then refused.
const REPO: &str = env!("WENYI_UPDATE_REPO");

/// Where the signed release manifest lives.
fn manifest_url() -> String {
    format!("https://github.com/{REPO}/releases/latest/download/latest.json")
}

/// The GitHub API endpoint naming the same release.
fn release_api_url() -> String {
    format!("https://api.github.com/repos/{REPO}/releases/latest")
}
/// Layer-two directory that keeps one verified copy per completed upgrade.
///
/// Layer two and not layer one: the copy has to outlive the application, and
/// layer one is the directory that is rebuilt on every launch.
#[cfg(test)]
const BACKUP: &str = "backups";
type Result<T> = std::result::Result<T, IpcError>;

#[derive(Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
enum Mode {
    Automatic,
    Manual,
    Unconfigured,
}

#[derive(Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
enum Phase {
    Idle,
    Checking,
    Current,
    Available,
    Downloading,
    Ready,
    Installing,
    Error,
}

impl Phase {
    fn busy(self) -> bool {
        matches!(self, Self::Checking | Self::Downloading | Self::Installing)
    }
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Snapshot {
    current_version: String,
    mode: Mode,
    phase: Phase,
    #[serde(skip_serializing_if = "Option::is_none")]
    version: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    notes: Option<String>,
    downloaded: u64,
    #[serde(skip_serializing_if = "Option::is_none")]
    total: Option<u64>,
    // The page's own failure vocabulary, not an IPC code: this is the state
    // the status command reports, and its shape must not change.
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<&'static str>,
}

struct UpdateState {
    snapshot: Snapshot,
    update: Option<Update>,
    // Keep verified bytes in memory, not the workspace or a stale on-disk cache.
    bytes: Option<Arc<Vec<u8>>>,
}

pub struct Updates(Mutex<UpdateState>);

fn stable(version: &Version) -> bool {
    version.pre.is_empty() && version.build.is_empty()
}

fn mode(version: &str, key: &str, debug: bool, os: &str, arch: &str, appimage: bool) -> Mode {
    if debug || key.trim().is_empty() || !Version::parse(version).is_ok_and(|v| stable(&v)) {
        return Mode::Unconfigured;
    }
    match (os, arch) {
        ("windows", "x86_64") | ("macos", "aarch64" | "x86_64") => Mode::Automatic,
        ("linux", "x86_64") if appimage => Mode::Automatic,
        _ => Mode::Manual,
    }
}

impl Updates {
    pub fn new() -> Self {
        Self(Mutex::new(UpdateState {
            snapshot: Snapshot {
                current_version: env!("WENYI_DESKTOP_VERSION").into(),
                mode: mode(
                    env!("WENYI_DESKTOP_VERSION"),
                    env!("WENYI_UPDATER_PUBLIC_KEY"),
                    cfg!(debug_assertions),
                    std::env::consts::OS,
                    std::env::consts::ARCH,
                    std::env::var_os("APPIMAGE").is_some_and(|p| !p.is_empty()),
                ),
                phase: Phase::Idle,
                version: None,
                notes: None,
                downloaded: 0,
                total: None,
                error: None,
            },
            update: None,
            bytes: None,
        }))
    }
}

fn trusted(window: &tauri::WebviewWindow) -> Result<()> {
    if window.label() == "main" && window.url().is_ok_and(|url| crate::trusted(&url)) {
        Ok(())
    } else {
        Err(IpcError::new(
            "update_unavailable",
            "Updates are not available right now.",
        ))
    }
}

#[tauri::command]
pub fn desktop_update_status(window: tauri::WebviewWindow) -> Result<Snapshot> {
    trusted(&window)?;
    Ok(lock(&window.state::<Updates>().0).snapshot.clone())
}

fn release_metadata(
    value: &serde_json::Value,
    current: &Version,
) -> Result<Option<(String, String)>> {
    if value["draft"].as_bool() != Some(false) || value["prerelease"].as_bool() != Some(false) {
        return Err(IpcError::new(
            "update_check_failed",
            "Could not check for updates.",
        ));
    }
    let tag = value["tag_name"].as_str().ok_or(IpcError::new(
        "update_check_failed",
        "Could not check for updates.",
    ))?;
    let mut parts: Vec<&str> = tag.strip_prefix('v').unwrap_or(tag).split('.').collect();
    if !(1..=3).contains(&parts.len())
        || parts
            .iter()
            .any(|p| p.is_empty() || !p.bytes().all(|b| b.is_ascii_digit()))
    {
        return Err(IpcError::new(
            "update_check_failed",
            "Could not check for updates.",
        ));
    }
    parts.resize(3, "0");
    let version = Version::parse(&parts.join("."))
        .map_err(|_| IpcError::new("update_check_failed", "Could not check for updates."))?;
    if !stable(&version) {
        return Err(IpcError::new(
            "update_check_failed",
            "Could not check for updates.",
        ));
    }
    Ok((version > *current).then(|| {
        (
            version.to_string(),
            value["body"]
                .as_str()
                .unwrap_or_default()
                .chars()
                .take(100_000)
                .collect(),
        )
    }))
}

fn manual_check() -> Result<Option<(String, String)>> {
    let client = reqwest::blocking::Client::builder()
        .user_agent(concat!("Wenyi Desktop/", env!("WENYI_DESKTOP_VERSION")))
        // The release asset list is checked against a fixed host afterwards, so a
        // redirect must not be followed first: it would move the check elsewhere.
        .redirect(reqwest::redirect::Policy::none())
        .connect_timeout(Duration::from_secs(10))
        .timeout(Duration::from_secs(30))
        .build()
        .map_err(|_| IpcError::new("update_check_failed", "Could not check for updates."))?;
    let response = client
        .get(release_api_url())
        .send()
        .map_err(|_| IpcError::new("update_check_failed", "Could not check for updates."))?;
    if !response.status().is_success() {
        return Err(IpcError::new(
            "update_check_failed",
            "Could not check for updates.",
        ));
    }
    let mut bytes = Vec::new();
    response
        .take(1024 * 1024 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| IpcError::new("update_check_failed", "Could not check for updates."))?;
    if bytes.len() > 1024 * 1024 {
        return Err(IpcError::new(
            "update_check_failed",
            "Could not check for updates.",
        ));
    }
    let value = serde_json::from_slice(&bytes)
        .map_err(|_| IpcError::new("update_check_failed", "Could not check for updates."))?;
    release_metadata(
        &value,
        &Version::parse(env!("WENYI_DESKTOP_VERSION")).unwrap(),
    )
}

/// Whether a download link belongs to the repository this build was made for.
///
/// The prefix names the same constant the manifest URL is built from, so a
/// build cannot be configured to watch one repository and accept assets from
/// another. Requiring the separator after the repository name is what keeps a
/// repository called `wenyi-extra` from matching `wenyi`.
fn trusted_artifact(url: &reqwest::Url) -> bool {
    url.scheme() == "https"
        && url.host_str() == Some("github.com")
        && url.port().is_none()
        && url.username().is_empty()
        && url.password().is_none()
        && url.query().is_none()
        && url.fragment().is_none()
        && url
            .path()
            .starts_with(&format!("/{REPO}/releases/download/"))
}

fn begin_check(state: &mut UpdateState) -> Result<()> {
    if state.snapshot.phase.busy() {
        return Err(IpcError::new(
            "update_busy",
            "Wenyi is busy. Try again when the current task finishes.",
        ));
    }
    state.update = None;
    state.bytes = None;
    state.snapshot.phase = Phase::Checking;
    state.snapshot.version = None;
    state.snapshot.notes = None;
    state.snapshot.downloaded = 0;
    state.snapshot.total = None;
    state.snapshot.error = None;
    Ok(())
}

fn start_check(app: &tauri::AppHandle) -> Result<()> {
    let automatic = {
        let updates = app.state::<Updates>();
        let mut state = lock(&updates.0);
        begin_check(&mut state)?;
        state.snapshot.mode == Mode::Automatic
    };
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let checked: Result<(Option<(String, String)>, Option<Update>)> = if automatic {
            let result = async {
                let manifest = manifest_url().parse().unwrap();
                let updater = app
                    .updater_builder()
                    .endpoints(vec![manifest])
                    .map_err(|_| {
                        IpcError::new("update_check_failed", "Could not check for updates.")
                    })?
                    .timeout(Duration::from_secs(30))
                    .version_comparator(|current, remote| {
                        stable(&remote.version) && remote.version > current
                    })
                    // Keep the window alive if Windows fails to launch the installer.
                    // The engine and native exports are already drained by our host.
                    .on_before_exit(|| {})
                    .build()
                    .map_err(|_| {
                        IpcError::new("update_check_failed", "Could not check for updates.")
                    })?;
                let update = updater.check().await.map_err(|_| {
                    IpcError::new("update_check_failed", "Could not check for updates.")
                })?;
                if let Some(update) = &update {
                    if !trusted_artifact(&update.download_url) {
                        return Err(IpcError::new(
                            "update_check_failed",
                            "Could not check for updates.",
                        ));
                    }
                }
                let release = update
                    .as_ref()
                    .map(|u| (u.version.clone(), u.body.clone().unwrap_or_default()));
                Ok((release, update))
            };
            result.await
        } else {
            tauri::async_runtime::spawn_blocking(manual_check)
                .await
                .unwrap_or_else(|_| {
                    Err(IpcError::new(
                        "update_check_failed",
                        "Could not check for updates.",
                    ))
                })
                .map(|release| (release, None))
        };
        let updates = app.state::<Updates>();
        let mut state = lock(&updates.0);
        match checked {
            Ok((release, update)) => {
                state.update = update;
                state.snapshot.phase = if release.is_some() {
                    Phase::Available
                } else {
                    Phase::Current
                };
                if let Some((version, notes)) = release {
                    state.snapshot.version = Some(version);
                    state.snapshot.notes = Some(notes);
                }
            }
            Err(_) => {
                app.state::<crate::diagnostics::Diagnostics>().record(
                    crate::diagnostics::Source::Native,
                    crate::diagnostics::Event::UpdateFailed,
                    serde_json::json!({}),
                );
                state.snapshot.phase = Phase::Error;
                state.snapshot.error = Some("check_failed");
            }
        }
    });
    Ok(())
}

pub fn check_on_startup(app: &tauri::AppHandle) {
    if lock(&app.state::<Updates>().0).snapshot.mode == Mode::Automatic {
        let _ = start_check(app);
    }
}

#[tauri::command]
pub fn desktop_update_check(window: tauri::WebviewWindow) -> Result<()> {
    trusted(&window)?;
    start_check(window.app_handle())
}

#[tauri::command]
pub fn desktop_update_download(window: tauri::WebviewWindow) -> Result<()> {
    trusted(&window)?;
    let mut update = {
        let updates = window.state::<Updates>();
        let mut state = lock(&updates.0);
        if state.snapshot.phase.busy() {
            return Err(IpcError::new(
                "update_busy",
                "Wenyi is busy. Try again when the current task finishes.",
            ));
        }
        if state.snapshot.mode != Mode::Automatic || state.snapshot.phase != Phase::Available {
            return Err(IpcError::new(
                "update_unavailable",
                "Updates are not available right now.",
            ));
        }
        let update = state.update.clone().ok_or(IpcError::new(
            "update_unavailable",
            "Updates are not available right now.",
        ))?;
        state.snapshot.phase = Phase::Downloading;
        state.snapshot.error = None;
        state.snapshot.downloaded = 0;
        state.snapshot.total = None;
        update
    };
    update.timeout = Some(Duration::from_secs(20 * 60));
    let app = window.app_handle().clone();
    tauri::async_runtime::spawn(async move {
        let progress = app.clone();
        let downloaded = update
            .download(
                move |chunk, total| {
                    let updates = progress.state::<Updates>();
                    let mut state = lock(&updates.0);
                    state.snapshot.downloaded += chunk as u64;
                    state.snapshot.total = total;
                },
                || {},
            )
            .await;
        let updates = app.state::<Updates>();
        let mut state = lock(&updates.0);
        match downloaded {
            Ok(bytes) => {
                state.bytes = Some(Arc::new(bytes));
                state.snapshot.phase = Phase::Ready;
            }
            Err(_) => {
                app.state::<crate::diagnostics::Diagnostics>().record(
                    crate::diagnostics::Source::Native,
                    crate::diagnostics::Event::UpdateFailed,
                    serde_json::json!({}),
                );
                state.snapshot.phase = Phase::Available;
                state.snapshot.error = Some("download_failed");
            }
        }
    });
    Ok(())
}

fn prepare_engine(connection: &Connection) -> Result<()> {
    let response = connection
        .client
        .post(format!("{}/desktop/updates/prepare", connection.base))
        .bearer_auth(&connection.token)
        .send()
        .map_err(|_| IpcError::new("update_unavailable", "Updates are not available right now."))?;
    match response.status().as_u16() {
        204 => Ok(()),
        409 => Err(IpcError::new(
            "update_busy",
            "Wenyi is busy. Try again when the current task finishes.",
        )),
        _ => Err(IpcError::new(
            "update_unavailable",
            "Updates are not available right now.",
        )),
    }
}

const CANCELLATION_UNCERTAIN: &str =
    "Wenyi could not confirm that update preparation was cancelled. Quit and reopen Wenyi before continuing.";

fn cancel_prepare(connection: &Connection) -> Result<()> {
    let response = connection
        .client
        .post(format!("{}/desktop/updates/cancel", connection.base))
        .bearer_auth(&connection.token)
        .send()
        .map_err(|_| IpcError::new("update_unavailable", CANCELLATION_UNCERTAIN))?;
    if response.status().as_u16() != 204 {
        return Err(IpcError::new("update_unavailable", CANCELLATION_UNCERTAIN));
    }
    Ok(())
}

fn unavailable(state: &Mutex<State>, closing: &AtomicBool, reason: &'static str) {
    let mut state = lock(state);
    state.connection = None;
    state.error = Some(reason);
    state.closing = false;
    // Keep native save admission closed, but allow an explicit Quit to proceed.
    closing.store(false, Ordering::SeqCst);
}

fn cancel_preparation(
    connection: &Connection,
    state: &Mutex<State>,
    saves: &Saves,
    closing: &AtomicBool,
) -> Result<()> {
    if let Err(error) = cancel_prepare(connection) {
        unavailable(state, closing, CANCELLATION_UNCERTAIN);
        return Err(error);
    }
    lock(state).closing = false;
    saves.reopen();
    closing.store(false, Ordering::SeqCst);
    Ok(())
}

fn stop_engine(engine: &Engine) -> std::result::Result<(), &'static str> {
    let mut owner = lock(&engine.child);
    if let Some(backend) = owner.as_mut() {
        backend.shutdown();
        if !backend.exited() {
            return Err(
                "The local engine has not stopped. Quit and reopen Wenyi before continuing.",
            );
        }
    }
    // Never discard a Child handle before its exit/reap has been observed.
    owner.take();
    Ok(())
}

#[derive(serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct BackupPlan {
    schema_version: u32,
    sources: Vec<migration::BackupSource>,
}

fn backup_plan(connection: &Connection) -> Result<BackupPlan> {
    let failure = || {
        IpcError::new(
            "update_backup_failed",
            "Wenyi could not locate all project data. Reconnect missing project folders and retry.",
        )
    };
    let response = connection
        .client
        .post(format!("{}/desktop/updates/backup-plan", connection.base))
        .bearer_auth(&connection.token)
        .send()
        .map_err(|_| failure())?
        .error_for_status()
        .map_err(|_| failure())?;
    let plan: BackupPlan = response.json().map_err(|_| failure())?;
    if plan.schema_version != 1 || plan.sources.is_empty() {
        return Err(failure());
    }
    Ok(plan)
}

/// Copy the frozen owned-source plan and prove it, or explain the refusal.
///
/// Called once the engine has exited and before the installer runs. Both ends of
/// that window matter: a live engine keeps a write-ahead log moving underneath
/// the copy, so the bytes read would be a mixture of two states, and an installer
/// that started first has already replaced the files the copy is meant to preserve.
///
/// The backup is a sibling of projects/runtime, not a copy of the entire root.
fn snapshot(
    plan: &BackupPlan,
    workspace: &std::path::Path,
) -> std::result::Result<PathBuf, String> {
    let target = paths::backups(workspace);
    let sources = stopped_sources(plan)?;
    let snapshot = migration::snapshot_sources(&sources, &target, env!("WENYI_DESKTOP_VERSION"))
        .map_err(|error| error.to_string())?;
    Ok(snapshot.root)
}

fn stopped_sources(plan: &BackupPlan) -> std::result::Result<Vec<migration::BackupSource>, String> {
    let mut sources = plan.sources.clone();
    for catalog in plan
        .sources
        .iter()
        .filter(|source| source.name == "catalog.sqlite3")
    {
        for suffix in ["-wal", "-shm"] {
            let mut path = catalog.path.as_os_str().to_os_string();
            path.push(suffix);
            let path = PathBuf::from(path);
            match std::fs::symlink_metadata(&path) {
                Ok(_) => sources.push(migration::BackupSource {
                    name: format!("catalog.sqlite3{suffix}"),
                    path,
                }),
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
                Err(error) => return Err(error.to_string()),
            }
        }
    }
    Ok(sources)
}

/// The production install gate. Neither a failed copy nor verification can
/// reach the installer; tests exercise this driver with a real multi-root copy.
fn install_prepared(
    backup: impl FnOnce() -> std::result::Result<PathBuf, String>,
    installer: impl FnOnce() -> std::result::Result<(), String>,
) -> std::result::Result<(), (&'static str, String)> {
    backup().map_err(|error| ("update_backup_failed", error))?;
    installer().map_err(|error| ("update_install_failed", error))
}

/// Put the application back into the state it was in before the upgrade attempt.
///
/// The engine is already stopped by the time any caller reaches this, so the one
/// thing that must happen is starting it again. Credentials are cleared first and
/// the page is not published until the new connection exists: a window that shows
/// the old token after the update failed is a window talking to a stopped engine.
fn recover(
    app: &tauri::AppHandle,
    engine: &Engine,
    state: &Mutex<State>,
    saves: &Saves,
    code: &str,
    reason: String,
) -> IpcError {
    // Preserve the verified download for a retry, but restore the engine first.
    lock(state).connection = None;
    let recovery = crate::start_backend(app, engine, true);
    {
        let mut state = lock(state);
        state.closing = false;
        if let Err(error) = recovery {
            state.error = Some(error);
        }
    }
    if recovery.is_ok() {
        saves.reopen();
    }
    engine.closing.store(false, Ordering::SeqCst);
    crate::publish(app, state);
    IpcError::new(code, reason)
}

fn install(app: &tauri::AppHandle, update: Update, bytes: Arc<Vec<u8>>) -> Result<()> {
    let engine = app.state::<Engine>();
    let state = app.state::<Arc<Mutex<State>>>();
    let raw = {
        let state = lock(&state);
        if state.closing || state.error.is_some() {
            return Err(IpcError::new(
                "update_unavailable",
                "Updates are not available right now.",
            ));
        }
        state.connection.clone().ok_or(IpcError::new(
            "update_unavailable",
            "Updates are not available right now.",
        ))?
    };
    let connection = Connection::new(&raw)
        .map_err(|_| IpcError::new("update_unavailable", "Updates are not available right now."))?;
    // Exclude every other native/standalone engine sharing this data root before
    // freezing the plan, and retain the lease until installation or recovery.
    let owned_writer = lock(&engine.child)
        .as_ref()
        .filter(|backend| backend.is_ready())
        .map(|backend| engine.native_owner.writer_name(backend.id()));
    let profile = engine
        .profile
        .exclusive(owned_writer.as_deref())
        .map_err(|error| IpcError::new("update_busy", error))?;
    if engine.closing.swap(true, Ordering::SeqCst) {
        return Err(IpcError::new(
            "update_busy",
            "Wenyi is busy. Try again when the current task finishes.",
        ));
    }
    let saves = app.state::<Saves>();
    if !saves.close_if_idle() {
        engine.closing.store(false, Ordering::SeqCst);
        return Err(IpcError::new(
            "update_busy",
            "Wenyi is busy. Try again when the current task finishes.",
        ));
    }
    lock(&state).closing = true;
    crate::publish(app, &state);
    let prepared = prepare_engine(&connection).and_then(|()| backup_plan(&connection));
    let plan = match prepared {
        Ok(plan) => plan,
        Err(error) => {
            // A timed-out response may still have closed admission on the engine.
            let cancellation = cancel_preparation(&connection, &state, &saves, &engine.closing);
            crate::publish(app, &state);
            return Err(cancellation.err().unwrap_or(error));
        }
    };
    app.state::<crate::native_drop::Grants>().clear();
    if let Err(error) = stop_engine(&engine) {
        unavailable(&state, &engine.closing, error);
        crate::publish(app, &state);
        return Err(IpcError::new("update_unavailable", error));
    }
    if let Err(error) = profile.quiescent() {
        drop(profile);
        return Err(recover(
            app,
            &engine,
            &state,
            &saves,
            "update_backup_failed",
            error,
        ));
    }
    // The engine is down and the profile lease excludes other writers. A forced
    // exit can leave committed WAL data, so every companion is still copied.
    //
    // The snapshot runs before the installer starts and aborts the upgrade when
    // it fails. An upgrade that replaces the application without a verified copy
    // of what the old version wrote is the failure this ordering prevents, and
    // the recovery path below is the one an installer that refused to start
    // already used.
    if let Err((code, error)) = install_prepared(
        || {
            snapshot(
                &plan,
                engine
                    .workspace
                    .as_deref()
                    .ok_or("Desktop data root unavailable.")?,
            )
        },
        || {
            update
                .install(bytes.as_slice())
                .map_err(|_| "The update could not be installed.".into())
        },
    ) {
        // A replacement engine registers its own writer lease through admission.
        // Restore our shared lease before spawning it; never bypass the gate.
        drop(profile);
        return Err(recover(app, &engine, &state, &saves, code, error));
    }
    let diagnostics = app.state::<crate::diagnostics::Diagnostics>();
    diagnostics.record(
        crate::diagnostics::Source::Native,
        crate::diagnostics::Event::AppStopped,
        serde_json::json!({}),
    );
    diagnostics.drain();
    engine.closed.store(true, Ordering::SeqCst);
    app.restart();
}

#[tauri::command]
pub async fn desktop_update_install(window: tauri::WebviewWindow) -> Result<()> {
    trusted(&window)?;
    let (update, bytes) = {
        let updates = window.state::<Updates>();
        let mut state = lock(&updates.0);
        if state.snapshot.phase.busy() {
            return Err(IpcError::new(
                "update_busy",
                "Wenyi is busy. Try again when the current task finishes.",
            ));
        }
        if state.snapshot.mode != Mode::Automatic || state.snapshot.phase != Phase::Ready {
            return Err(IpcError::new(
                "update_unavailable",
                "Updates are not available right now.",
            ));
        }
        let update = state.update.clone().ok_or(IpcError::new(
            "update_unavailable",
            "Updates are not available right now.",
        ))?;
        let bytes = state.bytes.clone().ok_or(IpcError::new(
            "update_unavailable",
            "Updates are not available right now.",
        ))?;
        state.snapshot.phase = Phase::Installing;
        state.snapshot.error = None;
        (update, bytes)
    };
    let app = window.app_handle().clone();
    let result = tauri::async_runtime::spawn_blocking(move || {
        let result = install(&app, update, bytes);
        if result.is_err() {
            app.state::<crate::diagnostics::Diagnostics>().record(
                crate::diagnostics::Source::Native,
                crate::diagnostics::Event::UpdateFailed,
                serde_json::json!({}),
            );
            let updates = app.state::<Updates>();
            let mut state = lock(&updates.0);
            state.snapshot.phase = Phase::Ready;
            state.snapshot.error = Some("install_failed");
        }
        result
    })
    .await;
    result.unwrap_or_else(|_| {
        Err(IpcError::new(
            "update_install_failed",
            "The update could not be installed.",
        ))
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::path::Path;

    fn controls(
        replies: Vec<(&'static str, Option<u16>)>,
    ) -> (Connection, std::thread::JoinHandle<()>) {
        use std::io::{Read, Write};
        use std::net::TcpListener;
        use std::time::Duration;
        let listener = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let address = listener.local_addr().unwrap();
        let worker = std::thread::spawn(move || {
            let mut handlers = Vec::new();
            for (path, response) in replies {
                let (mut stream, _) = listener.accept().unwrap();
                handlers.push(std::thread::spawn(move || {
                    stream.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
                    let mut request = Vec::new();
                    while !request.ends_with(b"\r\n\r\n") {
                        let mut byte = [0u8];
                        stream.read_exact(&mut byte).unwrap();
                        request.push(byte[0]);
                    }
                    assert!(String::from_utf8(request).unwrap().starts_with(&format!("POST {path} ")));
                    if let Some(status) = response {
                        write!(stream, "HTTP/1.1 {status} Test\r\nContent-Length: 0\r\nConnection: close\r\n\r\n").unwrap();
                    } else {
                        // Keep the connection open beyond the client's deadline.
                        std::thread::sleep(Duration::from_millis(300));
                    }
                }));
            }
            for handler in handlers {
                handler.join().unwrap();
            }
        });
        let connection = Connection {
            client: reqwest::blocking::Client::builder()
                .no_proxy()
                .timeout(Duration::from_millis(100))
                .build()
                .unwrap(),
            base: format!("http://{address}"),
            token: "offline-test-token".into(),
        };
        (connection, worker)
    }

    #[test]
    fn failed_cancellation_is_not_an_acknowledgement() {
        for reply in [Some(500), Some(200), None] {
            let (connection, worker) = controls(vec![("/desktop/updates/cancel", reply)]);
            let outcome = cancel_prepare(&connection);
            assert!(outcome.is_err(), "cancellation was silently ignored");
            worker.join().unwrap();
        }
    }

    #[test]
    fn preparation_and_plan_errors_reopen_only_after_acknowledged_cancellation() {
        for phase in ["prepare", "backup-plan"] {
            for failure in [Some(500), None] {
                for cancel in [Some(204), Some(500), None] {
                    let mut replies = vec![("/desktop/updates/prepare", Some(204))];
                    if phase == "prepare" {
                        replies[0].1 = failure;
                    } else {
                        replies.push(("/desktop/updates/backup-plan", failure));
                    }
                    replies.push(("/desktop/updates/cancel", cancel));
                    let (connection, worker) = controls(replies);
                    let prepared =
                        prepare_engine(&connection).and_then(|()| backup_plan(&connection));
                    assert!(prepared.is_err());
                    let state = Mutex::new(State {
                        connection: Some("old-connection".into()),
                        closing: true,
                        ..State::default()
                    });
                    let closing = AtomicBool::new(true);
                    let saves = Saves::default();
                    assert!(saves.close_if_idle());
                    let result = cancel_preparation(&connection, &state, &saves, &closing);
                    assert!(!closing.load(Ordering::SeqCst)); // Explicit Quit stays usable.
                    assert!(!lock(&state).closing);
                    if cancel == Some(204) {
                        assert!(result.is_ok());
                        assert!(lock(&state).connection.is_some());
                        assert!(lock(&state).error.is_none());
                        assert!(saves.close_if_idle());
                    } else {
                        assert!(result.is_err());
                        assert!(lock(&state).connection.is_none());
                        assert_eq!(lock(&state).error, Some(CANCELLATION_UNCERTAIN));
                        assert!(
                            !saves.close_if_idle(),
                            "uncertain cancellation reopened saves"
                        );
                    }
                    worker.join().unwrap();
                }
            }
        }
    }

    #[test]
    fn updater_requires_version_bound_signatures_without_guest_permissions() {
        let value: serde_json::Value =
            serde_json::from_str(include_str!("../tauri.conf.json")).unwrap();
        let config: tauri_plugin_updater::Config =
            serde_json::from_value(value["plugins"]["updater"].clone()).unwrap();
        assert!(config.require_signed_version);
        assert_eq!(
            value["app"]["security"]["capabilities"][0]["permissions"],
            serde_json::json!([])
        );
    }

    #[test]
    fn status_matches_frontend_fields_without_null_optional_values() {
        let updates = Updates::new();
        let value = serde_json::to_value(&lock(&updates.0).snapshot).unwrap();
        assert_eq!(value["currentVersion"], env!("WENYI_DESKTOP_VERSION"));
        assert_eq!(value["phase"], "idle");
        assert_eq!(value["downloaded"], 0);
        for field in ["version", "notes", "total", "error"] {
            assert!(value.get(field).is_none());
        }
    }

    #[test]
    fn only_configured_stable_supported_installations_are_automatic() {
        for (os, arch, appimage) in [
            ("windows", "x86_64", false),
            ("macos", "aarch64", false),
            ("macos", "x86_64", false),
            ("linux", "x86_64", true),
        ] {
            assert!(mode("1.2.3", "public", false, os, arch, appimage) == Mode::Automatic);
            for version in ["1.2.3-rc.1", "1.2.3+local", "invalid"] {
                assert!(mode(version, "public", false, os, arch, appimage) == Mode::Unconfigured);
            }
            assert!(mode("1.2.3", "", false, os, arch, appimage) == Mode::Unconfigured);
            assert!(mode("1.2.3", "public", true, os, arch, appimage) == Mode::Unconfigured);
        }
        assert!(mode("1.2.3", "public", false, "linux", "x86_64", false) == Mode::Manual);
        assert!(mode("1.2.3", "public", false, "macos", "unsupported", false) == Mode::Manual);
    }

    #[test]
    fn manual_release_checks_reject_unstable_and_malformed_versions() {
        let current = Version::parse("1.2.3").unwrap();
        let release = |tag: &str| {
            serde_json::json!({
                "tag_name": tag, "draft": false, "prerelease": false, "body": "Notes",
            })
        };
        assert_eq!(
            release_metadata(&release("v1.2.4"), &current).unwrap(),
            Some(("1.2.4".into(), "Notes".into()))
        );
        assert_eq!(
            release_metadata(&release("v2"), &current).unwrap(),
            Some(("2.0.0".into(), "Notes".into()))
        );
        assert_eq!(
            release_metadata(&release("v1.3"), &current).unwrap(),
            Some(("1.3.0".into(), "Notes".into()))
        );
        for tag in ["v1.2.3", "v1.0.0"] {
            assert!(release_metadata(&release(tag), &current).unwrap().is_none());
        }
        for tag in ["v1.2.4rc1", "v1.2.4-rc.1", "v1.2.4+local", "not-a-version"] {
            assert!(release_metadata(&release(tag), &current).is_err());
        }
        let mut draft = release("v1.2.4");
        draft["draft"] = true.into();
        assert!(release_metadata(&draft, &current).is_err());
        draft["draft"] = false.into();
        draft["prerelease"] = true.into();
        assert!(release_metadata(&draft, &current).is_err());
    }

    #[test]
    fn update_download_urls_are_fixed_to_this_repository_over_https() {
        // The accepted URL is derived from the same constant the manifest is, so
        // this states the invariant rather than repeating the repository name:
        // whatever repository this build was made for, its own releases are
        // trusted and nothing else is.
        assert!(trusted_artifact(
            &manifest_url()
                .replace("latest/download/latest.json", "download/v1.2.3/app.exe")
                .parse()
                .unwrap()
        ));
        for url in [
            format!("http://github.com/{REPO}/releases/download/v1/app.exe"),
            "https://github.com/other/repo/releases/download/v1/app.exe".to_owned(),
            format!("https://github.com.evil.example/{REPO}/releases/download/v1/app.exe"),
            format!("https://user@github.com/{REPO}/releases/download/v1/app.exe"),
            format!("https://github.com/{REPO}/releases/download/v1/app.exe?token=secret"),
            // A repository whose name merely starts with this one is another
            // repository, and its releases must not be accepted.
            format!("https://github.com/{REPO}-extra/releases/download/v1/app.exe"),
        ] {
            assert!(!trusted_artifact(&url.parse().unwrap()), "{url}");
        }
    }

    #[test]
    fn the_compiled_repository_is_one_owner_and_one_name() {
        // build.rs rejects a malformed WENYI_UPDATE_REPO, so this asserts the
        // rule the build enforces rather than a second implementation of it.
        // A value with an extra segment would splice into the trusted prefix and
        // silently widen which download links match.
        let (owner, name) = REPO.split_once('/').expect("build.rs accepted a non-slug");
        assert!(!owner.is_empty() && !name.is_empty());
        assert!(!name.contains('/'));
        assert!(
            REPO.bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'-' | b'_' | b'.' | b'/')),
            "{REPO} contains a character a repository name cannot"
        );
    }

    #[test]
    fn both_update_urls_name_the_same_repository() {
        // Two literals that drift apart produce an updater that downloads a
        // release and then refuses to install it. Both are built from one
        // constant, and this is what notices if that stops being true.
        assert!(manifest_url().contains(&format!("/{REPO}/")));
        assert!(release_api_url().ends_with(&format!("/repos/{REPO}/releases/latest")));
        assert!(REPO
            .split_once('/')
            .is_some_and(|(owner, name)| !owner.is_empty()
                && !name.is_empty()
                && !name.contains('/')));
    }

    #[test]
    fn checks_cannot_replace_an_inflight_download_or_install() {
        let updates = Updates::new();
        let mut state = lock(&updates.0);
        for phase in [Phase::Checking, Phase::Downloading, Phase::Installing] {
            state.snapshot.phase = phase;
            state.bytes = Some(Arc::new(vec![1, 2, 3]));
            assert_eq!(
                begin_check(&mut state),
                Err(IpcError::new(
                    "update_busy",
                    "Wenyi is busy. Try again when the current task finishes."
                ))
            );
            assert_eq!(state.bytes.as_ref().unwrap().as_slice(), &[1, 2, 3]);
        }
        state.snapshot.phase = Phase::Ready;
        state.snapshot.error = Some("install_failed");
        begin_check(&mut state).unwrap();
        assert!(state.snapshot.phase == Phase::Checking);
        assert!(state.bytes.is_none());
        assert!(state.snapshot.error.is_none());
    }

    #[test]
    fn backups_and_projects_are_disjoint_children_of_the_actual_data_root() {
        let workspace = Path::new("C:/Users/example/AppData/Local/Wenyi/Desktop");
        let backup = paths::backups(workspace);
        let projects = workspace.join("projects");
        paths::check_disjoint(&[("projects", &projects), ("backup", &backup)]).unwrap();
        assert_eq!(backup, workspace.join(BACKUP));
    }

    #[test]
    fn catalog_companions_are_resolved_after_engine_exit_not_during_preparation() {
        let root = std::env::temp_dir().join(format!(
            "wenyi-catalog-companions-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&root).unwrap();
        let catalog = root.join("catalog.sqlite3");
        let wal = root.join("catalog.sqlite3-wal");
        let plan = BackupPlan {
            schema_version: 1,
            sources: vec![migration::BackupSource {
                name: "catalog.sqlite3".into(),
                path: catalog,
            }],
        };
        assert_eq!(stopped_sources(&plan).unwrap().len(), 1);
        std::fs::write(&wal, b"committed pages left by a stopped engine").unwrap();
        let sources = stopped_sources(&plan).unwrap();
        assert_eq!(sources.len(), 2);
        assert_eq!(sources[1].path, wal);
        std::fs::remove_file(wal).unwrap();
        assert_eq!(stopped_sources(&plan).unwrap().len(), 1);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn a_backup_inside_the_data_directory_is_refused() {
        // The snapshot walks its source, so a target inside it would be copied
        // into itself and every upgrade would duplicate the previous copy.
        let workspace = Path::new("C:/Users/example/Wenyi Desktop");
        let nested = workspace.join("Wenyi").join(BACKUP);
        assert_eq!(
            paths::check_disjoint(&[("data", workspace), ("backup", &nested)]).err(),
            Some(paths::Error::Overlap)
        );
    }

    #[test]
    fn the_snapshot_refuses_an_upgrade_it_cannot_restore() {
        // A refused snapshot has to stop the installer before it runs. The error
        // code is what the page branches on, and it has to stay distinct from a
        // failed launch so the two failures are not reported as one.
        let refusal = IpcError::new(
            "update_backup_failed",
            migration::Error::InsufficientSpace {
                required: 2_000_000_000,
                available: 1_000_000_000,
            }
            .to_string(),
        );
        assert_eq!(refusal.code, "update_backup_failed");
        assert!(refusal.message.contains("cancelled"), "{}", refusal.message);
        assert_ne!(refusal.code, "update_install_failed");
    }

    #[test]
    fn prepared_install_covers_user_and_chosen_projects_before_running_installer() {
        let root = std::env::temp_dir().join(format!(
            "wenyi-update-plan-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let user = root.join("data");
        let project = root.join("chosen/0123456789abcdef-Book");
        std::fs::create_dir_all(user.join("locks/.desktop-writers")).unwrap();
        std::fs::create_dir_all(&project).unwrap();
        std::fs::create_dir_all(user.join(BACKUP)).unwrap();
        for (path, bytes) in [
            (
                user.join("catalog.sqlite3"),
                b"catalog, settings and credential references".as_slice(),
            ),
            (
                user.join("catalog.sqlite3-wal"),
                b"committed WAL".as_slice(),
            ),
            (project.join("state.sqlite3"), b"project".as_slice()),
            (user.join("backups/old-copy"), b"do not recurse".as_slice()),
            (root.join("chosen/private.txt"), b"not a project".as_slice()),
            (
                user.join("locks/.desktop-profile-admission.lock"),
                b"".as_slice(),
            ),
            (
                user.join("locks/.desktop-writers/transient.lock"),
                b"".as_slice(),
            ),
        ] {
            std::fs::write(path, bytes).unwrap();
        }
        let sources = vec![
            migration::BackupSource {
                name: "catalog.sqlite3".into(),
                path: user.join("catalog.sqlite3"),
            },
            migration::BackupSource {
                name: "catalog.sqlite3-wal".into(),
                path: user.join("catalog.sqlite3-wal"),
            },
            migration::BackupSource {
                name: "projects/0".into(),
                path: project.clone(),
            },
        ];
        let mut installed = false;
        let target = user.join(BACKUP);
        install_prepared(
            || {
                migration::snapshot_sources(&sources, &target, "1.4.0")
                    .map(|copy| {
                        let manifest: serde_json::Value = serde_json::from_slice(
                            &std::fs::read(copy.root.join("MANIFEST.json")).unwrap(),
                        )
                        .unwrap();
                        assert_eq!(manifest["sources"].as_array().unwrap().len(), 3);
                        assert_eq!(copy.files.len(), 3);
                        assert!(copy
                            .files
                            .iter()
                            .any(|entry| entry.path == "catalog.sqlite3-wal"));
                        assert!(copy
                            .files
                            .iter()
                            .any(|entry| entry.path == "projects/0/state.sqlite3"));
                        assert!(
                            !copy.files.iter().any(|entry| entry.path.contains("private")
                                || entry.path.contains("old-copy"))
                        );
                        copy.root
                    })
                    .map_err(|error| error.to_string())
            },
            || {
                installed = true;
                Ok(())
            },
        )
        .unwrap();
        assert!(installed);

        // The production install driver must not invoke the installer on an
        // incomplete plan, even if another source (the catalog) was readable.
        std::fs::remove_dir_all(&project).unwrap();
        installed = false;
        let failure = install_prepared(
            || {
                migration::snapshot_sources(&sources, &target, "1.4.0")
                    .map(|copy| copy.root)
                    .map_err(|error| error.to_string())
            },
            || {
                installed = true;
                Ok(())
            },
        )
        .unwrap_err();
        assert_eq!(failure.0, "update_backup_failed");
        assert!(!installed);
        std::fs::remove_dir_all(root).unwrap();
    }
}
