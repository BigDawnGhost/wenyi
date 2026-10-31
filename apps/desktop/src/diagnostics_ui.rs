//! Native diagnostic controls work without the loopback server or the React tree.
use serde_json::json;
use std::{
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
};
use tauri::Manager;

use crate::{
    diagnostics::{Diagnostics, Event, Range, Source, Status},
    native_export::{Saved, Saves},
};

#[derive(Default)]
pub struct ExportControl {
    busy: AtomicBool,
    cancel: AtomicBool,
}

fn trusted(window: &tauri::WebviewWindow) -> Result<(), String> {
    if window.label() == "main" && window.url().is_ok_and(|url| crate::trusted(&url)) {
        Ok(())
    } else {
        Err("Desktop diagnostics are unavailable.".into())
    }
}

#[tauri::command]
pub async fn desktop_diagnostics_status(window: tauri::WebviewWindow) -> Result<Status, String> {
    trusted(&window)?;
    let diagnostics = window.state::<Diagnostics>().inner().clone();
    tauri::async_runtime::spawn_blocking(move || diagnostics.status())
        .await
        .map_err(|_| "Cannot inspect diagnostics.".into())
}

#[tauri::command]
pub fn desktop_diagnostics_record(
    window: tauri::WebviewWindow,
    event: Event,
) -> Result<Option<String>, String> {
    trusted(&window)?;
    // Renderer supplies only an enum, never arbitrary strings, frames or attributes.
    Ok(window
        .state::<Diagnostics>()
        .record(Source::Frontend, event, json!({})))
}

#[tauri::command]
pub fn desktop_diagnostics_detail(
    window: tauri::WebviewWindow,
    enabled: bool,
) -> Result<(), String> {
    trusted(&window)?;
    window.state::<Diagnostics>().detail(enabled);
    Ok(())
}

#[tauri::command]
pub fn desktop_diagnostics_cancel(window: tauri::WebviewWindow) -> Result<(), String> {
    trusted(&window)?;
    window
        .state::<Arc<ExportControl>>()
        .cancel
        .store(true, Ordering::SeqCst);
    Ok(())
}

#[tauri::command]
pub async fn desktop_diagnostics_clear(window: tauri::WebviewWindow) -> Result<(), String> {
    trusted(&window)?;
    let diagnostics = window.state::<Diagnostics>().inner().clone();
    let control = window.state::<Arc<ExportControl>>().inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let _guard = Admission::new(&control)?;
        if rfd::MessageDialog::new().set_title("Clear application logs?")
            .set_description("Only Desktop diagnostic logs will be removed. Projects, translations and usage are kept.")
            .set_buttons(rfd::MessageButtons::YesNo).show() == rfd::MessageDialogResult::Yes {
            diagnostics.clear()?;
        }
        Ok(())
    }).await.map_err(|_| "Could not clear diagnostics.".to_string())?
}

#[tauri::command]
pub async fn desktop_diagnostics_open(window: tauri::WebviewWindow) -> Result<(), String> {
    trusted(&window)?;
    let diagnostics = window.state::<Diagnostics>().inner().clone();
    tauri::async_runtime::spawn_blocking(move || {
        let path = diagnostics
            .status()
            .directory
            .ok_or("Application logs are only in memory.")?;
        #[cfg(windows)]
        let mut command = std::process::Command::new("explorer.exe");
        #[cfg(target_os = "macos")]
        let mut command = std::process::Command::new("open");
        #[cfg(not(any(windows, target_os = "macos")))]
        let mut command = std::process::Command::new("xdg-open");
        command
            .arg(path)
            .spawn()
            .map_err(|_| "Cannot open the log folder.".to_string())?;
        Ok(())
    })
    .await
    .map_err(|_| "Cannot open the log folder.".to_string())?
}

struct Admission<'a>(&'a ExportControl);
impl<'a> Admission<'a> {
    fn new(control: &'a ExportControl) -> Result<Self, String> {
        if control
            .busy
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .is_err()
        {
            return Err("A diagnostic operation is already running.".into());
        }
        control.cancel.store(false, Ordering::SeqCst);
        Ok(Self(control))
    }
}
impl Drop for Admission<'_> {
    fn drop(&mut self) {
        self.0.busy.store(false, Ordering::SeqCst);
    }
}

fn save(app: &tauri::AppHandle, range: Range) -> Result<Option<Saved>, String> {
    let control = app.state::<Arc<ExportControl>>();
    let _guard = Admission::new(&control)?;
    let diagnostics = app.state::<Diagnostics>();
    let mut roots: Vec<PathBuf> = app
        .state::<crate::Engine>()
        .workspace
        .clone()
        .into_iter()
        .collect();
    if let Some(path) = diagnostics.status().directory {
        roots.push(path.into());
    }
    // An unavailable workspace must not prevent saving a memory-only failure report.
    // Destination admission still rejects links and protects every reachable internal root.
    roots.retain(|path| path.is_dir());
    let result = crate::native_export::save_diagnostics(
        &app.state::<Saves>(),
        &roots,
        || diagnostics.bundle(range, || control.cancel.load(Ordering::SeqCst)),
        || control.cancel.load(Ordering::SeqCst),
    );
    match &result {
        Ok(Some(_)) => {
            diagnostics.record(Source::Native, Event::ExportCompleted, json!({}));
        }
        Err(_) if !control.cancel.load(Ordering::SeqCst) => {
            diagnostics.record(Source::Native, Event::ExportFailed, json!({}));
        }
        _ => {}
    }
    result
}

#[tauri::command]
pub async fn desktop_diagnostics_export(
    window: tauri::WebviewWindow,
    range: Range,
) -> Result<Option<Saved>, String> {
    trusted(&window)?;
    let app = window.app_handle().clone();
    tauri::async_runtime::spawn_blocking(move || save(&app, range))
        .await
        .map_err(|_| "Diagnostic export stopped unexpectedly.".to_string())?
}

pub fn export_from_menu(app: &tauri::AppHandle) {
    let app = app.clone();
    tauri::async_runtime::spawn_blocking(move || match save(&app, Range::Day) {
        Ok(Some(_)) => {
            rfd::MessageDialog::new().set_title("Wenyi diagnostics")
                    .set_description("Diagnostic report saved. Review it before sharing; nothing was uploaded. Missing sources are listed in manifest.json.")
                    .show();
        }
        Err(error) => {
            rfd::MessageDialog::new()
                .set_title("Wenyi diagnostics")
                .set_description(error)
                .set_level(rfd::MessageLevel::Error)
                .show();
        }
        _ => {}
    });
}

pub fn startup_failure(diagnostics: &Diagnostics) {
    diagnostics.record(Source::Native, Event::EngineFailed, json!({}));
    if rfd::MessageDialog::new().set_title("Wenyi Desktop")
        .set_description("Desktop could not start. Save a diagnostic report? No document content or credentials are included.")
        .set_buttons(rfd::MessageButtons::YesNo).show() == rfd::MessageDialogResult::Yes {
        let roots = diagnostics.status().directory.into_iter().map(PathBuf::from).collect::<Vec<_>>();
        if crate::native_export::save_diagnostics(&Saves::default(), &roots,
            || diagnostics.bundle(Range::Day, || false), || false).is_err() {
            rfd::MessageDialog::new().set_title("Wenyi Desktop")
                .set_description("Could not save the diagnostic report. Check destination permissions and free space.")
                .set_level(rfd::MessageLevel::Error).show();
        }
    }
}
