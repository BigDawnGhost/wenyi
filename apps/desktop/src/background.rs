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

fn hide_window_menu_on(platform: &str) -> bool {
    matches!(platform, "linux" | "windows")
}

#[cfg(target_os = "linux")]
fn hide_gtk_menu_bar(container: &gtk::Box) {
    use gtk::prelude::*;

    for child in container.children() {
        if let Ok(menu_bar) = child.downcast::<gtk::MenuBar>() {
            // Tao remaps windows with show_all(), which would undo hide() alone.
            // Keep the menu attached so its keyboard accelerators remain registered.
            menu_bar.set_no_show_all(true);
            menu_bar.hide();
            // GTK otherwise rejects accelerators whose menu bar is not visible.
            menu_bar.connect_can_activate_accel(|menu_bar, _| menu_bar.is_sensitive());
        }
    }
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
    let _ = try_restore(app);
}

pub fn try_restore(app: &tauri::AppHandle) -> bool {
    if let Some(window) = app.get_webview_window("main") {
        #[cfg(target_os = "linux")]
        // GTK/Wayland may not report compositor-side minimization, and deiconify
        // alone may not restore it. Remap the same window without replacing its WebView.
        if window.hide().is_err() {
            return false;
        }
        if window.show().is_ok() && window.unminimize().is_ok() {
            refresh(app);
            // Some window managers deny focus stealing even after a successful show.
            let _ = window.set_focus();
            return true;
        }
    }
    false
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
    let diagnostics = MenuItem::with_id(app, "wenyi-diagnostics", "Export diagnostic report…", true, Some("CmdOrCtrl+Shift+D"))?;
    let menu = Menu::with_items(app, &[&show, &diagnostics, &quit])?;
    // Always install the application menu and its keyboard quit accelerator.
    let application = Submenu::with_items(app, "Wenyi", true, &[&show, &diagnostics, &quit])?;
    app.set_menu(Menu::with_items(app, &[&application])?)?;
    if hide_window_menu_on(std::env::consts::OS) {
        if let Some(window) = app.get_webview_window("main") {
            // Hide the embedded bar without unregistering the Quit accelerator.
            // macOS keeps its system application menu.
            window.hide_menu()?;
            #[cfg(target_os = "linux")]
            hide_gtk_menu_bar(&window.default_vbox()?);
        }
    }
    app.on_menu_event(|app, event| match event.id().as_ref() {
        "show-wenyi" => restore(app),
        "quit-wenyi" => app.exit(0),
        "wenyi-diagnostics" => crate::diagnostics_ui::export_from_menu(app),
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
    fn embedded_menu_stays_hidden_independently_of_tray_availability() {
        // Hiding is an OS policy, not conditional on tray creation or its host.
        assert!(hide_window_menu_on("linux"));
        assert!(hide_window_menu_on("windows"));
        assert!(!hide_window_menu_on("macos"));
    }

    #[cfg(target_os = "linux")]
    #[test]
    #[ignore = "requires a GTK display; run explicitly with xvfb-run"]
    fn gtk_menu_stays_hidden_after_repeated_window_remaps() {
        use gtk::prelude::*;
        use std::{cell::Cell, rc::Rc};

        gtk::init().expect("GTK display required");
        let window = gtk::Window::new(gtk::WindowType::Toplevel);
        window.set_focus_on_map(false);
        let container = gtk::Box::new(gtk::Orientation::Vertical, 0);
        let menu_bar = gtk::MenuBar::new();
        let application = gtk::MenuItem::with_label("Wenyi");
        let menu = gtk::Menu::new();
        let quit = gtk::MenuItem::with_label("Quit Wenyi");
        menu.append(&quit);
        application.set_submenu(Some(&menu));
        menu_bar.append(&application);
        let content = gtk::Label::new(Some("Window content"));
        container.pack_start(&menu_bar, false, false, 0);
        container.pack_start(&content, true, true, 0);
        window.add(&container);

        let activations = Rc::new(Cell::new(0));
        let activated = activations.clone();
        quit.connect_activate(move |_| activated.set(activated.get() + 1));
        let accelerators = gtk::AccelGroup::new();
        window.add_accel_group(&accelerators);
        let (key, modifiers) = gtk::accelerator_parse("<Control>q");
        quit.add_accelerator(
            "activate",
            &accelerators,
            key,
            modifiers,
            gtk::AccelFlags::VISIBLE,
        );
        window.show_all();
        // Submenu items are shown separately, as they are when Tauri installs a menu.
        menu.show_all();
        hide_gtk_menu_bar(&container);
        assert!(!menu_bar.is_visible());

        for cycle in 1..=3 {
            window.hide();
            // Tao uses this recursive GTK call to show/remap the same native window.
            window.show_all();
            assert!(
                !menu_bar.is_visible(),
                "menu reappeared after remap {cycle}"
            );
            assert!(content.is_visible());
            assert!(gtk::accel_groups_activate(&window, key, modifiers));
            assert_eq!(activations.get(), cycle);
        }
        quit.set_sensitive(false);
        assert!(!gtk::accel_groups_activate(&window, key, modifiers));
        assert_eq!(activations.get(), 3);
        window.close();
    }

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
