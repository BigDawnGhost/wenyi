import type { LibraryServices, RecoveryReport } from "@wenyi/ui/platform";
import { api } from "@wenyi/ui/api";
import { asIpcError } from "./ipcError";

/** Native folder pickup: the only way a desktop can name a directory that exists. */
async function choose(): Promise<string | null> {
  const native = window.__TAURI_INTERNALS__;
  if (!native) throw new Error("Choosing a folder is unavailable.");
  try {
    return await native.invoke<string | null>("native_library_choose", {});
  } catch (error) {
    throw asIpcError(error);
  }
}

/** Ask the engine whether one folder may hold a new project. */
async function admitLocation(path: string): Promise<string> {
  return api.admitProjectLocation(path);
}

/** What the startup repair found, or null when it ran nothing worth reporting. */
async function recoveryReport(): Promise<RecoveryReport | null> {
  const answer = await api.lastRecovery();
  return answer.recovered || answer.skipped.length ? answer : null;
}

export const library: LibraryServices = { choose, importProject: api.importProject, admitLocation };
export { recoveryReport };
