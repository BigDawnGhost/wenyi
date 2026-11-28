//! Structured failures for the commands the webview calls.
//!
//! A command used to fail with a bare string, so the page could only guess at
//! what happened by searching that string for a keyword. The tag is a stable
//! code the page branches on; the detail is what a person reads. Both travel,
//! so an old page that only shows `message` behaves exactly as before.

use serde::Serialize;

/// Stable `code` values, mirrored from `packages/shared-schema/src/ipc-errors.ts`.
///
/// The webview imports that TypeScript registry directly; the shell keeps these
/// constants in sync with it through the registry sync test at the end of this
/// file, so a one-sided addition fails the build instead of shipping a code the
/// other side cannot recognize.
pub mod codes {
    pub const IPC_FAILURE: &str = "ipc_failure";
    pub const UPDATE_BUSY: &str = "update_busy";
    pub const UPDATE_UNAVAILABLE: &str = "update_unavailable";
    pub const UPDATE_CHECK_FAILED: &str = "update_check_failed";
    pub const UPDATE_INSTALL_FAILED: &str = "update_install_failed";
    pub const UPDATE_BACKUP_FAILED: &str = "update_backup_failed";
    pub const EXPORT_SOURCE_UNAVAILABLE: &str = "export_source_unavailable";
    pub const EXPORT_REQUEST_INVALID: &str = "export_request_invalid";
    pub const EXPORT_DESTINATION_INVALID: &str = "export_destination_invalid";
    pub const EXPORT_DESTINATION_CHANGED: &str = "export_destination_changed";
    pub const EXPORT_TRANSFER_INTERRUPTED: &str = "export_transfer_interrupted";
    pub const EXPORT_RESPONSE_INVALID: &str = "export_response_invalid";
    pub const EXPORT_ENGINE_UNAVAILABLE: &str = "export_engine_unavailable";
    pub const EXPORT_PROTECTION_LIMIT: &str = "export_protection_limit";
    pub const EXPORT_CLOSING: &str = "export_closing";
    pub const EXPORT_UNAVAILABLE: &str = "export_unavailable";
    pub const EXPORT_FAILED: &str = "export_failed";
    pub const DROP_UNSUPPORTED_FORMAT: &str = "drop_unsupported_format";
    pub const DROP_EMPTY_SOURCE: &str = "drop_empty_source";
    pub const DROP_UPLOAD_BUSY: &str = "drop_upload_busy";
    pub const DROP_SELECTION_INVALID: &str = "drop_selection_invalid";
    pub const DROP_TOO_MANY_FILES: &str = "drop_too_many_files";
    pub const DROP_UPLOAD_FAILED: &str = "drop_upload_failed";
    pub const DROP_RESPONSE_INVALID: &str = "drop_response_invalid";
    pub const DROP_UNTRUSTED_WINDOW: &str = "drop_untrusted_window";
    pub const DROP_ENGINE_UNAVAILABLE: &str = "drop_engine_unavailable";
    pub const ZOOM_INVALID_PERCENT: &str = "zoom_invalid_percent";
    pub const ZOOM_UNTRUSTED_WINDOW: &str = "zoom_untrusted_window";
    pub const ZOOM_SAVE_FAILED: &str = "zoom_save_failed";
    pub const ZOOM_APPLY_FAILED: &str = "zoom_apply_failed";
    pub const ZOOM_UNAVAILABLE: &str = "zoom_unavailable";
    pub const LIBRARY_UNAVAILABLE: &str = "library_unavailable";
    pub const LIBRARY_ENGINE_UNAVAILABLE: &str = "library_engine_unavailable";
    pub const LIBRARY_PATH_INVALID: &str = "library_path_invalid";
    pub const DIAGNOSTICS_UNTRUSTED_WINDOW: &str = "diagnostics_untrusted_window";
    pub const DIAGNOSTICS_UNAVAILABLE: &str = "diagnostics_unavailable";
    pub const DIAGNOSTICS_BUSY: &str = "diagnostics_busy";
    pub const DIAGNOSTICS_INTERRUPTED: &str = "diagnostics_interrupted";
    pub const DIAGNOSTICS_COLLECTION_FAILED: &str = "diagnostics_collection_failed";
    pub const NOTIFICATIONS_UNTRUSTED_WINDOW: &str = "notifications_untrusted_window";
    pub const NOTIFICATIONS_UNAVAILABLE: &str = "notifications_unavailable";
    pub const NOTIFICATIONS_SETTINGS_UNAVAILABLE: &str = "notifications_settings_unavailable";
    pub const SOURCE_NOT_READY: &str = "source_not_ready";
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize)]
pub struct IpcError {
    /// Stable, machine-readable, and safe for the page to match on.
    pub code: String,
    /// Human-readable detail. Never a code, never a provider message.
    pub message: String,
}

