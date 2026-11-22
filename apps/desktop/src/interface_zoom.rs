use std::{
    path::{Path, PathBuf},
    sync::{mpsc, Mutex},
};

use serde::{Deserialize, Serialize};
use tauri::{webview::PlatformWebview, Manager, WebviewWindow};

use crate::{ipc_error::IpcError, mutex::lock, native_preferences, trusted};

const FILE: &str = "interface-zoom.json";
const DEFAULT: u8 = 100;
const PRESETS: [u8; 5] = [80, 90, DEFAULT, 110, 125];

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct Document {
    percent: u8,
}

pub struct InterfaceZoom {
    root: PathBuf,
    operation: Mutex<()>,
}

impl InterfaceZoom {
    pub fn new(root: &Path) -> Self {
        Self {
            root: root.to_path_buf(),
            operation: Mutex::new(()),
        }
    }

    fn saved_percent(&self) -> u8 {
        native_preferences::read::<Document>(&self.root, FILE)
            .ok()
            .map(|document| document.percent)
            .filter(|percent| PRESETS.contains(percent))
            .unwrap_or(DEFAULT)
    }

    pub fn restore(&self, window: &WebviewWindow) {
        if supported() {
            let percent = self.saved_percent();
            // Setup is on the UI thread. Apply there before showing the window;
            // never wait for a worker (which could itself be waiting for the UI).
            let _ = window.with_webview(move |view| {
                if set_confirmed(percent, |factor| native_zoom(&view, Some(factor))).is_err() {
                    let _ = native_zoom(&view, Some(1.0));
                }
            });
        }
    }

    fn request(
        &self,
        percent: Option<u8>,
        access: impl Fn(Option<f64>) -> Result<f64, IpcError>,
    ) -> Result<u8, IpcError> {
        // Only IPC workers take this lock. Reads cannot observe a tentative
        // scale between native application, persistence and possible rollback.
        let _guard = lock(&self.operation);
        let Some(percent) = percent else {
            return preset_for(access(None)?).ok_or_else(zoom_error);
        };
        if !PRESETS.contains(&percent) {
            return Err(IpcError::new(
                "zoom_invalid_percent",
                "Choose a supported interface zoom percentage.",
            ));
        }
        let previous = access(None)?;
        let result = set_confirmed(percent, |factor| access(Some(factor))).and_then(|()| {
            native_preferences::save(&self.root, FILE, &Document { percent })
                .map_err(|_| IpcError::new("zoom_save_failed", "Could not save interface zoom."))
        });
        if let Err(error) = result {
            let _ = access(Some(previous));
            return Err(error);
        }
        Ok(percent)
    }
}

fn zoom_error() -> IpcError {
    IpcError::new("zoom_apply_failed", "Could not confirm interface zoom.")
}

fn preset_for(factor: f64) -> Option<u8> {
    // Some engines store the level at single precision.
    PRESETS
        .into_iter()
        .find(|&p| (factor * 100.0 - f64::from(p)).abs() < 0.01)
}

fn set_confirmed(
    percent: u8,
    set: impl FnOnce(f64) -> Result<f64, IpcError>,
) -> Result<(), IpcError> {
    let actual = set(f64::from(percent) / 100.0)?;
    if preset_for(actual) == Some(percent) {
        Ok(())
    } else {
        Err(zoom_error())
    }
}

fn supported() -> bool {
    #[cfg(target_os = "macos")]
    {
        // WKWebView's pageZoom selector does not exist before macOS 11.
        objc2_foundation::NSProcessInfo::processInfo()
            .operatingSystemVersion()
            .majorVersion
            >= 11
    }
    #[cfg(not(target_os = "macos"))]
    {
        cfg!(any(windows, target_os = "linux"))
    }
}

// Tauri's set_zoom acknowledges dispatch, not the WebView's result. Keep the
// setter and its readback in one UI-thread callback, on the same native view.
fn native_zoom(view: &PlatformWebview, set: Option<f64>) -> Result<f64, IpcError> {
    #[cfg(target_os = "linux")]
    {
        use webkit2gtk::WebViewExt;
        let webview = view.inner();
        if let Some(factor) = set {
            webview.set_zoom_level(factor);
        }
        Ok(webview.zoom_level())
    }
    #[cfg(windows)]
    {
        let controller = view.controller();
        let mut actual = 0.0;
        // SAFETY: with_webview supplies the controller on its owning thread.
        unsafe {
            if let Some(factor) = set {
                controller.SetZoomFactor(factor).map_err(|_| zoom_error())?;
            }
            controller
                .ZoomFactor(&mut actual)
                .map_err(|_| zoom_error())?;
        }
        Ok(actual)
    }
    #[cfg(target_os = "macos")]
    {
        // SAFETY: Tauri lends its live WKWebView for this main-thread callback.
        // Both entry points check macOS 11+ before accessing pageZoom.
        unsafe {
            let webview = &*view.inner().cast::<objc2_web_kit::WKWebView>();
            if let Some(factor) = set {
                webview.setPageZoom(factor);
            }
            Ok(webview.pageZoom())
        }
    }
    #[cfg(not(any(windows, target_os = "linux", target_os = "macos")))]
    {
        let _ = (view, set);
        Err(zoom_error())
    }
}

