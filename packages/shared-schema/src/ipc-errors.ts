/**
 * Stable, machine-readable error codes for the desktop IPC boundary.
 *
 * Tauri commands fail with `{ code, message }`; pages branch on `code` and
 * fall back to a generic label only for a code they do not recognize. The
 * Rust shell mirrors this registry in `apps/desktop/src/ipc_error.rs`, and a
 * Rust unit test keeps the two lists identical.
 */
export const IpcErrorCodes = {
  ipcFailure: "ipc_failure",
  updateBusy: "update_busy",
  updateUnavailable: "update_unavailable",
  updateCheckFailed: "update_check_failed",
  updateInstallFailed: "update_install_failed",
  updateBackupFailed: "update_backup_failed",
  exportSourceUnavailable: "export_source_unavailable",
  exportRequestInvalid: "export_request_invalid",
  exportDestinationInvalid: "export_destination_invalid",
  exportDestinationChanged: "export_destination_changed",
  exportTransferInterrupted: "export_transfer_interrupted",
  exportResponseInvalid: "export_response_invalid",
  exportEngineUnavailable: "export_engine_unavailable",
  exportProtectionLimit: "export_protection_limit",
  exportClosing: "export_closing",
  exportUnavailable: "export_unavailable",
  exportFailed: "export_failed",
  sourceNotReady: "source_not_ready",
  dropUnsupportedFormat: "drop_unsupported_format",
  dropEmptySource: "drop_empty_source",
  dropUploadBusy: "drop_upload_busy",
  dropSelectionInvalid: "drop_selection_invalid",
  dropTooManyFiles: "drop_too_many_files",
  dropUploadFailed: "drop_upload_failed",
  dropResponseInvalid: "drop_response_invalid",
  dropUntrustedWindow: "drop_untrusted_window",
  dropEngineUnavailable: "drop_engine_unavailable",
  zoomInvalidPercent: "zoom_invalid_percent",
  zoomUntrustedWindow: "zoom_untrusted_window",
  zoomSaveFailed: "zoom_save_failed",
  zoomApplyFailed: "zoom_apply_failed",
  zoomUnavailable: "zoom_unavailable",
  libraryUnavailable: "library_unavailable",
  libraryEngineUnavailable: "library_engine_unavailable",
  libraryPathInvalid: "library_path_invalid",
  diagnosticsUntrustedWindow: "diagnostics_untrusted_window",
  diagnosticsUnavailable: "diagnostics_unavailable",
  diagnosticsBusy: "diagnostics_busy",
  diagnosticsInterrupted: "diagnostics_interrupted",
  diagnosticsCollectionFailed: "diagnostics_collection_failed",
  notificationsUntrustedWindow: "notifications_untrusted_window",
  notificationsUnavailable: "notifications_unavailable",
  notificationsSettingsUnavailable: "notifications_settings_unavailable",
} as const;

export type IpcErrorCode = (typeof IpcErrorCodes)[keyof typeof IpcErrorCodes];
