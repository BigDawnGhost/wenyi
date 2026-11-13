// Keep console diagnostics for development, but ship a Windows GUI application.
#![cfg_attr(all(windows, not(debug_assertions)), windows_subsystem = "windows")]

mod background;
mod diagnostics;
mod diagnostics_ui;
mod graphics;
mod instance;
mod ipc_error;
mod library_choose;
mod local_connection;
mod migration;
mod mutex;
mod native_drop;
mod native_export;
mod paths;
mod process;
mod profile;
mod publish;
mod updates;

use crate::mutex::lock;
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use tauri::{Manager, WebviewUrl, WebviewWindowBuilder};

fn trusted(url: &tauri::Url) -> bool {
    url.username().is_empty()
        && url.password().is_none()
        && url.port().is_none()
        && matches!(
            (url.scheme(), url.host_str()),
            ("tauri", Some("localhost")) | ("http" | "https", Some("tauri.localhost"))
        )
}

#[derive(Default)]
struct State {
    connection: Option<String>,
    error: Option<&'static str>,
    closing: bool,
    background: bool,
}

impl State {
    fn connected(&mut self, connection: String) {
        // A late startup ready signal must not cancel an already requested close.
        self.connection = Some(connection);
        self.error = None;
    }

    fn background_script(&self) -> String {
        format!("window.__WENYI_DESKTOP_BACKGROUND__={};window.dispatchEvent(new Event('wenyi:desktop-visibility'));", self.background)
    }

    fn script(&self) -> String {
        let status: String = if self.closing {
            "window.__WENYI_DESKTOP_CLOSING__=true;window.dispatchEvent(new Event('wenyi:desktop-closing'));".into()
        } else if let Some(error) = self.error {
            format!("window.__WENYI_DESKTOP_ERROR__={};window.dispatchEvent(new CustomEvent('wenyi:desktop-error',{{detail:window.__WENYI_DESKTOP_ERROR__}}));", serde_json::to_string(error).unwrap())
        } else if let Some(connection) = &self.connection {
            format!("window.__WENYI_DESKTOP_CLOSING__=false;window.__WENYI_DESKTOP_ERROR__=undefined;window.__WENYI_DESKTOP_STATUS__=undefined;window.__WENYI_DESKTOP__={connection};window.dispatchEvent(new Event('wenyi:desktop-ready'));")
        } else {
            "window.__WENYI_DESKTOP_PENDING__=true;".into()
        };
        format!("{}{status}", self.background_script())
    }
}

#[derive(Clone)]
struct Engine {
    child: Arc<Mutex<Option<process::Backend>>>,
    workspace: Option<std::path::PathBuf>,
    native_owner: instance::NativeOwner,
    profile: Arc<profile::Profile>,
    closing: Arc<AtomicBool>,
    closed: Arc<AtomicBool>,
}

/// The same launch path is used at startup and to recover a failed installation.
fn start_backend(
    app: &tauri::AppHandle,
    engine: &Engine,
    recovery: bool,
) -> Result<(), &'static str> {
    let diagnostics = app.state::<diagnostics::Diagnostics>();
    let resources = app.path().resource_dir().map_err(|_| {
        process::Failure::Setup("Local resources unavailable.").report(&diagnostics)
    })?;
    let ready = {
        let mut owner = lock(&engine.child);
        if engine.closed.load(Ordering::SeqCst)
            || (!recovery && engine.closing.load(Ordering::SeqCst))
        {
            return Err("Wenyi is closing.");
        }
        if owner.as_mut().is_some_and(|backend| !backend.exited()) {
            return Err("The previous local engine has not stopped. Quit and reopen Wenyi.");
        }
        let (backend, ready) = process::Backend::spawn(
            &resources,
            engine.workspace.as_deref(),
            &engine.native_owner,
            diagnostics.inner().clone(),
        )
        .map_err(|failure| failure.report(&diagnostics))?;
        *owner = Some(backend);
        ready
    };
    match process::wait_ready(&ready, std::time::Duration::from_secs(60)) {
        Ok(connection) => {
            if let Some(backend) = lock(&engine.child).as_mut() {
                backend.mark_ready();
            }
            diagnostics.record(
                diagnostics::Source::Native,
                diagnostics::Event::EngineReady,
                serde_json::json!({}),
            );
            let state = app.state::<Arc<Mutex<State>>>();
            let mut state = lock(&state);
            state.connected(connection);
            Ok(())
        }
        Err(failure) => {
            let message = failure.report(&diagnostics);
            let mut owner = lock(&engine.child);
            if let Some(backend) = owner.as_mut() {
                backend.shutdown();
                if backend.exited() {
                    owner.take();
                }
            }
            Err(message)
        }
    }
}

