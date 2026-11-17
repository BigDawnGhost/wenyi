//! Platform-independent status notifications.
//!
//! The engine reports terminal states on the same ordered pipe as diagnostics,
//! but on a separate line type: a notice carries a project display name, which
//! must not enter the diagnostic persistence boundary. This module validates the
//! notice again, applies the policy, and hands one localized presentation to a
//! platform sink. Platform selection is the only source of support information;
//! permission prompts and native event-loop requirements stay outside policy.

#[cfg(target_os = "linux")]
mod linux;
#[cfg(target_os = "macos")]
mod macos;
#[cfg(windows)]
mod windows;

mod preferences;

use std::{
    collections::HashMap,
    path::Path,
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        mpsc, Arc,
    },
    time::{Duration, Instant},
};

use serde::{Deserialize, Serialize};
use serde_json::json;
use sha2::{Digest, Sha256};

use crate::diagnostics::{Diagnostics, Event, Source};

/// Stable application identity, independent of the platform's registration scheme.
pub const APP_ID: &str = "org.wenyi.desktop";
const QUEUE: usize = 32;
const MAX_NOTICE: usize = 2048;
const DEDUPE: Duration = Duration::from_secs(600);
const DEDUPE_LIMIT: usize = 512;
const RATE_WINDOW: Duration = Duration::from_secs(60);
const RATE_LIMIT: usize = 5;

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Kind {
    TaskFailed,
    TaskTimeout,
    EngineStopped,
    ExportFailed,
    PrepareCompleted,
    ProjectCompleted,
}

impl Kind {
    fn success(self) -> bool {
        matches!(self, Self::PrepareCompleted | Self::ProjectCompleted)
    }
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Task {
    Parse,
    Prepare,
    Translation,
    ChapterTranslation,
    Review,
    Export,
}

#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum Reason {
    Timeout,
    Permission,
    Io,
    Invalid,
    Dependency,
    Interrupted,
    Unknown,
}

/// One terminal state, as the engine reports it.
#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Notice {
    kind: Kind,
    #[serde(default)]
    project_id: Option<String>,
    #[serde(default)]
    project_name: Option<String>,
    #[serde(default)]
    run_id: Option<String>,
    #[serde(default)]
    task: Option<Task>,
    #[serde(default)]
    reason: Option<Reason>,
}

impl Notice {
    /// Parse and revalidate one protocol payload; malformed notices are dropped.
    pub fn parse(payload: &str) -> Option<Self> {
        if payload.len() > MAX_NOTICE {
            return None;
        }
        let notice: Self = serde_json::from_str(payload).ok()?;
        notice.valid().then_some(notice)
    }

    fn valid(&self) -> bool {
        match self.kind {
            Kind::EngineStopped => {
                self.project_id.is_none()
                    && self.project_name.is_none()
                    && self.run_id.is_none()
                    && self.task.is_none()
                    && self.reason.is_none()
            }
            Kind::TaskFailed | Kind::TaskTimeout | Kind::ExportFailed => {
                self.identities()
                    && self.name().is_some()
                    && self.task.is_some()
                    && self.reason.is_some()
            }
            Kind::PrepareCompleted | Kind::ProjectCompleted => {
                self.identities()
                    && self.name().is_some()
                    && self.task.is_some()
                    && self.reason.is_none()
            }
        }
    }

    fn identities(&self) -> bool {
        [self.project_id.as_deref(), self.run_id.as_deref()]
            .into_iter()
            .all(|value| {
                value.is_some_and(|text| {
                    !text.is_empty()
                        && text.len() <= 128
                        && text
                            .bytes()
                            .all(|byte| byte.is_ascii_alphanumeric() || b"-_.:".contains(&byte))
                })
            })
    }

    fn name(&self) -> Option<&str> {
        self.project_name.as_deref().map(str::trim).filter(|name| {
            !name.is_empty() && name.chars().count() <= 120 && !name.chars().any(char::is_control)
        })
    }
}

/// One localized notification ready for a platform sink.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Presentation {
    pub title: String,
    pub body: String,
    pub tag: String,
    pub group: String,
}

/// A bounded platform failure classification; never carries user text.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Failure {
    Unavailable,
    PermissionRequired,
    PermissionDenied,
    OsDisabled,
    DeliveryFailed,
    TimedOut,
}

impl Failure {
    pub const ALL: [Self; 6] = [
        Self::Unavailable,
        Self::PermissionRequired,
        Self::PermissionDenied,
        Self::OsDisabled,
        Self::DeliveryFailed,
        Self::TimedOut,
    ];