impl IpcError {
    pub fn new(code: impl Into<String>, message: impl Into<String>) -> Self {
        Self {
            code: code.into(),
            message: message.into(),
        }
    }
}

impl std::fmt::Display for IpcError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for IpcError {}

impl From<String> for IpcError {
    fn from(message: String) -> Self {
        Self::new(codes::IPC_FAILURE, message)
    }
}

impl From<&str> for IpcError {
    fn from(message: &str) -> Self {
        Self::new(codes::IPC_FAILURE, message)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn serializes_both_fields_for_the_page() {
        let error = IpcError::new("update_busy", "Wenyi is busy.");
        let value = serde_json::to_value(&error).unwrap();
        assert_eq!(value["code"], "update_busy");
        assert_eq!(value["message"], "Wenyi is busy.");
    }

    #[test]
    fn display_is_the_message_not_the_code() {
        // Anything that prints the error keeps showing the sentence a person
        // can act on; the code only matters to the page.
        let error = IpcError::new("update_busy", "Wenyi is busy.");
        assert_eq!(error.to_string(), "Wenyi is busy.");
    }
}
/// Guards the mirror between the shell constants and the TypeScript registry
/// the webview imports. Both directions are checked so a code cannot be added,
/// renamed, or retired on one side alone.
#[cfg(test)]
mod registry_sync {
    use super::codes;

    #[test]
    fn shell_codes_match_the_shared_typescript_registry() {
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("../../packages/shared-schema/src/ipc-errors.ts");
        let registry = std::fs::read_to_string(&path)
            .unwrap_or_else(|error| panic!("cannot read {}: {error}", path.display()));
        let shell_codes = [
            codes::IPC_FAILURE,
            codes::UPDATE_BUSY,
            codes::UPDATE_UNAVAILABLE,
            codes::UPDATE_CHECK_FAILED,
            codes::UPDATE_INSTALL_FAILED,
            codes::UPDATE_BACKUP_FAILED,
            codes::EXPORT_SOURCE_UNAVAILABLE,
            codes::EXPORT_REQUEST_INVALID,
            codes::EXPORT_DESTINATION_INVALID,
            codes::EXPORT_DESTINATION_CHANGED,
            codes::EXPORT_TRANSFER_INTERRUPTED,
            codes::EXPORT_RESPONSE_INVALID,
            codes::EXPORT_ENGINE_UNAVAILABLE,
            codes::EXPORT_PROTECTION_LIMIT,
            codes::EXPORT_CLOSING,
            codes::EXPORT_UNAVAILABLE,
            codes::EXPORT_FAILED,
            codes::DROP_UNSUPPORTED_FORMAT,
            codes::DROP_EMPTY_SOURCE,
            codes::DROP_UPLOAD_BUSY,
            codes::DROP_SELECTION_INVALID,
            codes::DROP_TOO_MANY_FILES,
            codes::DROP_UPLOAD_FAILED,
            codes::DROP_RESPONSE_INVALID,
            codes::DROP_UNTRUSTED_WINDOW,
            codes::DROP_ENGINE_UNAVAILABLE,
            codes::ZOOM_INVALID_PERCENT,
            codes::ZOOM_UNTRUSTED_WINDOW,
            codes::ZOOM_SAVE_FAILED,
            codes::ZOOM_APPLY_FAILED,
            codes::ZOOM_UNAVAILABLE,
            codes::LIBRARY_UNAVAILABLE,
            codes::LIBRARY_ENGINE_UNAVAILABLE,
            codes::LIBRARY_PATH_INVALID,
            codes::DIAGNOSTICS_UNTRUSTED_WINDOW,
            codes::DIAGNOSTICS_UNAVAILABLE,
            codes::DIAGNOSTICS_BUSY,
            codes::DIAGNOSTICS_INTERRUPTED,
            codes::DIAGNOSTICS_COLLECTION_FAILED,
            codes::NOTIFICATIONS_UNTRUSTED_WINDOW,
            codes::NOTIFICATIONS_UNAVAILABLE,
            codes::NOTIFICATIONS_SETTINGS_UNAVAILABLE,
            codes::SOURCE_NOT_READY,
        ];
        for code in shell_codes {
            assert!(
                registry.contains(&format!("\"{code}\"")),
                "{code} is missing from the shared TypeScript registry"
            );
        }
        let mut registry_codes = Vec::new();
        for line in registry.lines() {
            let trimmed = line.trim();
            if !trimmed.contains(": \"") {
                continue;
            }
            registry_codes.push(trimmed.split('"').nth(1).expect("registry value is quoted"));
        }
        assert_eq!(
            registry_codes.len(),
            shell_codes.len(),
            "the shared registry and the shell constants disagree on the code count"
        );
        for code in registry_codes {
            assert!(
                shell_codes.contains(&code),
                "{code} exists in the TypeScript registry without a shell constant"
            );
        }
    }
}