fn begin_shutdown(explicit_quit: bool, closing: &AtomicBool) -> bool {
    explicit_quit && !closing.swap(true, Ordering::SeqCst)
}

fn publish(app: &tauri::AppHandle, state: &Mutex<State>) {
    if let Some(window) = app.get_webview_window("main") {
        if window.url().is_ok_and(|url| trusted(&url)) {
            let _ = window.eval(lock(state).script());
        }
    }
}

fn publish_background(app: &tauri::AppHandle, state: &Mutex<State>) {
    if let Some(window) = app.get_webview_window("main") {
        if window.url().is_ok_and(|url| trusted(&url)) {
            let _ = window.eval(lock(state).background_script());
        }
    }
}

fn fail(app: &tauri::AppHandle, state: &Mutex<State>, error: &'static str) {
    lock(state).error = Some(error);
    publish(app, state);
}

/// Refuse overlap between the actual executable directory and actual data root,
/// including source, unpacked and --data-dir launches; no registry lookup.
///
/// The log is already open here on purpose. A start that ends at this point has
/// no window, no engine, and no console in a release build, so the file is the
/// only place that can say which folders were compared.
fn assert_layout(
    diagnostics: &diagnostics::Diagnostics,
    workspace: &std::path::Path,
) -> Result<(), &'static str> {
    let running = paths::running_install_dir().map_err(|_| {
        "Wenyi could not determine which folder it is running from. Reinstall Wenyi into a folder of its own."
    })?;
    let layout = paths::check_layout(&running, workspace);
    if layout == paths::LayoutCheck::Consistent {
        return Ok(());
    }
    diagnostics.record(
        diagnostics::Source::Native,
        diagnostics::Event::LayoutRefused,
        serde_json::json!({}),
    );
    diagnostics.drain();
    Err(layout.message().unwrap_or("Wenyi's data folders overlap."))
}

fn application_context() -> tauri::Context<tauri::Wry> {
    // macOS embeds a global Info.plist symbol; expand this macro only once.
    tauri::generate_context!()
}

impl tauri::Resource for instance::Instance {}

/// Report one refusal raised before any window exists.
///
/// A start that ends here has no interface to report in, so the dialog and the
/// console line are the whole report and the diagnostic log carries the record.
fn notice(message: &str) {
    eprintln!("{message}");
    rfd::MessageDialog::new()
        .set_title("Wenyi Desktop")
        .set_description(message)
        .set_level(rfd::MessageLevel::Warning)
        .show();
}

fn instance_error(error: instance::Error) {
    notice(error.message());
}

fn activate_instance(app: &tauri::AppHandle) {
    let handle = app.clone();
    let pending = app.state::<instance::Activation>().ready(Arc::new(move || {
        if handle.state::<Engine>().closing.load(Ordering::SeqCst) {
            return instance::Reply::Closing;
        }
        let (tx, rx) = std::sync::mpsc::channel();
        let activation_handle = handle.clone();
        if handle
            .run_on_main_thread(move || {
                let reply = if activation_handle
                    .state::<Engine>()
                    .closing
                    .load(Ordering::SeqCst)
                {
                    instance::Reply::Closing
                } else if background::try_restore(&activation_handle) {
                    instance::Reply::Activated
                } else {
                    instance::Reply::Unreachable
                };
                let _ = tx.send(reply);
            })
            .is_err()
        {
            return instance::Reply::Unreachable;
        }
        rx.recv_timeout(std::time::Duration::from_secs(2))
            .unwrap_or(instance::Reply::Unreachable)
    }));
    if pending && !background::try_restore(app) {
        instance_error(instance::Error::Unreachable);
    }
}

