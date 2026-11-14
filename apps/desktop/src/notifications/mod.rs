//! Platform-independent status notifications.
//!
//! The engine reports terminal states on the same ordered pipe as diagnostics,
//! but on a separate line type: a notice carries a project display name, which
//! must not enter the diagnostic persistence boundary. This module validates the
//! notice again, applies the policy, and hands one localized presentation to a
//! platform sink. Windows owns the first sink; every other platform keeps the
//! empty implementation and changes no existing behaviour.

#[cfg(windows)]
mod windows;

use std::{
    collections::HashMap,
    path::Path,
    sync::{
        atomic::{AtomicBool, AtomicU64, Ordering},
        mpsc, Arc,
    },
    time::{Duration, Instant},
};

use serde::Deserialize;
use serde_json::json;
use sha2::{Digest, Sha256};

use crate::diagnostics::{Diagnostics, Event, Source};

/// Application identity shared by the process, its shortcuts and its toasts.
pub const AUMID: &str = "org.wenyi.desktop";
const QUEUE: usize = 32;
const MAX_NOTICE: usize = 2048;
const DEDUPE: Duration = Duration::from_secs(600);
const DEDUPE_LIMIT: usize = 512;
const RATE_WINDOW: Duration = Duration::from_secs(60);
const RATE_LIMIT: usize = 5;
/// Notifications are delivered only where a platform sink exists.
const SUPPORTED: bool = cfg!(windows);

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
#[derive(Debug, PartialEq, Eq)]
pub struct Failure(pub &'static str);

/// The only port a platform has to implement.
pub trait Sink: Send + 'static {
    /// Whether this platform can show a notification right now.
    fn available(&mut self) -> bool;
    /// Show one already-localized notification.
    fn show(&mut self, presentation: &Presentation) -> Result<(), Failure>;
    /// Best-effort tray/taskbar fallback after a failed or unavailable toast.
    fn fallback(&mut self, presentation: &Presentation);
    /// Release platform resources on shutdown.
    fn shutdown(&mut self);
}

#[cfg(any(not(windows), test))]
struct NullSink;

#[cfg(any(not(windows), test))]
impl Sink for NullSink {
    fn available(&mut self) -> bool {
        false
    }
    fn show(&mut self, _presentation: &Presentation) -> Result<(), Failure> {
        Err(Failure("unavailable"))
    }
    fn fallback(&mut self, _presentation: &Presentation) {}
    fn shutdown(&mut self) {}
}

#[cfg(windows)]
fn platform_sink(app: &tauri::AppHandle) -> Box<dyn Sink> {
    Box::new(windows::WindowsSink::new(app.clone()))
}

#[cfg(not(windows))]
fn platform_sink(_app: &tauri::AppHandle) -> Box<dyn Sink> {
    Box::new(NullSink)
}

/// One notification center per process: policy, dedupe, rate limit and delivery.
#[derive(Clone)]
pub struct Center {
    sender: mpsc::SyncSender<Command>,
    armed: Arc<AtomicBool>,
    session: Arc<AtomicU64>,
    stopping: Arc<AtomicBool>,
    diagnostics: Diagnostics,
}

enum Command {
    Notice(Notice),
    EngineStopped(u64),
    Locale(String),
    Enabled(bool),
    Success(bool),
    Stop,
}

pub fn install(app: &tauri::AppHandle, diagnostics: Diagnostics, workspace: &Path) -> Center {
    let identity = format!(
        "{:x}",
        Sha256::digest(workspace.to_string_lossy().as_bytes())
    );
    let group = format!("{AUMID}:{}", &identity[..16]);
    with_sink(platform_sink(app), diagnostics, group, SUPPORTED)
}