#[tauri::command]
pub async fn desktop_interface_zoom(
    window: WebviewWindow,
    percent: Option<u8>,
) -> Result<Option<u8>, IpcError> {
    if window.label() != "main" || !window.url().is_ok_and(|url| trusted(&url)) {
        return Err(IpcError::new(
            "zoom_untrusted_window",
            "Interface zoom is unavailable.",
        ));
    }
    if !supported() {
        return Ok(None);
    }

    tauri::async_runtime::spawn_blocking(move || {
        let zoom = window.state::<InterfaceZoom>();
        zoom.request(percent, |set| {
            let (tx, rx) = mpsc::channel();
            window
                .with_webview(move |view| {
                    let _ = tx.send(native_zoom(&view, set));
                })
                .map_err(|_| zoom_error())?;
            rx.recv().map_err(|_| zoom_error())?
        })
        .map(Some)
    })
    .await
    .map_err(|_| IpcError::new("zoom_unavailable", "Interface zoom is unavailable."))?
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{cell::Cell, fs};

    struct Workspace(PathBuf);

    impl Workspace {
        fn new() -> Self {
            let id = getrandom::u64().unwrap();
            let path = std::env::temp_dir().join(format!("wenyi-interface-zoom-{id:016x}"));
            fs::create_dir(&path).unwrap();
            Self(path)
        }
    }

    impl Drop for Workspace {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    #[test]
    fn saves_zoom_and_restores_it_only_for_the_same_data_root() {
        let first = Workspace::new();
        let second = Workspace::new();
        let zoom = InterfaceZoom::new(&first.0);
        assert_eq!(zoom.saved_percent(), 100);

        let applied = Cell::new(1.0);
        for percent in [80, 90, 110, 125, 100] {
            assert_eq!(
                zoom.request(Some(percent), |value| Ok(view_access(&applied, value)))
                    .unwrap(),
                percent
            );
            assert_eq!(preset_for(applied.get()), Some(percent));
            assert_eq!(InterfaceZoom::new(&first.0).saved_percent(), percent);
            assert_eq!(InterfaceZoom::new(&second.0).saved_percent(), 100);
        }
    }

    fn view_access(view: &Cell<f64>, set: Option<f64>) -> f64 {
        if let Some(factor) = set {
            view.set(factor);
        }
        view.get()
    }

    #[test]
    fn rejects_unsupported_presets_and_recovers_from_invalid_saved_values() {
        let root = Workspace::new();
        native_preferences::save(&root.0, FILE, &Document { percent: 200 }).unwrap();
        let zoom = InterfaceZoom::new(&root.0);
        assert_eq!(zoom.saved_percent(), 100);
        let invalid = zoom.request(Some(120), |_| panic!("invalid preset was applied"));
        assert_eq!(invalid.unwrap_err().code, "zoom_invalid_percent");
        let applied = Cell::new(1.0);
        assert_eq!(
            zoom.request(Some(125), |v| Ok(view_access(&applied, v)))
                .unwrap(),
            125
        );
        assert_eq!(InterfaceZoom::new(&root.0).saved_percent(), 125);
    }

    #[test]
    fn a_setter_that_accepts_but_does_not_apply_zoom_cannot_publish_it() {
        let root = Workspace::new();
        native_preferences::save(&root.0, FILE, &Document { percent: 110 }).unwrap();
        let zoom = InterfaceZoom::new(&root.0);
        let result = zoom.request(Some(125), |_| Ok(1.1));
        assert_eq!(result.unwrap_err().code, "zoom_apply_failed");
        assert_eq!(zoom.saved_percent(), 110);
        assert_eq!(zoom.request(None, |_| Ok(1.1)).unwrap(), 110);
    }

    #[test]
    fn restores_previous_zoom_on_save_failure_and_can_retry() {
        let root = Workspace::new();
        let zoom = InterfaceZoom::new(&root.0);
        let blocked = root.0.join("runtime").join(FILE);
        fs::create_dir_all(&blocked).unwrap();

        let applied = Cell::new(1.0);
        let access = |set| Ok(view_access(&applied, set));
        assert_eq!(
            zoom.request(Some(80), access).unwrap_err().code,
            "zoom_save_failed"
        );
        assert_eq!(zoom.request(None, access).unwrap(), 100);
        assert_eq!(fs::read_dir(root.0.join("runtime")).unwrap().count(), 1);
        fs::remove_dir(&blocked).unwrap();
        assert_eq!(zoom.request(Some(80), access).unwrap(), 80);
        assert_eq!(InterfaceZoom::new(&root.0).saved_percent(), 80);
    }
}
