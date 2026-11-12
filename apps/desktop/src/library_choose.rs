//! Native folder selection for the one place a person points the application at
//! a directory that already exists.
//!
//! The engine is asked to adopt a folder, and only a folder picker can name one
//! on a desktop: an HTML file input yields files, and a typed path is a guess.
//! The picker therefore runs here and returns the absolute path; nothing else
//! crosses, so this command cannot read or write the folder it names.

use std::sync::{Arc, Mutex};

use tauri::Manager;

use crate::ipc_error::IpcError;

/// Open the folder picker and return the chosen absolute path.
///
/// A cancelled picker is a normal outcome and answers None. A path that is not
/// absolute is refused rather than corrected: the engine resolves library
/// locations from it, and a relative path would resolve against whatever
/// directory the child process happens to be in.
#[tauri::command]
pub async fn native_library_choose(
    window: tauri::WebviewWindow,
) -> Result<Option<String>, IpcError> {
    if window.label() != "main" || !window.url().is_ok_and(|url| crate::trusted(&url)) {
        return Err(IpcError::new(
            "library_unavailable",
            "Choosing a folder is unavailable.",
        ));
    }
    let state = window.state::<Arc<Mutex<crate::State>>>();
    let refusing = {
        let state = crate::mutex::lock(&state);
        state.closing || state.error.is_some()
    };
    if refusing {
        return Err(IpcError::new(
            "library_engine_unavailable",
            "Local engine unavailable.",
        ));
    }
    tauri::async_runtime::spawn_blocking(move || {
        let chosen = rfd::FileDialog::new()
            .set_title("Choose a Wenyi project folder")
            .pick_folder();
        match chosen {
            None => Ok(None),
            Some(path) if path.is_absolute() => Ok(Some(path.to_string_lossy().into_owned())),
            Some(_) => Err(IpcError::new(
                "library_path_invalid",
                "That folder cannot be used.",
            )),
        }
    })
    .await
    .map_err(|_| {
        IpcError::new(
            "library_unavailable",
            "Choosing a folder stopped unexpectedly.",
        )
    })?
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_refused_path_is_reported_with_the_code_the_page_matches() {
        let error = IpcError::new("library_path_invalid", "That folder cannot be used.");
        assert_eq!(error.code, "library_path_invalid");
        assert_eq!(error.to_string(), "That folder cannot be used.");
    }
}