fn with_sink(
    sink: Box<dyn Sink>,
    diagnostics: Diagnostics,
    group: String,
    supported: bool,
) -> Center {
    let (sender, receiver) = mpsc::sync_channel(QUEUE);
    let armed = Arc::new(AtomicBool::new(false));
    let session = Arc::new(AtomicU64::new(0));
    let stopping = Arc::new(AtomicBool::new(false));
    let worker = Worker {
        receiver,
        armed: armed.clone(),
        stopping: stopping.clone(),
        sink,
        diagnostics: diagnostics.clone(),
        policy: Policy::new(group),
        supported,
    };
    if supported {
        std::thread::spawn(move || worker.run());
    } else {
        // No platform sink exists, so there is nothing to poll for. Dropping the
        // worker closes the channel; senders already treat that as a drop.
        drop(worker);
    }
    Center {
        sender,
        armed,
        session,
        stopping,
        diagnostics,
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
        self.send(Command::EngineStopped(self.session.load(Ordering::SeqCst)));
    }

    /// Validate and queue one protocol payload.
    pub fn submit_payload(&self, payload: &str) {
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

    /// Reserved for the settings interface that a later batch adds.
    #[allow(dead_code)]
    pub fn set_enabled(&self, enabled: bool) {
        self.send(Command::Enabled(enabled));
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
        let _ = self.sender.try_send(Command::Stop);
    }

    fn send(&self, command: Command) {
        if self.sender.try_send(command).is_err() {
            self.suppressed("queue_full");
        }
    }

    fn suppressed(&self, reason: &'static str) {
        if SUPPORTED {
            self.diagnostics.record(
                Source::Native,
                Event::NotificationSuppressed,
                json!({ "reason": reason }),
            );
        }
    }

    #[cfg(test)]
    pub(crate) fn silent(diagnostics: Diagnostics) -> Self {
        with_sink(Box::new(NullSink), diagnostics, "test".into(), true)
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

struct Policy {
    enabled: bool,
    success_enabled: bool,
    locale: String,
    seen: HashMap<String, Instant>,
    shown: Vec<Instant>,
    group: String,
}

impl Policy {
    fn new(group: String) -> Self {
        Self {
            enabled: true,
            success_enabled: true,
            locale: default_locale(),
            seen: HashMap::new(),
            shown: Vec::new(),
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
        self.shown
            .retain(|shown| now.duration_since(*shown) < RATE_WINDOW);
        self.shown.len() < RATE_LIMIT
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
                let reason = reason_label(notice.reason.unwrap_or(Reason::Unknown), chinese);
                if chinese {
                    format!("{task}失败：{reason}")
                } else {
                    format!("{task} failed: {reason}")
                }
            }
            Kind::ExportFailed => {
                let reason = reason_label(notice.reason.unwrap_or(Reason::Unknown), chinese);
                if chinese {
                    format!("导出失败：{reason}")
                } else {
                    format!("Export failed: {reason}")
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
    sink: Box<dyn Sink>,
    diagnostics: Diagnostics,
    policy: Policy,
    supported: bool,
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
                Ok(Command::Enabled(enabled)) => self.policy.enabled = enabled,
                Ok(Command::Success(enabled)) => self.policy.success_enabled = enabled,
                Ok(Command::Stop) => break,
                Err(mpsc::RecvTimeoutError::Timeout) => {}
                Err(mpsc::RecvTimeoutError::Disconnected) => break,
            }
        }
        self.sink.shutdown();
    }

    fn handle(&mut self, notice: Notice, key: String, armed: bool) {
        if !self.supported {
            return;
        }
        if !armed {
            self.suppress("unarmed");
            return;
        }
        if !self.policy.enabled {
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
        if !self.sink.available() {
            self.policy.seen.insert(key, now);
            self.suppress("os_disabled");
            self.sink.fallback(&presentation);
            return;
        }
        if !self.policy.allow_rate(now) {
            self.suppress("rate_limited");
            return;
        }
        self.policy.seen.insert(key, now);
        match self.sink.show(&presentation) {
            Ok(()) => {
                self.policy.shown.push(now);
                self.diagnostics
                    .record(Source::Native, Event::NotificationShown, json!({}));
            }
            Err(Failure("os_disabled")) => {
                self.suppress("os_disabled");
                self.sink.fallback(&presentation);
            }
            Err(failure) => {
                self.diagnostics.record(
                    Source::Native,
                    Event::NotificationFailed,
                    json!({ "reason": failure.0 }),
                );
                self.sink.fallback(&presentation);
            }
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

fn reason_label(reason: Reason, chinese: bool) -> &'static str {
    match (reason, chinese) {
        (Reason::Timeout, true) => "超时",
        (Reason::Timeout, false) => "timed out",
        (Reason::Permission, true) => "权限不足",
        (Reason::Permission, false) => "permission denied",
        (Reason::Io, true) => "文件读写失败",
        (Reason::Io, false) => "file access failed",
        (Reason::Invalid, true) => "输入无效",
        (Reason::Invalid, false) => "invalid input",
        (Reason::Dependency, true) => "依赖不可用",
        (Reason::Dependency, false) => "a dependency is unavailable",
        (Reason::Interrupted, true) => "已中断",
        (Reason::Interrupted, false) => "interrupted",
        (Reason::Unknown, true) => "未知错误",
        (Reason::Unknown, false) => "an unknown error",
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
    "en".to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Mutex;

    #[derive(Clone, Default)]
    struct Recording(Arc<Mutex<Vec<Presentation>>>);

    impl Sink for Recording {
        fn available(&mut self) -> bool {
            true
        }
        fn show(&mut self, presentation: &Presentation) -> Result<(), Failure> {
            self.0.lock().unwrap().push(presentation.clone());
            Ok(())
        }
        fn fallback(&mut self, _presentation: &Presentation) {}
        fn shutdown(&mut self) {}
    }

    fn center(sink: Recording) -> Center {
        with_sink(Box::new(sink), Diagnostics::new(None), "test".into(), true)
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
        fn available(&mut self) -> bool {
            false
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
            Box::new(sink.clone()),
            Diagnostics::new(None),
            "test".into(),
            true,
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
}
