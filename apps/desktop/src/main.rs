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
}

impl State {
    fn script(&self) -> String {
        if self.closing {
            "window.__WENYI_DESKTOP_CLOSING__=true;window.dispatchEvent(new Event('wenyi:desktop-closing'));".into()
        } else if let Some(error) = self.error {
            format!("window.__WENYI_DESKTOP_ERROR__={};window.dispatchEvent(new CustomEvent('wenyi:desktop-error',{{detail:window.__WENYI_DESKTOP_ERROR__}}));", serde_json::to_string(error).unwrap())
        } else if let Some(connection) = &self.connection {
            format!("window.__WENYI_DESKTOP__={connection};window.dispatchEvent(new Event('wenyi:desktop-ready'));")
        } else {
            "window.__WENYI_DESKTOP_PENDING__=true;".into()
        }
    }
}

fn publish(app: &tauri::AppHandle, state: &Mutex<State>) {
    if let Some(window) = app.get_webview_window("main") {
        if window.url().is_ok_and(|url| trusted(&url)) {
            let _ = window.eval(state.lock().unwrap().script());
        }
    }
}

fn fail(app: &tauri::AppHandle, state: &Mutex<State>, error: &'static str) {
    state.lock().unwrap().error = Some(error);
    publish(app, state);
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
                .initialization_script("window.__WENYI_DESKTOP_PENDING__=true;")
                .on_navigation(trusted)
                .on_new_window(|_, _| tauri::webview::NewWindowResponse::Deny)
                .on_page_load(move |window, payload| {
                    if trusted(payload.url()) {
                        window.state::<native_drop::Grants>().clear();
                        let _ = window.eval(reload_state.lock().unwrap().script());
                    }
                })
                .build()?;
            let handle = app.handle().clone();
            let resources = app.path().resource_dir()?;
            std::thread::spawn(move || {
                // Serialize spawning with close so quit never outruns child ownership.
                let ready = {
                    let mut owner = startup_child.lock().unwrap();
                    if startup_closing.load(Ordering::SeqCst) { return; }
                    match process::Backend::spawn(&resources, data_dir.as_deref()) {
                        Ok((backend, ready)) => { *owner = Some(backend); ready }
                        Err(_) => {
                            fail(&handle, &startup_state, "The local engine could not start. Close and reopen Wenyi to retry.");
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
                        fail(&handle, &startup_state, "The local engine did not become ready. Close and reopen Wenyi to retry.");
                        if let Some(mut backend) = startup_child.lock().unwrap().take() { backend.shutdown(); }
                        return;
                    }
                }
                while !startup_closing.load(Ordering::SeqCst) {
                    let exited = startup_child.lock().unwrap().as_mut().is_some_and(|backend| backend.exited());
                    if exited {
                        fail(&handle, &startup_state, "The local engine stopped unexpectedly. Close and reopen Wenyi to recover saved work.");
                        break;
                    }
                    std::thread::sleep(std::time::Duration::from_millis(250));
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("Could not build the Wenyi desktop window");
    app.run(move |handle, event_kind| {
        let requested = match event_kind {
            tauri::RunEvent::WindowEvent {
                event: tauri::WindowEvent::CloseRequested { api, .. },
                ..
            } => {
                api.prevent_close();
                true
            }
            tauri::RunEvent::ExitRequested { api, .. } if !closed.load(Ordering::SeqCst) => {
                api.prevent_exit();
                true
            }
            _ => false,
        };
        if requested && !closing.swap(true, Ordering::SeqCst) {
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
        let context: tauri::Context<tauri::Wry> = tauri::generate_context!();
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
}
