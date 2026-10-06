//! Signed whole-application updates, owned by the native host rather than a page.
use std::{
    io::Read,
    sync::{atomic::Ordering, Arc, Mutex},
    time::Duration,
};

use semver::Version;
use serde::Serialize;
use tauri::Manager;
use tauri_plugin_updater::{Update, UpdaterExt};

use crate::{local_connection::Connection, native_export::Saves, Engine, State};

const RELEASES: &str = "https://github.com/BigDawnGhost/wenyi/releases";
const MANIFEST: &str = "https://github.com/BigDawnGhost/wenyi/releases/latest/download/latest.json";
const RELEASE_API: &str = "https://api.github.com/repos/BigDawnGhost/wenyi/releases/latest";
type Result<T> = std::result::Result<T, &'static str>;

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
        ("windows", "x86_64") | ("macos", "aarch64") => Mode::Automatic,
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
        Err("unavailable")
    }
}

#[tauri::command]
pub fn desktop_update_status(window: tauri::WebviewWindow) -> Result<Snapshot> {
    trusted(&window)?;
    Ok(window.state::<Updates>().0.lock().unwrap().snapshot.clone())
}

fn release_metadata(value: &serde_json::Value, current: &Version) -> Result<Option<(String, String)>> {
    if value["draft"].as_bool() != Some(false) || value["prerelease"].as_bool() != Some(false) {
        return Err("check_failed");
    }
    let tag = value["tag_name"].as_str().ok_or("check_failed")?;
    let mut parts: Vec<&str> = tag.strip_prefix('v').unwrap_or(tag).split('.').collect();
    if !(1..=3).contains(&parts.len())
        || parts
            .iter()
            .any(|p| p.is_empty() || !p.bytes().all(|b| b.is_ascii_digit()))
    {
        return Err("check_failed");
    }
    parts.resize(3, "0");
    let version = Version::parse(&parts.join(".")).map_err(|_| "check_failed")?;
    if !stable(&version) {
        return Err("check_failed");
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
        .connect_timeout(Duration::from_secs(10))
        .timeout(Duration::from_secs(30))
        .build()
        .map_err(|_| "check_failed")?;
    let response = client.get(RELEASE_API).send().map_err(|_| "check_failed")?;
    if !response.status().is_success() {
        return Err("check_failed");
    }
    let mut bytes = Vec::new();
    response
        .take(1024 * 1024 + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| "check_failed")?;
    if bytes.len() > 1024 * 1024 {
        return Err("check_failed");
    }
    let value = serde_json::from_slice(&bytes).map_err(|_| "check_failed")?;
    release_metadata(&value, &Version::parse(env!("WENYI_DESKTOP_VERSION")).unwrap())
}

fn trusted_artifact(url: &reqwest::Url) -> bool {
    url.scheme() == "https"
        && url.host_str() == Some("github.com")
        && url.port().is_none()
        && url.username().is_empty()
        && url.password().is_none()
        && url.query().is_none()
        && url.fragment().is_none()
        && url.path().starts_with("/BigDawnGhost/wenyi/releases/download/")
}

fn begin_check(state: &mut UpdateState) -> Result<()> {
    if state.snapshot.phase.busy() {
        return Err("busy");
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
        let mut state = updates.0.lock().unwrap();
        begin_check(&mut state)?;
        state.snapshot.mode == Mode::Automatic
    };
    let app = app.clone();
    tauri::async_runtime::spawn(async move {
        let checked: Result<(Option<(String, String)>, Option<Update>)> = if automatic {
            let result = async {
                let updater = app
                    .updater_builder()
                    .endpoints(vec![MANIFEST.parse().unwrap()])
                    .map_err(|_| "check_failed")?
                    .timeout(Duration::from_secs(30))
                    .version_comparator(|current, remote| {
                        stable(&remote.version) && remote.version > current
                    })
                    // Keep the window alive if Windows fails to launch the installer.
                    // The engine and native exports are already drained by our host.
                    .on_before_exit(|| {})
                    .build()
                    .map_err(|_| "check_failed")?;
                let update = updater.check().await.map_err(|_| "check_failed")?;
                if let Some(update) = &update {
                    if !trusted_artifact(&update.download_url) {
                        return Err("check_failed");
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
                .unwrap_or(Err("check_failed"))
                .map(|release| (release, None))
        };
        let updates = app.state::<Updates>();
        let mut state = updates.0.lock().unwrap();
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
            Err(error) => {
                state.snapshot.phase = Phase::Error;
                state.snapshot.error = Some(error);
            }
        }
    });
    Ok(())
}

pub fn check_on_startup(app: &tauri::AppHandle) {
    if app.state::<Updates>().0.lock().unwrap().snapshot.mode == Mode::Automatic {
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
        let mut state = updates.0.lock().unwrap();
        if state.snapshot.phase.busy() {
            return Err("busy");
        }
        if state.snapshot.mode != Mode::Automatic || state.snapshot.phase != Phase::Available {
            return Err("unavailable");
        }
        let update = state.update.clone().ok_or("unavailable")?;
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
                    let mut state = updates.0.lock().unwrap();
                    state.snapshot.downloaded += chunk as u64;
                    state.snapshot.total = total;
                },
                || {},
            )
            .await;
        let updates = app.state::<Updates>();
        let mut state = updates.0.lock().unwrap();
        match downloaded {
            Ok(bytes) => {
                state.bytes = Some(Arc::new(bytes));
                state.snapshot.phase = Phase::Ready;
            }
            Err(_) => {
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
        .map_err(|_| "unavailable")?;
    match response.status().as_u16() {
        204 => Ok(()),
        409 => Err("busy"),
        _ => Err("unavailable"),
    }
}

fn cancel_prepare(connection: &Connection) {
    let _ = connection
        .client
        .post(format!("{}/desktop/updates/cancel", connection.base))
        .bearer_auth(&connection.token)
        .send();
}

fn install(app: &tauri::AppHandle, update: Update, bytes: Arc<Vec<u8>>) -> Result<()> {
    let engine = app.state::<Engine>();
    let state = app.state::<Arc<Mutex<State>>>();
    let raw = {
        let state = state.lock().unwrap();
        if state.closing || state.error.is_some() {
            return Err("unavailable");
        }
        state.connection.clone().ok_or("unavailable")?
    };
    let connection = Connection::new(&raw).map_err(|_| "unavailable")?;
    if engine.closing.swap(true, Ordering::SeqCst) {
        return Err("busy");
    }
    let saves = app.state::<Saves>();
    if !saves.close_if_idle() {
        engine.closing.store(false, Ordering::SeqCst);
        return Err("busy");
    }
    state.lock().unwrap().closing = true;
    if let Err(error) = prepare_engine(&connection) {
        // A timed-out response may still have closed admission on the engine.
        cancel_prepare(&connection);
        state.lock().unwrap().closing = false;
        saves.reopen();
        engine.closing.store(false, Ordering::SeqCst);
        crate::publish(app, &state);
        return Err(error);
    }
    app.state::<crate::native_drop::Grants>().clear();
    if let Some(mut backend) = engine.child.lock().unwrap().take() {
        backend.shutdown();
    }
    if update.install(bytes.as_slice()).is_err() {
        // Preserve verified download for a retry, but restore the engine first.
        state.lock().unwrap().closing = false;
        if let Err(error) = crate::start_backend(app, &engine, true) {
            crate::fail(app, &state, error);
        } else {
            crate::publish(app, &state);
        }
        saves.reopen();
        engine.closing.store(false, Ordering::SeqCst);
        return Err("install_failed");
    }
    engine.closed.store(true, Ordering::SeqCst);
    app.restart();
}

#[tauri::command]
pub async fn desktop_update_install(window: tauri::WebviewWindow) -> Result<()> {
    trusted(&window)?;
    let (update, bytes) = {
        let updates = window.state::<Updates>();
        let mut state = updates.0.lock().unwrap();
        if state.snapshot.phase.busy() {
            return Err("busy");
        }
        if state.snapshot.mode != Mode::Automatic || state.snapshot.phase != Phase::Ready {
            return Err("unavailable");
        }
        let update = state.update.clone().ok_or("unavailable")?;
        let bytes = state.bytes.clone().ok_or("unavailable")?;
        state.snapshot.phase = Phase::Installing;
        state.snapshot.error = None;
        (update, bytes)
    };
    let app = window.app_handle().clone();
    let result = tauri::async_runtime::spawn_blocking(move || {
        let result = install(&app, update, bytes);
        if let Err(error) = result {
            let updates = app.state::<Updates>();
            let mut state = updates.0.lock().unwrap();
            state.snapshot.phase = Phase::Ready;
            state.snapshot.error = Some(error);
        }
        result
    })
    .await;
    result.unwrap_or(Err("install_failed"))
}

#[tauri::command]
pub async fn desktop_update_open_release(window: tauri::WebviewWindow) -> Result<()> {
    trusted(&window)?;
    tauri::async_runtime::spawn_blocking(|| open::that(RELEASES))
        .await
        .map_err(|_| "unavailable")?
        .map_err(|_| "unavailable")
}

#[cfg(test)]
mod tests {
    use super::*;

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
        let value = serde_json::to_value(&updates.0.lock().unwrap().snapshot).unwrap();
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
        assert!(mode("1.2.3", "public", false, "macos", "x86_64", false) == Mode::Manual);
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
        assert!(trusted_artifact(
            &MANIFEST
                .replace("latest/download/latest.json", "download/v1.2.3/app.exe")
                .parse()
                .unwrap()
        ));
        for url in [
            "http://github.com/BigDawnGhost/wenyi/releases/download/v1/app.exe",
            "https://github.com/other/repo/releases/download/v1/app.exe",
            "https://github.com.evil.example/BigDawnGhost/wenyi/releases/download/v1/app.exe",
            "https://user@github.com/BigDawnGhost/wenyi/releases/download/v1/app.exe",
            "https://github.com/BigDawnGhost/wenyi/releases/download/v1/app.exe?token=secret",
        ] {
            assert!(!trusted_artifact(&url.parse().unwrap()));
        }
    }

    #[test]
    fn checks_cannot_replace_an_inflight_download_or_install() {
        let updates = Updates::new();
        let mut state = updates.0.lock().unwrap();
        for phase in [Phase::Checking, Phase::Downloading, Phase::Installing] {
            state.snapshot.phase = phase;
            state.bytes = Some(Arc::new(vec![1, 2, 3]));
            assert_eq!(begin_check(&mut state), Err("busy"));
            assert_eq!(state.bytes.as_ref().unwrap().as_slice(), &[1, 2, 3]);
        }
        state.snapshot.phase = Phase::Ready;
        state.snapshot.error = Some("install_failed");
        begin_check(&mut state).unwrap();
        assert!(state.snapshot.phase == Phase::Checking);
        assert!(state.bytes.is_none());
        assert!(state.snapshot.error.is_none());
    }
}
