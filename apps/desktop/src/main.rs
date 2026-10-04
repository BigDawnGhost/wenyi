// Keep console diagnostics for development, but ship a Windows GUI application.
#![cfg_attr(all(windows, not(debug_assertions)), windows_subsystem = "windows")]

mod background;
mod graphics;
mod native_drop;
mod native_export;
mod process;

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
    fn background_script(&self) -> String {
        format!("window.__WENYI_DESKTOP_BACKGROUND__={};window.dispatchEvent(new Event('wenyi:desktop-visibility'));", self.background)
    }

    fn script(&self) -> String {
        let status: String = if self.closing {
            "window.__WENYI_DESKTOP_CLOSING__=true;window.dispatchEvent(new Event('wenyi:desktop-closing'));".into()
        } else if let Some(error) = self.error {
            format!("window.__WENYI_DESKTOP_ERROR__={};window.dispatchEvent(new CustomEvent('wenyi:desktop-error',{{detail:window.__WENYI_DESKTOP_ERROR__}}));", serde_json::to_string(error).unwrap())
        } else if let Some(connection) = &self.connection {
            format!("window.__WENYI_DESKTOP__={connection};window.dispatchEvent(new Event('wenyi:desktop-ready'));")
        } else {
            "window.__WENYI_DESKTOP_PENDING__=true;".into()
        };
        format!("{}{status}", self.background_script())
    }
}

fn begin_shutdown(explicit_quit: bool, closing: &AtomicBool) -> bool {
    explicit_quit && !closing.swap(true, Ordering::SeqCst)
}

fn publish(app: &tauri::AppHandle, state: &Mutex<State>) {
    if let Some(window) = app.get_webview_window("main") {
        if window.url().is_ok_and(|url| trusted(&url)) {
            let _ = window.eval(state.lock().unwrap().script());
        }
    }
}

fn publish_background(app: &tauri::AppHandle, state: &Mutex<State>) {
    if let Some(window) = app.get_webview_window("main") {
        if window.url().is_ok_and(|url| trusted(&url)) {
            let _ = window.eval(state.lock().unwrap().background_script());
        }
    }
}

fn fail(app: &tauri::AppHandle, state: &Mutex<State>, error: &'static str) {
    state.lock().unwrap().error = Some(error);
    publish(app, state);
}

fn application_context() -> tauri::Context<tauri::Wry> {
    // macOS embeds a global Info.plist symbol; expand this macro only once.
    tauri::generate_context!()
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
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
    graphics::configure();
    let state = Arc::new(Mutex::new(State::default()));
    let closing = Arc::new(AtomicBool::new(false));
    let closed = Arc::new(AtomicBool::new(false));
    let child = Arc::new(Mutex::new(None::<process::Backend>));
    let startup_child = child.clone();
    let startup_closing = closing.clone();
    let startup_state = state.clone();
    let tray_available = Arc::new(AtomicBool::new(false));
    let setup_tray = tray_available.clone();
    let app = tauri::Builder::default()
        .manage(state.clone())
        .manage(native_drop::Grants::default())
        .manage(native_export::Saves::default())
        .invoke_handler(tauri::generate_handler![
            native_drop::native_drop_release,
            native_drop::native_drop_upload,
            native_export::native_export_save,
        ])
        .on_webview_event(|window, event| {
            if let tauri::WebviewEvent::DragDrop(event) = event {
                if let Some(window) = window.app_handle().get_webview_window(window.label()) {
                    native_drop::event(&window, event);
                }
            }
        })
        .setup(move |app| {
            app.state::<native_drop::Grants>().start_expiry();
            let reload_state = startup_state.clone();
            WebviewWindowBuilder::new(app, "main", WebviewUrl::App("index.html".into()))
                .title("Wenyi").inner_size(1280.0, 850.0).min_inner_size(800.0, 600.0)
                .initialization_script(startup_state.lock().unwrap().script())
                .on_navigation(trusted)
                .on_new_window(|_, _| tauri::webview::NewWindowResponse::Deny)
                .on_page_load(move |window, payload| {
                    if trusted(payload.url()) {
                        window.state::<native_drop::Grants>().clear();
                        background::refresh(window.app_handle());
                        let _ = window.eval(reload_state.lock().unwrap().script());
                    }
                })
                .build()?;
            setup_tray.store(background::install(app.handle())?, Ordering::SeqCst);
            let handle = app.handle().clone();
            let resources = app.path().resource_dir()?;
            let monitor_handle = handle.clone();
            let monitor_child = startup_child.clone();
            let monitor_closing = startup_closing.clone();
            let monitor_state = startup_state.clone();
            // Keep the existing health cadence alive even during startup or after
            // engine failure, so minimize-only platforms still report visibility.
            std::thread::spawn(move || {
                let mut reported_exit = false;
                let mut health_ticks = 0;
                while !monitor_closing.load(Ordering::SeqCst) {
                    let visibility_handle = monitor_handle.clone();
                    let _ = monitor_handle.run_on_main_thread(move || background::refresh(&visibility_handle));
                    health_ticks += 1;
                    if health_ticks == 8 {
                        health_ticks = 0;
                        background::recover_if_unreachable(&monitor_handle);
                    }
                    if !reported_exit && monitor_child.lock().unwrap().as_mut().is_some_and(|backend| backend.exited()) {
                        reported_exit = true;
                        fail(&monitor_handle, &monitor_state, "The local engine stopped unexpectedly. Quit and reopen Wenyi to recover saved work.");
                    }
                    std::thread::sleep(std::time::Duration::from_millis(250));
                }
            });
            std::thread::spawn(move || {
                // Serialize spawning with close so quit never outruns child ownership.
                let ready = {
                    let mut owner = startup_child.lock().unwrap();
                    if startup_closing.load(Ordering::SeqCst) { return; }
                    match process::Backend::spawn(&resources, data_dir.as_deref()) {
                        Ok((backend, ready)) => { *owner = Some(backend); ready }
                        Err(_) => {
                            fail(&handle, &startup_state, "The local engine could not start. Quit and reopen Wenyi to retry.");
                            return;
                        }
                    }
                };
                match ready.recv_timeout(std::time::Duration::from_secs(60)) {
                    Ok(Ok(connection)) => {
                        startup_state.lock().unwrap().connection = Some(connection);
                        publish(&handle, &startup_state);
                    }
                    _ => {
                        fail(&handle, &startup_state, "The local engine did not become ready. Quit and reopen Wenyi to retry.");
                        if let Some(mut backend) = startup_child.lock().unwrap().take() { backend.shutdown(); }
                        return;
                    }
                }
            });
            Ok(())
        })
        .build(application_context())
        .expect("Could not build the Wenyi desktop window");
    app.run(move |handle, event_kind| {
        let requested = match event_kind {
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
            state.lock().unwrap().closing = true;
            publish(handle, &state);
            let child = child.clone();
            let closed = closed.clone();
            let handle = handle.clone();
            std::thread::spawn(move || {
                handle.state::<native_export::Saves>().wait();
                if let Some(mut backend) = child.lock().unwrap().take() {
                    backend.shutdown();
                }
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
        assert!(state.script().contains("__WENYI_DESKTOP_BACKGROUND__=false;"));
        state.background = true;
        for status in 0..4 {
            state.connection = (status == 1).then(|| "{}".into());
            state.error = (status == 2).then_some("Safe failure");
            state.closing = status == 3;
            assert!(state.script().contains("__WENYI_DESKTOP_BACKGROUND__=true;"));
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