    pub fn reason(self) -> &'static str {
        match self {
            Self::Unavailable => "unavailable",
            Self::PermissionRequired => "permission_required",
            Self::PermissionDenied => "permission_denied",
            Self::OsDisabled => "os_disabled",
            Self::DeliveryFailed => "delivery_failed",
            Self::TimedOut => "delivery_timeout",
        }
    }

    pub fn suppressed(self) -> bool {
        matches!(
            self,
            Self::Unavailable
                | Self::PermissionRequired
                | Self::PermissionDenied
                | Self::OsDisabled
        )
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
pub enum Permission {
    NotRequired,
    NotDetermined,
    Granted,
    Denied,
    Unavailable,
}

/// Platform port, owned and called serially by one dedicated dispatcher thread.
///
/// Neither availability checks nor delivery may prompt for permission. Native
/// I/O and completion waits must have deadlines; permission requests have a
/// separate, explicit user action. `show(Ok)` means the OS accepted the request,
/// never that it displayed a banner (focus modes and user settings still apply).
/// Adapters marshal UI work to the main thread without waiting for that thread.
/// No callback may log payloads or alter task state.
pub trait Sink: Send + 'static {
    /// Why this platform cannot show a notification right now, if it cannot.
    fn available(&mut self) -> Result<(), Failure>;
    /// Submit a localized notification and await bounded acknowledgement.
    fn show(&mut self, presentation: &Presentation) -> Result<(), Failure>;
    /// Best-effort attention request, without showing/focusing the window.
    fn fallback(&mut self, presentation: &Presentation);
    /// Release platform resources on shutdown.
    fn shutdown(&mut self);
}

#[cfg(test)]
struct NullSink;

#[cfg(test)]
impl Sink for NullSink {
    fn available(&mut self) -> Result<(), Failure> {
        Err(Failure::Unavailable)
    }
    fn show(&mut self, _presentation: &Presentation) -> Result<(), Failure> {
        Err(Failure::Unavailable)
    }
    fn fallback(&mut self, _presentation: &Presentation) {}
    fn shutdown(&mut self) {}
}

#[cfg(windows)]
fn platform_sink(app: &tauri::AppHandle, workspace: &Path) -> Option<Box<dyn Sink>> {
    Some(Box::new(windows::WindowsSink::new(app.clone(), workspace)))
}

#[cfg(target_os = "linux")]
fn platform_sink(app: &tauri::AppHandle, _workspace: &Path) -> Option<Box<dyn Sink>> {
    Some(Box::new(linux::LinuxSink::new(app.clone())))
}

#[cfg(target_os = "macos")]
fn platform_sink(app: &tauri::AppHandle, _workspace: &Path) -> Option<Box<dyn Sink>> {
    Some(Box::new(macos::MacosSink::new(app.clone())))
}

#[cfg(not(any(windows, target_os = "linux", target_os = "macos")))]
fn platform_sink(_app: &tauri::AppHandle, _workspace: &Path) -> Option<Box<dyn Sink>> {
    None
}

#[cfg(any(target_os = "linux", target_os = "macos"))]
fn request_attention(app: &tauri::AppHandle) {
    use tauri::Manager;

    let handle = app.clone();
    let _ = app.run_on_main_thread(move || {
        if !handle
            .state::<Center>()
            .preferences
            .enabled
            .load(Ordering::SeqCst)
        {
            return;
        }
        if let Some(window) = handle.get_webview_window("main") {
            let _ = window.request_user_attention(Some(tauri::UserAttentionType::Informational));
        }
    });
}

/// One notification center per process: policy, dedupe, rate limit and delivery.
#[derive(Clone)]
pub struct Center {
    sender: Option<mpsc::SyncSender<Command>>,
    armed: Arc<AtomicBool>,
    session: Arc<AtomicU64>,
    stopping: Arc<AtomicBool>,
    diagnostics: Diagnostics,
    preferences: Arc<preferences::Preferences>,
}

enum Command {
    Notice(Notice),
    EngineStopped(u64),
    Locale(String),
    Success(bool),
    Stop,
}

pub fn install(app: &tauri::AppHandle, diagnostics: Diagnostics, workspace: &Path) -> Center {
    let identity = format!(
        "{:x}",
        Sha256::digest(workspace.to_string_lossy().as_bytes())
    );
    let group = format!("{APP_ID}:{}", &identity[..16]);
    with_preferences(
        platform_sink(app, workspace),
        diagnostics,
        group,
        preferences::Preferences::new(workspace),
    )
}