fn main() {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    let data_dir = match args.as_slice() {
        [] => None,
        [flag, path] if flag == "--data-dir" => Some(std::path::PathBuf::from(path)),
        [flag] if flag == "--version" || flag == "-V" => {
            println!("Wenyi Desktop {}", env!("WENYI_DESKTOP_VERSION"));
            return;
        }
        [flag] if flag == "--help" || flag == "-h" => {
            println!("Usage: wenyi-desktop [--data-dir <isolated-desktop-workspace>] [--version]");
            return;
        }
        _ => {
            eprintln!("Unexpected arguments. See --help.");
            std::process::exit(2);
        }
    };
    let workspace = match instance::workspace(data_dir) {
        Ok(workspace) => workspace,
        Err(error) => {
            instance_error(error);
            diagnostics_ui::startup_failure(&diagnostics::Diagnostics::new(None));
            return;
        }
    };
    let primary = match instance::enter(&workspace) {
        Ok(Some(primary)) => primary,
        Ok(None) => return,
        Err(error) => {
            instance_error(error);
            diagnostics_ui::startup_failure(&diagnostics::Diagnostics::new(None));
            return;
        }
    };
    let diagnostics = diagnostics::Diagnostics::for_workspace(&workspace);
    let profile = match profile::Profile::enter(&paths::locks(&workspace)) {
        Ok(profile) => Arc::new(profile),
        Err(message) => {
            notice(&message);
            diagnostics_ui::startup_failure(&diagnostics);
            return;
        }
    };
    diagnostics.record(
        diagnostics::Source::Native,
        diagnostics::Event::AppStarted,
        serde_json::json!({}),
    );
    let panic_diagnostics = diagnostics.clone();
    let previous_panic = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        panic_diagnostics.record(
            diagnostics::Source::Native,
            diagnostics::Event::NativePanic,
            serde_json::json!({}),
        );
        previous_panic(info);
    }));
    // Refusing to start is the whole point of the assertion: a layout that would
    // let one cleanup reach another layer is not a state this build runs in. The
    // check runs before any window exists, so the reason is shown instead of an
    // interface that fails later for a cause nothing connects back to this.
    if let Err(error) = assert_layout(&diagnostics, &workspace) {
        notice(error);
        diagnostics_ui::startup_failure(&diagnostics);
        return;
    }
    graphics::configure();
    let state = Arc::new(Mutex::new(State::default()));
    let closing = Arc::new(AtomicBool::new(false));
    let closed = Arc::new(AtomicBool::new(false));
    let child = Arc::new(Mutex::new(None::<process::Backend>));
    let engine = Engine {
        child: child.clone(),
        workspace: Some(workspace.clone()),
        native_owner: primary.owner.clone(),
        profile,
        closing: closing.clone(),
        closed: closed.clone(),
    };
    let startup_engine = engine.clone();
    let startup_state = state.clone();
    let tray_available = Arc::new(AtomicBool::new(false));
    let setup_tray = tray_available.clone();
    let app = tauri::Builder::default()
        .manage(primary.activation.clone())
        .manage(state.clone())
        .manage(diagnostics.clone())
        .manage(Arc::new(diagnostics_ui::ExportControl::default()))
        .manage(engine)
        .manage(updates::Updates::new())
        .plugin(tauri_plugin_updater::Builder::new()
            .pubkey(env!("WENYI_UPDATER_PUBLIC_KEY"))
            .build())
        .manage(native_drop::Grants::default())
        .manage(native_export::Saves::default())
        .invoke_handler(tauri::generate_handler![
            library_choose::native_library_choose,
            native_drop::native_drop_release,
            native_drop::native_drop_upload,
            native_export::native_export_save,
            native_export::native_glossary_save,
            updates::desktop_update_status,
            updates::desktop_update_check,
            updates::desktop_update_download,
            updates::desktop_update_install,
            diagnostics_ui::desktop_diagnostics_status,
            diagnostics_ui::desktop_diagnostics_record,
            diagnostics_ui::desktop_diagnostics_detail,
            diagnostics_ui::desktop_diagnostics_export,
            diagnostics_ui::desktop_diagnostics_cancel,
            diagnostics_ui::desktop_diagnostics_clear,
            diagnostics_ui::desktop_diagnostics_open,
        ])
        .on_window_event(|window, event| {
            // A WebviewWindow's native drops are emitted as window events.
            if let tauri::WindowEvent::DragDrop(event) = event {
                if let Some(window) = window.app_handle().get_webview_window(window.label()) {
                    native_drop::event(&window, event);
                }
            }
        })
        .setup(move |app| {
            // Tauri closes resources before exit/restart, after our engine cleanup.
            app.resources_table().add(primary);
            app.state::<native_drop::Grants>().start_expiry();
            let reload_state = startup_state.clone();
            let webview_data = paths::webview(&workspace);
            std::fs::create_dir_all(&webview_data)?;
            // Overlap Python startup with WebView creation. State retains an
            // early ready/error result for the initialization/page-load scripts;
            // the UI still cannot issue requests before the owned engine is ready.
            let handle = app.handle().clone();
            let backend_engine = startup_engine.clone();
            let backend_state = startup_state.clone();
            std::thread::spawn(move || {
                if backend_engine.closing.load(Ordering::SeqCst) { return; }
                if let Err(error) = start_backend(&handle, &backend_engine, false) {
                    fail(&handle, &backend_state, error);
                    return;
                }
                publish(&handle, &backend_state);
            });
            // The shared shell reserves 752 logical px for content plus the 240 px
            // expanded sidebar, so a narrower minimum would clip settings behind a
            // horizontal scrollbar; 1024 leaves room for both scrollbar styles.
            WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("Wenyi").inner_size(1280.0, 850.0).min_inner_size(1024.0, 600.0)
                .data_directory(webview_data)
                .initialization_script(lock(&startup_state).script())
                .on_navigation(trusted)
                .on_new_window(|_, _| tauri::webview::NewWindowResponse::Deny)
                .on_page_load(move |window, payload| {
                    if trusted(payload.url()) {
                        window.state::<native_drop::Grants>().clear();
                        background::refresh(window.app_handle());
                        let _ = window.eval(lock(&reload_state).script());
                    }
                })
                .build()?;
            setup_tray.store(background::install(app.handle())?, Ordering::SeqCst);
            let handle = app.handle().clone();
            updates::check_on_startup(&handle);
            let monitor_handle = handle.clone();
            let monitor_engine = startup_engine.clone();
            let monitor_state = startup_state.clone();
            // Keep the existing health cadence alive even during startup or after
            // engine failure, so minimize-only platforms still report visibility.
            std::thread::spawn(move || {
                let mut reported_exit = false;
                let mut health_ticks = 0;
                while !monitor_engine.closed.load(Ordering::SeqCst) {
                    // Updates temporarily own closing and can restore this same engine.
                    if monitor_engine.closing.load(Ordering::SeqCst) {
                        reported_exit = false;
                        std::thread::sleep(std::time::Duration::from_millis(250));
                        continue;
                    }
                    let visibility_handle = monitor_handle.clone();
                    let _ = monitor_handle.run_on_main_thread(move || background::refresh(&visibility_handle));
                    health_ticks += 1;
                    if health_ticks == 8 {
                        health_ticks = 0;
                        background::recover_if_unreachable(&monitor_handle);
                    }
                    if !reported_exit && lock(&monitor_engine.child).as_mut().is_some_and(|backend| backend.is_ready() && backend.exited()) {
                        reported_exit = true;
                        monitor_handle.state::<diagnostics::Diagnostics>().record(
                            diagnostics::Source::Native, diagnostics::Event::EngineFailed,
                            serde_json::json!({"stage": "running", "reason": "process_exited"}),
                        );
                        fail(&monitor_handle, &monitor_state, "The local engine stopped unexpectedly. Quit and reopen Wenyi to recover saved work.");
                    }
                    std::thread::sleep(std::time::Duration::from_millis(250));
                }
            });
            Ok(())
        })
        .build(application_context());
    let app = match app {
        Ok(app) => app,
        Err(_) => {
            // Setup may fail after spawning, before any event-loop shutdown
            // handler exists. Close admission first, then take the same ownership
            // mutex used by start_backend so a late spawn cannot escape cleanup.
            closing.store(true, Ordering::SeqCst);
            closed.store(true, Ordering::SeqCst);
            if let Some(mut backend) = lock(&child).take() {
                backend.shutdown();
            }
            diagnostics_ui::startup_failure(&diagnostics);
            return;
        }
    };
    app.run(move |handle, event_kind| {
        let requested = match event_kind {
            tauri::RunEvent::Ready => {
                activate_instance(handle);
                false
            }
            tauri::RunEvent::WindowEvent {
                label,
                event: tauri::WindowEvent::CloseRequested { api, .. },
                ..
            } if label == "main" => {
                api.prevent_close();
                if !closing.load(Ordering::SeqCst) {
                    background::close(handle, tray_available.load(Ordering::SeqCst));
                }
                false
            }
            tauri::RunEvent::ExitRequested { api, .. } if !closed.load(Ordering::SeqCst) => {
                api.prevent_exit();
                true
            }
            tauri::RunEvent::WindowEvent { .. } => {
                background::refresh(handle);
                false
            }
            #[cfg(target_os = "macos")]
            tauri::RunEvent::Reopen { .. } => {
                background::restore(handle);
                false
            }
            _ => false,
        };
        if begin_shutdown(requested, &closing) {
            handle.state::<native_drop::Grants>().clear();
            handle.state::<native_export::Saves>().close();
            lock(&state).closing = true;
            publish(handle, &state);
            let child = child.clone();
            let closed = closed.clone();
            let handle = handle.clone();
            std::thread::spawn(move || {
                handle.state::<native_export::Saves>().wait();
                if let Some(mut backend) = lock(&child).take() {
                    backend.shutdown();
                }
                let diagnostics = handle.state::<diagnostics::Diagnostics>();
                diagnostics.record(
                    diagnostics::Source::Native,
                    diagnostics::Event::AppStopped,
                    serde_json::json!({}),
                );
                diagnostics.drain();
                closed.store(true, Ordering::SeqCst);
                handle.exit(0);
            });
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn tauri_application_version_matches_native_version_report() {
        let context = application_context();
        assert_eq!(
            context.package_info().version.to_string(),
            env!("WENYI_DESKTOP_VERSION")
        );
    }

    #[test]
    fn tauri_resource_cleanup_releases_instance_for_restart() {
        let mut id = [0u8; 16];
        getrandom::fill(&mut id).unwrap();
        let path = std::env::temp_dir().join(format!("wenyi-resource-{id:x?}"));
        std::fs::create_dir(&path).unwrap();
        let mut resources = tauri::ResourceTable::default();
        let primary = instance::enter(&path).unwrap().unwrap();
        let rid = resources.add(primary);
        assert!(instance::enter(&path).unwrap().is_none());
        resources.close(rid).unwrap();
        let replacement = instance::enter(&path).unwrap().unwrap();
        drop(replacement);
        std::fs::remove_dir_all(&path).unwrap();
    }

    #[test]
    fn only_packaged_navigation_can_receive_credentials() {
        for url in ["tauri://localhost/index.html", "http://tauri.localhost/"] {
            assert!(trusted(&tauri::Url::parse(url).unwrap()));
        }
        for url in [
            "https://evil.example/",
            "http://127.0.0.1:5173/",
            "https://tauri.localhost.evil.example/",
            "https://user@tauri.localhost/",
            "https://tauri.localhost:8080/",
            "file:///tmp/index.html",
        ] {
            assert!(!trusted(&tauri::Url::parse(url).unwrap()));
        }
    }
    #[test]
    fn closing_and_errors_override_ready_on_reload() {
        let mut state = State {
            connection: Some("secret".into()),
            ..Default::default()
        };
        state.error = Some("Safe failure");
        assert!(!state.script().contains("secret"));
        assert!(state.script().contains("__WENYI_DESKTOP_ERROR__"));
        state.closing = true;
        assert!(state.script().contains("__WENYI_DESKTOP_CLOSING__"));
        assert!(!state.script().contains("secret"));
    }

    #[test]
    fn late_ready_does_not_reopen_a_closing_application() {
        let mut state = State {
            closing: true,
            background: true,
            ..Default::default()
        };
        state.connected("secret".into());
        assert!(state.closing);
        assert!(state.script().contains("__WENYI_DESKTOP_CLOSING__"));
        assert!(!state.script().contains("secret"));
        // Recovery publishes new credentials only after lifecycle admission reopens.
        state.closing = false;
        assert!(state.script().contains("secret"));
        assert!(state
            .script()
            .contains("__WENYI_DESKTOP_BACKGROUND__=true;"));
    }

    #[test]
    fn close_does_not_shutdown_and_explicit_quit_starts_only_once() {
        let closing = AtomicBool::new(false);
        assert!(!begin_shutdown(false, &closing));
        assert!(!closing.load(Ordering::SeqCst));
        assert!(begin_shutdown(true, &closing));
        assert!(!begin_shutdown(true, &closing));
    }

    #[test]
    fn reload_replays_latest_background_in_every_state() {
        let mut state = State::default();
        assert!(state
            .script()
            .contains("__WENYI_DESKTOP_BACKGROUND__=false;"));
        state.background = true;
        for status in 0..4 {
            state.connection = (status == 1).then(|| "{}".into());
            state.error = (status == 2).then_some("Safe failure");
            state.closing = status == 3;
            assert!(state
                .script()
                .contains("__WENYI_DESKTOP_BACKGROUND__=true;"));
            assert!(state.script().contains("wenyi:desktop-visibility"));
        }
    }

    #[test]
    fn visibility_changes_do_not_replay_connection_or_closing_events() {
        let state = State {
            connection: Some("secret".into()),
            closing: true,
            background: true,
            ..Default::default()
        };
        assert_eq!(state.background_script(), "window.__WENYI_DESKTOP_BACKGROUND__=true;window.dispatchEvent(new Event('wenyi:desktop-visibility'));");
    }
}
