// The native commands fail with `{ code, message }`, so the page branches on
// the code and shows the message. A bare string still arrives from the
// untrusted-window guard and from older builds, so it falls back to the
// previous behaviour instead of breaking.
export type IpcFailure = { code: string; message: string };

export function ipcFailure(reason: unknown): IpcFailure {
  if (typeof reason === "object" && reason !== null) {
    const candidate = reason as { code?: unknown; message?: unknown };
    if (typeof candidate.code === "string") {
      return {
        code: candidate.code,
        message:
          typeof candidate.message === "string" ? candidate.message : candidate.code,
      };
    }
  }
  return { code: "ipc_failure", message: String(reason) };
}
