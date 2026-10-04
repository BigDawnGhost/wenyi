use tauri::{
    menu::{Menu, MenuItem, Submenu},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Manager,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum CloseAction {
    Hide,
    Minimize,
}

pub fn close_action(tray_created: bool, host_available: bool) -> CloseAction {
    if tray_created && host_available {
        CloseAction::Hide
    } else {
        CloseAction::Minimize
    }
}

pub fn is_background(visible: bool, minimized: bool) -> bool {
    !visible || minimized
}

#[cfg(target_os = "linux")]
fn tray_host_available() -> bool {
    use dbus::blocking::stdintf::org_freedesktop_dbus::Properties;

    // AppIndicator creation can succeed without a desktop tray host (e.g. GNOME
    // without its extension). Fail closed rather than hiding an unreachable window.
    let Ok(connection) = dbus::blocking::Connection::new_session() else {
        return false;
    };
    let deadline = std::time::Instant::now() + std::time::Duration::from_millis(500);
    let watcher = connection.with_proxy(
        "org.kde.StatusNotifierWatcher",
        "/StatusNotifierWatcher",
        deadline.saturating_duration_since(std::time::Instant::now()),
    );
    let host: Result<bool, _> =
        watcher.get("org.kde.StatusNotifierWatcher", "IsStatusNotifierHostRegistered");
    if !matches!(host, Ok(true)) {
        return false;
    }
    let remaining = deadline.saturating_duration_since(std::time::Instant::now());
    if remaining.is_zero() {
        return false;
    }
    let watcher = connection.with_proxy(
        "org.kde.StatusNotifierWatcher",
        "/StatusNotifierWatcher",
        remaining,
    );
    let Ok(items): Result<Vec<String>, _> =
        watcher.get("org.kde.StatusNotifierWatcher", "RegisteredStatusNotifierItems")
    else {
        return false;
    };
    // The watcher alone is insufficient: our icon must have registered successfully.
    items.iter().any(|item| {
        let remaining = deadline.saturating_duration_since(std::time::Instant::now());
        if remaining.is_zero() {
            return false;
        }
        let bus = item.split('/').next().unwrap_or_default();
        let proxy = connection.with_proxy("org.freedesktop.DBus", "/org/freedesktop/DBus", remaining);
        let owner: Result<(u32,), _> =
            proxy.method_call("org.freedesktop.DBus", "GetConnectionUnixProcessID", (bus,));
        owner.is_ok_and(|(pid,)| pid == std::process::id())
    })
}

#[cfg(not(target_os = "linux"))]
fn tray_host_available() -> bool {
    true
}

pub fn recover_if_unreachable(app: &tauri::AppHandle) {
    #[cfg(target_os = "linux")]
    {
        let state = app.state::<std::sync::Arc<std::sync::Mutex<crate::State>>>();
        let background = state.lock().unwrap().background;
        // Run the optional bus probe off the UI thread, only while backgrounded.
        if background && !tray_host_available() {
            let handle = app.clone();
            let _ = app.run_on_main_thread(move || {
                if handle.state::<std::sync::Arc<std::sync::Mutex<crate::State>>>().lock().unwrap().closing {
                    return;
                }
                if let Some(window) = handle.get_webview_window("main") {
                    if window.is_visible().is_ok_and(|visible| !visible) {
                        close(&handle, false);
                    }
                }
            });
        }
    }
    #[cfg(not(target_os = "linux"))]
    let _ = app;
}

pub fn refresh(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        // Query actual native state: focus loss alone is not background.
        if let (Ok(visible), Ok(minimized)) = (window.is_visible(), window.is_minimized()) {
            let state = app.state::<std::sync::Arc<std::sync::Mutex<crate::State>>>();
            let changed = {
                let mut state = state.lock().unwrap();
                let background = is_background(visible, minimized);
                let changed = state.background != background;
                state.background = background;
                changed
            };
            if changed {
                crate::publish_background(app, &state);
            }
        }
    }
}

pub fn restore(app: &tauri::AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        #[cfg(target_os = "linux")]
        // GTK/Wayland may not report compositor-side minimization, and deiconify
        // alone may not restore it. Remap the same window without replacing its WebView.
        if window.hide().is_err() {
            return;
        }
        if window.show().is_ok() && window.unminimize().is_ok() {
            refresh(app);
            let _ = window.set_focus();
        }
    }
}

pub fn close(app: &tauri::AppHandle, tray_available: bool) {
    if let Some(window) = app.get_webview_window("main") {
        let action = close_action(tray_available, tray_available && tray_host_available());
        let hidden = action == CloseAction::Hide && window.hide().is_ok();
        if !hidden {
            // Keep the taskbar/Dock entry usable if a tray cannot be created or hide fails.
            let _ = window.show();
            let _ = window.minimize();
        }
        refresh(app);
    }
}

pub fn install(app: &tauri::AppHandle) -> tauri::Result<bool> {
    let show = MenuItem::with_id(app, "show-wenyi", "Show Wenyi", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit-wenyi", "Quit Wenyi", true, Some("CmdOrCtrl+Q"))?;
    let menu = Menu::with_items(app, &[&show, &quit])?;
    // An explicit exit route is required even when the optional tray fails.
    let application = Submenu::with_items(app, "Wenyi", true, &[&show, &quit])?;
    app.set_menu(Menu::with_items(app, &[&application])?)?;
    app.on_menu_event(|app, event| match event.id().as_ref() {
        "show-wenyi" => restore(app),
        "quit-wenyi" => app.exit(0),
        _ => {}
    });
    let result = (|| -> tauri::Result<()> {
        let mut tray = TrayIconBuilder::with_id("wenyi")
            .tooltip("Wenyi")
            .menu(&menu)
            .show_menu_on_left_click(false)
            .on_tray_icon_event(|tray, event| {
                if matches!(
                    event,
                    TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } | TrayIconEvent::DoubleClick {
                        button: MouseButton::Left,
                        ..
                    }
                ) {
                    restore(tray.app_handle());
                }
            });
        if let Some(icon) = app.default_window_icon() {
            tray = tray.icon(icon.clone());
        } else {
            return Err(tauri::Error::AssetNotFound("tray icon".into()));
        }
        // Linux's dynamically loaded AppIndicator library can panic when absent.
        // Contain only this optional integration, not application setup failures.
        match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| tray.build(app))) {
            Ok(result) => {
                result?;
            }
            Err(_) => return Err(tauri::Error::AssetNotFound("tray integration".into())),
        }
        Ok(())
    })();
    if result.is_err() {
        eprintln!("The Wenyi tray is unavailable; closing will minimize the window.");
    }
    Ok(result.is_ok())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn close_hides_only_with_a_recoverable_tray() {
        assert_eq!(close_action(true, true), CloseAction::Hide);
        assert_eq!(close_action(false, true), CloseAction::Minimize);
        assert_eq!(close_action(true, false), CloseAction::Minimize);
        assert_eq!(close_action(false, false), CloseAction::Minimize);
    }

    #[test]
    fn native_visibility_not_blur_defines_background() {
        assert!(!is_background(true, false));
        assert!(is_background(false, false));
        assert!(is_background(true, true));
        assert!(is_background(false, true));
    }
}