#[cfg(test)]
fn with_sink(sink: Option<Box<dyn Sink>>, diagnostics: Diagnostics, group: String) -> Center {
    with_preferences(sink, diagnostics, group, preferences::Preferences::memory())
}

fn with_preferences(
    sink: Option<Box<dyn Sink>>,
    diagnostics: Diagnostics,
    group: String,
    preferences: preferences::Preferences,
) -> Center {
    let preferences = Arc::new(preferences);
    let armed = Arc::new(AtomicBool::new(false));
    let session = Arc::new(AtomicU64::new(0));
    let stopping = Arc::new(AtomicBool::new(false));
    let sender = sink.map(|sink| {
        let (sender, receiver) = mpsc::sync_channel(QUEUE);
        let worker = Worker {
            receiver,
            armed: armed.clone(),
            stopping: stopping.clone(),
            enabled: preferences.enabled.clone(),
            sink,
            diagnostics: diagnostics.clone(),
            policy: Policy::new(group),
        };
        std::thread::spawn(move || worker.run());
        sender
    });
    Center {
        sender,
        armed,
        session,
        stopping,
        diagnostics,
        preferences,
    }
}

impl Center {
    /// Enter the running state after the engine is ready.
    pub fn arm(&self) {
        self.session.fetch_add(1, Ordering::SeqCst);
        self.armed.store(true, Ordering::SeqCst);
    }

    /// Leave the running state during startup, shutdown or after engine death.
    pub fn disarm(&self) {
        self.armed.store(false, Ordering::SeqCst);
    }

    /// Report one unexpected engine exit as a single notice.
    pub fn engine_stopped(&self) {
        if self.preferences.enabled.load(Ordering::SeqCst) {
            self.send(Command::EngineStopped(self.session.load(Ordering::SeqCst)));
        }
    }

    /// Validate and queue one protocol payload.
    pub fn submit_payload(&self, payload: &str) {
        if !self.preferences.enabled.load(Ordering::SeqCst) {
            self.suppressed("disabled");
            return;
        }
        match Notice::parse(payload) {
            Some(notice) => self.send(Command::Notice(notice)),
            None => self.suppressed("invalid_payload"),
        }
    }

    /// Apply a locale reported by the interface. Unknown languages fall back to English.
    pub fn set_locale(&self, locale: &str) {
        let locale = if locale.to_ascii_lowercase().starts_with("zh") {
            "zh-CN"
        } else {
            "en"
        };
        self.send(Command::Locale(locale.to_string()));
    }

    /// Persist before confirming a change; admission never depends on queue capacity.
    fn set_enabled(&self, enabled: bool) -> std::io::Result<bool> {
        self.preferences.save(enabled)
    }

    /// Reserved for the settings interface that a later batch adds.
    #[allow(dead_code)]
    pub fn set_success_enabled(&self, enabled: bool) {
        self.send(Command::Success(enabled));
    }

    /// Stop accepting work, drain briefly and release the platform sink.
    pub fn stop(&self) {
        self.armed.store(false, Ordering::SeqCst);
        // A full queue must not lose the stop: the flag ends the worker on its
        // next poll even when this wake-up command cannot be queued.
        self.stopping.store(true, Ordering::SeqCst);
        if let Some(sender) = &self.sender {
            let _ = sender.try_send(Command::Stop);
        }
    }

    fn send(&self, command: Command) {
        if let Some(sender) = &self.sender {
            if sender.try_send(command).is_err() {
                self.suppressed("queue_full");
            }
        }
    }

    fn suppressed(&self, reason: &'static str) {
        if self.sender.is_some() {
            self.diagnostics.record(
                Source::Native,
                Event::NotificationSuppressed,
                json!({ "reason": reason }),
            );
        }
    }

    #[cfg(test)]
    pub(crate) fn silent(diagnostics: Diagnostics) -> Self {
        with_sink(Some(Box::new(NullSink)), diagnostics, "test".into())
    }

    #[cfg(test)]
    pub(crate) fn is_armed(&self) -> bool {
        self.armed.load(Ordering::SeqCst)
    }
}

/// Set the process identity before the first window exists (Windows only).
#[cfg(windows)]
pub fn set_process_app_user_model_id() {
    windows::set_app_user_model_id();
}

/// Let the interface report the language its notifications should use.
#[tauri::command]
pub fn desktop_notifications_set_locale(
    window: tauri::WebviewWindow,
    locale: String,
) -> Result<(), crate::ipc_error::IpcError> {
    use tauri::Manager;

    if window.label() != "main" || !window.url().is_ok_and(|url| crate::trusted(&url)) {
        return Err(crate::ipc_error::IpcError::new(
            "notifications_untrusted_window",
            "Desktop notifications are unavailable.",
        ));
    }
    window.state::<Center>().set_locale(&locale);
    Ok(())
}

