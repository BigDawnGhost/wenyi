import { asIpcError } from "./ipcError";

/** Native diagnostics accept event names, never errors, request objects or user text. */
export type DiagnosticEvent =
  | "frontend_ready" | "frontend_error" | "frontend_rejection"
  | "frontend_render_failed" | "frontend_request_failed";

export type DiagnosticStatus = {
  version: string;
  directory: string | null;
  bytes: number;
  degraded: boolean;
  dropped: number;
  detailSeconds: number;
  lastErrorId: string | null;
};

export async function diagnosticCommand<T>(action: string, args: Record<string, unknown> = {}): Promise<T> {
  if (!window.__TAURI_INTERNALS__) throw new Error("Native diagnostics unavailable");
  try {
    return await window.__TAURI_INTERNALS__.invoke<T>(`desktop_diagnostics_${action}`, args);
  } catch (error) {
    throw asIpcError(error);
  }
}

const recent = new Map<DiagnosticEvent, number>();
export function reportDiagnostic(event: DiagnosticEvent) {
  const now = performance.now();
  if (now - (recent.get(event) ?? -Infinity) < 1000) return;
  recent.set(event, now);
  // Failure of the diagnostic transport must not recursively report itself.
  void diagnosticCommand("record", { event }).catch(() => {});
}

export function installDiagnostics() {
  window.addEventListener("error", () => reportDiagnostic("frontend_error"));
  window.addEventListener("unhandledrejection", () => reportDiagnostic("frontend_rejection"));
  reportDiagnostic("frontend_ready");
}
