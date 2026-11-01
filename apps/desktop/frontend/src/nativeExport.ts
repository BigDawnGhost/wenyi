import type { ExportOptions } from "@wenyi/ui/platform";
import type { GlossaryExportFormat } from "@wenyi/ui/api/types";

export type SavedExport = { path: string };

// The destination, API connection and credentials never enter command arguments.
// The Rust task owns the entire operation and outlives the initiating page.
export async function saveNativeExport(
  projectId: string,
  options: ExportOptions,
  exportId?: number,
): Promise<SavedExport | null> {
  if (!window.__TAURI_INTERNALS__)
    throw new Error("Native saving is unavailable.");
  try {
    return await window.__TAURI_INTERNALS__.invoke("native_export_save", {
      projectId,
      options,
      ...(exportId === undefined ? {} : { exportId }),
    });
  } catch (error) {
    throw error instanceof Error ? error : new Error(String(error));
  }
}

export async function saveNativeGlossary(
  projectId: string,
  format: GlossaryExportFormat,
): Promise<SavedExport | null> {
  if (!window.__TAURI_INTERNALS__)
    throw new Error("Native saving is unavailable.");
  try {
    return await window.__TAURI_INTERNALS__.invoke("native_glossary_save", { projectId, format });
  } catch (error) {
    throw error instanceof Error ? error : new Error(String(error));
  }
}