/// Permission queries never prompt; only the UI's explicit button sends request=true.
#[tauri::command]
pub async fn desktop_notifications_permission(
    window: tauri::WebviewWindow,
    request: bool,
) -> Result<Permission, crate::ipc_error::IpcError> {
    use tauri::Manager;

    if window.label() != "main" || !window.url().is_ok_and(|url| crate::trusted(&url)) {
        return Err(crate::ipc_error::IpcError::new(
            "notifications_untrusted_window",
            "Desktop notifications are unavailable.",
        ));
    }
    if !window
        .state::<Center>()
        .preferences
        .enabled
        .load(Ordering::SeqCst)
    {
        return Ok(Permission::Unavailable);
    }
    tauri::async_runtime::spawn_blocking(move || {
        #[cfg(target_os = "macos")]
        {
            macos::permission(request)
        }
        #[cfg(not(target_os = "macos"))]
        {
            let _ = request;
            Permission::NotRequired
        }
    })
    .await
    .map_err(|_| {
        crate::ipc_error::IpcError::new(
            "notifications_unavailable",
            "Desktop notifications are unavailable.",
        )
    })
}

/// Native UI preferences are separate from translation configuration and OS consent.
#[tauri::command]
pub async fn desktop_notifications_enabled(
    window: tauri::WebviewWindow,
) -> Result<bool, crate::ipc_error::IpcError> {
    notification_preference(window, None).await
}

#[tauri::command]
pub async fn desktop_notifications_set_enabled(
    window: tauri::WebviewWindow,
    enabled: bool,
) -> Result<bool, crate::ipc_error::IpcError> {
    notification_preference(window, Some(enabled)).await
}

async fn notification_preference(
    window: tauri::WebviewWindow,
    enabled: Option<bool>,
) -> Result<bool, crate::ipc_error::IpcError> {
    use tauri::Manager;

    if window.label() != "main" || !window.url().is_ok_and(|url| crate::trusted(&url)) {
        return Err(crate::ipc_error::IpcError::new(
            "notifications_untrusted_window",
            "Desktop notifications are unavailable.",
        ));
    }
    let center = window.state::<Center>().inner().clone();
    tauri::async_runtime::spawn_blocking(move || match enabled {
        Some(enabled) => center.set_enabled(enabled),
        None => center.preferences.read(),
    })
    .await
    .ok()
    .and_then(Result::ok)
    .ok_or_else(|| {
        crate::ipc_error::IpcError::new(
            "notifications_settings_unavailable",
            "Notification settings could not be read or saved. Please retry.",
        )
    })
}

struct Policy {
    success_enabled: bool,
    locale: String,
    seen: HashMap<String, Instant>,
    attempts: Vec<Instant>,
    group: String,
}

impl Policy {
    fn new(group: String) -> Self {
        Self {
            success_enabled: true,
            locale: default_locale(),
            seen: HashMap::new(),
            attempts: Vec::new(),
            group,
        }
    }

    fn expire(&mut self, now: Instant) {
        self.seen
            .retain(|_, shown| now.duration_since(*shown) < DEDUPE);
        if self.seen.len() > DEDUPE_LIMIT {
            // The window is ten minutes; reaching the cap means a pathological
            // stream of runs. Forgetting the oldest keys is preferable to growing
            // without a bound, and the display still suppresses rate-limited work.
            self.seen.clear();
        }
    }

    fn key(&self, notice: &Notice) -> String {
        format!("run:{}", notice.run_id.as_deref().unwrap_or_default())
    }

    fn allow_rate(&mut self, now: Instant) -> bool {
        self.attempts
            .retain(|attempt| now.duration_since(*attempt) < RATE_WINDOW);
        self.attempts.len() < RATE_LIMIT
    }

    fn presentation(&self, notice: &Notice) -> Presentation {
        let chinese = self.locale == "zh-CN";
        let title = notice
            .project_name
            .clone()
            .unwrap_or_else(|| "Wenyi Desktop".to_string());
        let body = match notice.kind {
            Kind::TaskFailed => {
                let task = task_label(notice.task.unwrap_or(Task::Translation), chinese);
                if chinese {
                    format!("{task}失败")
                } else {
                    format!("{task} failed")
                }
            }
            Kind::ExportFailed => {
                if chinese {
                    "导出失败".to_string()
                } else {
                    "Export failed".to_string()
                }
            }
            Kind::TaskTimeout => {
                let task = task_label(notice.task.unwrap_or(Task::Translation), chinese);
                if chinese {
                    format!("{task}超时，任务已停止")
                } else {
                    format!("{task} timed out and was stopped")
                }
            }
            Kind::EngineStopped => {
                if chinese {
                    "本地引擎意外停止，所有运行已中断".to_string()
                } else {
                    "The local engine stopped unexpectedly; active runs were interrupted"
                        .to_string()
                }
            }
            Kind::PrepareCompleted => {
                if chinese {
                    "翻译准备已完成".to_string()
                } else {
                    "Translation preparation is complete".to_string()
                }
            }
            Kind::ProjectCompleted => {
                if chinese {
                    "全书翻译已完成".to_string()
                } else {
                    "Book translation is complete".to_string()
                }
            }
        };
        let tag = match notice.kind {
            Kind::EngineStopped => "engine".to_string(),
            _ => {
                let run = notice.run_id.as_deref().unwrap_or_default();
                format!("run:{}", run.chars().take(48).collect::<String>())
            }
        };
        Presentation {
            title,
            body,
            tag,
            group: self.group.clone(),
        }
    }
}

struct Worker {
    receiver: mpsc::Receiver<Command>,
    armed: Arc<AtomicBool>,
    stopping: Arc<AtomicBool>,
    enabled: Arc<AtomicBool>,
    sink: Box<dyn Sink>,
    diagnostics: Diagnostics,
    policy: Policy,
}

impl Worker {
    fn run(mut self) {
        loop {
            if self.stopping.load(Ordering::SeqCst) {
                break;
            }
            match self.receiver.recv_timeout(Duration::from_millis(250)) {
                Ok(Command::Notice(notice)) => {
                    let key = self.policy.key(&notice);
                    let armed = self.armed.load(Ordering::SeqCst);
                    self.handle(notice, key, armed);
                }
                Ok(Command::EngineStopped(session)) => {
                    // Native ownership proves this notice came from an engine that
                    // was armed; it must not lose a race with the following disarm.
                    self.handle(
                        Notice {
                            kind: Kind::EngineStopped,
                            project_id: None,
                            project_name: None,
                            run_id: None,
                            task: None,
                            reason: None,
                        },
                        format!("engine:{session}"),
                        true,
                    );
                }
                Ok(Command::Locale(locale)) => self.policy.locale = locale,
                Ok(Command::Success(enabled)) => self.policy.success_enabled = enabled,
                Ok(Command::Stop) => break,
                Err(mpsc::RecvTimeoutError::Timeout) => {}
                Err(mpsc::RecvTimeoutError::Disconnected) => break,
            }
        }
        self.sink.shutdown();
    }

    fn handle(&mut self, notice: Notice, key: String, armed: bool) {
        if !armed {
            self.suppress("unarmed");
            return;
        }
        if !self.enabled.load(Ordering::SeqCst) {
            self.suppress("disabled");
            return;
        }
        if notice.kind.success() && !self.policy.success_enabled {
            self.suppress("disabled_success");
            return;
        }
        let now = Instant::now();
        self.policy.expire(now);
        if self.policy.seen.contains_key(&key) {
            self.suppress("duplicate");
            return;
        }
        let presentation = self.policy.presentation(&notice);
        if !self.policy.allow_rate(now) {
            self.suppress("rate_limited");
            return;
        }
        // Every alert attempt consumes the budget, including attention fallback.
        // Missing permission or a broken service must not bypass rate limiting.
        self.policy.seen.insert(key, now);
        self.policy.attempts.push(now);
        if let Err(failure) = self.sink.available() {
            self.failed(failure);
            if self.enabled.load(Ordering::SeqCst) {
                self.sink.fallback(&presentation);
            }
            return;
        }
        // Availability can block on the OS while the user disables notifications.
        if !self.enabled.load(Ordering::SeqCst) {
            self.suppress("disabled");
            return;
        }
        match self.sink.show(&presentation) {
            Ok(()) => {
                self.diagnostics
                    .record(Source::Native, Event::NotificationShown, json!({}));
            }
            Err(failure) => {
                self.failed(failure);
                if self.enabled.load(Ordering::SeqCst) {
                    self.sink.fallback(&presentation);
                }
            }
        }
    }

    fn failed(&self, failure: Failure) {
        if failure.suppressed() {
            self.suppress(failure.reason());
        } else {
            self.diagnostics.record(
                Source::Native,
                Event::NotificationFailed,
                json!({ "reason": failure.reason() }),
            );
        }
    }

    fn suppress(&self, reason: &'static str) {
        self.diagnostics.record(
            Source::Native,
            Event::NotificationSuppressed,
            json!({ "reason": reason }),
        );
    }
}

fn task_label(task: Task, chinese: bool) -> &'static str {
    match (task, chinese) {
        (Task::Parse, true) => "解析",
        (Task::Parse, false) => "Parsing",
        (Task::Prepare, true) => "翻译准备",
        (Task::Prepare, false) => "Preparation",
        (Task::Translation, true) => "翻译",
        (Task::Translation, false) => "Translation",
        (Task::ChapterTranslation, true) => "章节翻译",
        (Task::ChapterTranslation, false) => "Chapter translation",
        (Task::Review, true) => "审阅",
        (Task::Review, false) => "Review",
        (Task::Export, true) => "导出",
        (Task::Export, false) => "Export",
    }
}

fn default_locale() -> String {
    #[cfg(windows)]
    {
        if windows::user_locale()
            .to_ascii_lowercase()
            .starts_with("zh")
        {
            return "zh-CN".to_string();
        }
    }
    #[cfg(target_os = "macos")]
    {
        return macos::user_locale();
    }
    #[cfg(not(target_os = "macos"))]
    "en".to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    #[derive(Clone, Default)]
    struct Recording(Arc<Mutex<Vec<Presentation>>>);

    impl Sink for Recording {
        fn available(&mut self) -> Result<(), Failure> {
            Ok(())
        }
        fn show(&mut self, presentation: &Presentation) -> Result<(), Failure> {
            self.0.lock().unwrap().push(presentation.clone());
            Ok(())
        }
        fn fallback(&mut self, _presentation: &Presentation) {}
        fn shutdown(&mut self) {}
    }

    fn center(sink: Recording) -> Center {
        with_sink(Some(Box::new(sink)), Diagnostics::new(None), "test".into())
    }

    #[test]
    fn missing_adapter_is_inert_without_a_second_support_flag() {
        let center = with_sink(None, Diagnostics::new(None), "test".into());
        assert!(center.sender.is_none());
        center.arm();
        center.submit_payload("invalid");
        center.engine_stopped();
        center.stop();
    }

    #[test]
    fn native_identity_and_macos_minimum_match_the_bundle() {
        let config: serde_json::Value =
            serde_json::from_str(include_str!("../../tauri.conf.json")).unwrap();
        assert_eq!(config["identifier"], APP_ID);
        assert_eq!(config["bundle"]["macOS"]["minimumSystemVersion"], "10.14");
    }

    fn failed_notice(run: &str) -> Notice {
        Notice::parse(&format!(
            concat!(
                "{{\"kind\":\"task_failed\",\"project_id\":\"abc\",\"project_name\":\"Book\",",
                "\"run_id\":\"{}\",\"task\":\"translation\",\"reason\":\"io\"}}"
            ),
            run
        ))
        .unwrap()
    }

    fn wait_for(shown: &Recording, count: usize) {
        let deadline = Instant::now() + Duration::from_secs(2);
        while Instant::now() < deadline {
            if shown.0.lock().unwrap().len() >= count {
                return;
            }
            std::thread::sleep(Duration::from_millis(5));
        }
        panic!("notification was not delivered");
    }

    #[test]
    fn notices_are_strict_and_bounded() {
        assert!(Notice::parse(
            r#"{"kind":"task_failed","project_id":"abc","project_name":"Book","run_id":"run","task":"translation","reason":"io"}"#
        )
        .is_some());
        for payload in [
            r#"{"kind":"task_failed","project_id":"abc","project_name":"Book","run_id":"run","task":"translation"}"#,
            r#"{"kind":"task_failed","project_id":"abc","project_name":"","run_id":"run","task":"translation","reason":"io"}"#,
            r#"{"kind":"engine_stopped","run_id":"run"}"#,
            r#"{"kind":"task_failed","project_id":"abc","project_name":"Book","run_id":"run","task":"translation","reason":"io","message":"secret"}"#,
            r#"{"kind":"unknown"}"#,
        ] {
            assert!(Notice::parse(payload).is_none(), "{payload}");
        }
        assert!(Notice::parse(&"x".repeat(MAX_NOTICE + 1)).is_none());
    }

    #[test]
    fn unarmed_and_duplicate_notices_are_suppressed() {
        let shown = Recording::default();
        let center = center(shown.clone());
        center.submit_payload(
            &serde_json::to_string(&json!({
                "kind": "task_failed", "project_id": "abc", "project_name": "Book",
                "run_id": "run", "task": "translation", "reason": "io"
            }))
            .unwrap(),
        );
        std::thread::sleep(Duration::from_millis(30));
        assert!(shown.0.lock().unwrap().is_empty());
        center.arm();
        center.send(Command::Notice(failed_notice("run")));
        center.send(Command::Notice(failed_notice("run")));
        wait_for(&shown, 1);
        std::thread::sleep(Duration::from_millis(30));
        assert_eq!(shown.0.lock().unwrap().len(), 1);
    }

    #[test]
    fn success_can_be_disabled_and_engine_death_is_reported_once() {
        let shown = Recording::default();
        let center = center(shown.clone());
        center.set_success_enabled(false);
        center.arm();
        center.send(Command::Notice(
            Notice::parse(
                r#"{"kind":"project_completed","project_id":"abc","project_name":"Book","run_id":"done","task":"translation"}"#,
            )
            .unwrap(),
        ));
        center.engine_stopped();
        center.engine_stopped();
        wait_for(&shown, 1);
        std::thread::sleep(Duration::from_millis(30));
        let shown = shown.0.lock().unwrap();
        assert_eq!(shown.len(), 1);
        assert_eq!(shown[0].tag, "engine");
        assert!(shown[0].body.contains("engine"));
    }

    #[derive(Clone)]
    struct Unavailable {
        shown: Arc<Mutex<usize>>,
        fallbacks: Arc<Mutex<usize>>,
    }

    impl Sink for Unavailable {
        fn available(&mut self) -> Result<(), Failure> {
            Err(Failure::OsDisabled)
        }
        fn show(&mut self, _presentation: &Presentation) -> Result<(), Failure> {
            *self.shown.lock().unwrap() += 1;
            Ok(())
        }
        fn fallback(&mut self, _presentation: &Presentation) {
            *self.fallbacks.lock().unwrap() += 1;
        }
        fn shutdown(&mut self) {}
    }

    #[test]
    fn an_unavailable_sink_falls_back_without_showing() {
        let sink = Unavailable {
            shown: Arc::new(Mutex::new(0)),
            fallbacks: Arc::new(Mutex::new(0)),
        };
        let center = with_sink(
            Some(Box::new(sink.clone())),
            Diagnostics::new(None),
            "test".into(),
        );
        center.arm();
        center.send(Command::Notice(failed_notice("run")));
        let deadline = Instant::now() + Duration::from_secs(2);
        while *sink.fallbacks.lock().unwrap() == 0 && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(5));
        }
        assert_eq!(*sink.fallbacks.lock().unwrap(), 1);
        assert_eq!(*sink.shown.lock().unwrap(), 0);
    }

    #[test]
    fn disabling_a_full_queue_suppresses_pending_delivery_and_attention() {
        struct Blocked {
            entered: mpsc::Sender<()>,
            release: mpsc::Receiver<()>,
            stopped: mpsc::Sender<()>,
            effects: Arc<AtomicU64>,
            available: bool,
        }
        impl Sink for Blocked {
            fn available(&mut self) -> Result<(), Failure> {
                self.entered.send(()).unwrap();
                self.release.recv_timeout(Duration::from_secs(2)).unwrap();
                if self.available {
                    Ok(())
                } else {
                    Err(Failure::Unavailable)
                }
            }
            fn show(&mut self, _: &Presentation) -> Result<(), Failure> {
                self.effects.fetch_add(1, Ordering::SeqCst);
                Ok(())
            }
            fn fallback(&mut self, _: &Presentation) {
                self.effects.fetch_add(1, Ordering::SeqCst);
            }
            fn shutdown(&mut self) {
                self.stopped.send(()).unwrap();
            }
        }
        for available in [true, false] {
            let (entered, started) = mpsc::channel();
            let (release, waiting) = mpsc::channel();
            let (stopped, finished) = mpsc::channel();
            let effects = Arc::new(AtomicU64::new(0));
            let center = with_sink(
                Some(Box::new(Blocked {
                    entered,
                    release: waiting,
                    stopped,
                    effects: effects.clone(),
                    available,
                })),
                Diagnostics::new(None),
                "test".into(),
            );
            center.arm();
            center.send(Command::Notice(failed_notice("in-flight")));
            started.recv_timeout(Duration::from_secs(2)).unwrap();
            for index in 0..QUEUE {
                center.send(Command::Notice(failed_notice(&format!("queued-{index}"))));
            }
            assert!(matches!(
                center.sender.as_ref().unwrap().try_send(Command::Stop),
                Err(mpsc::TrySendError::Full(_))
            ));
            assert!(!center.set_enabled(false).unwrap());
            release.send(()).unwrap();
            // A blocking Stop follows all pending notices, proving they were suppressed.
            center.sender.as_ref().unwrap().send(Command::Stop).unwrap();
            finished.recv_timeout(Duration::from_secs(2)).unwrap();
            assert_eq!(effects.load(Ordering::SeqCst), 0);
        }
    }

    #[test]
    fn disabled_startup_drops_task_and_engine_notices_without_replaying_on_enable() {
        let preferences = preferences::Preferences::memory();
        preferences.save(false).unwrap();
        let shown = Recording::default();
        let center = with_preferences(
            Some(Box::new(shown.clone())),
            Diagnostics::new(None),
            "test".into(),
            preferences,
        );
        center.arm();
        center.submit_payload(
            r#"{"kind":"task_failed","project_id":"abc","project_name":"Book","run_id":"disabled","task":"translation","reason":"io"}"#,
        );
        center.engine_stopped();
        assert!(center.set_enabled(true).unwrap());
        center.send(Command::Notice(failed_notice("enabled")));
        wait_for(&shown, 1);
        assert_eq!(shown.0.lock().unwrap().len(), 1);
        assert_eq!(shown.0.lock().unwrap()[0].tag, "run:enabled");
        center.stop();
    }

    #[test]
    fn visible_notifications_are_rate_limited_per_minute() {
        let shown = Recording::default();
        let center = center(shown.clone());
        center.arm();
        for index in 0..RATE_LIMIT + 1 {
            center.send(Command::Notice(failed_notice(&format!("run-{index}"))));
        }
        wait_for(&shown, RATE_LIMIT);
        std::thread::sleep(Duration::from_millis(50));
        assert_eq!(shown.0.lock().unwrap().len(), RATE_LIMIT);
    }

    #[test]
    fn unavailable_notifications_cannot_bypass_the_attention_rate_limit() {
        let sink = Unavailable {
            shown: Arc::new(Mutex::new(0)),
            fallbacks: Arc::new(Mutex::new(0)),
        };
        let center = with_sink(
            Some(Box::new(sink.clone())),
            Diagnostics::new(None),
            "test".into(),
        );
        center.arm();
        for index in 0..RATE_LIMIT + 1 {
            center.send(Command::Notice(failed_notice(&format!("run-{index}"))));
        }
        let deadline = Instant::now() + Duration::from_secs(2);
        while *sink.fallbacks.lock().unwrap() < RATE_LIMIT && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(5));
        }
        std::thread::sleep(Duration::from_millis(50));
        assert_eq!(*sink.fallbacks.lock().unwrap(), RATE_LIMIT);
        assert_eq!(*sink.shown.lock().unwrap(), 0);
        center.stop();
    }

    #[test]
    fn delivery_failure_and_shutdown_stay_on_the_dispatcher() {
        #[derive(Clone, Default)]
        struct Probe(Arc<Mutex<Vec<(&'static str, std::thread::ThreadId)>>>);
        impl Probe {
            fn record(&self, event: &'static str) {
                self.0
                    .lock()
                    .unwrap()
                    .push((event, std::thread::current().id()));
            }
        }
        impl Sink for Probe {
            fn available(&mut self) -> Result<(), Failure> {
                self.record("available");
                Ok(())
            }
            fn show(&mut self, _: &Presentation) -> Result<(), Failure> {
                self.record("show");
                Err(Failure::DeliveryFailed)
            }
            fn fallback(&mut self, _: &Presentation) {
                self.record("fallback");
            }
            fn shutdown(&mut self) {
                self.record("shutdown");
            }
        }
        let probe = Probe::default();
        let center = with_sink(
            Some(Box::new(probe.clone())),
            Diagnostics::new(None),
            "test".into(),
        );
        center.arm();
        center.send(Command::Notice(failed_notice("failed")));
        let deadline = Instant::now() + Duration::from_secs(2);
        while probe.0.lock().unwrap().len() < 3 && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(5));
        }
        center.stop();
        while probe.0.lock().unwrap().len() < 4 && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(5));
        }
        let events = probe.0.lock().unwrap();
        assert_eq!(
            events.iter().map(|event| event.0).collect::<Vec<_>>(),
            vec!["available", "show", "fallback", "shutdown"]
        );
        assert!(events.iter().all(|event| event.1 == events[0].1));
        assert_ne!(events[0].1, std::thread::current().id());
    }
}
