import type { LibraryServices } from "@wenyi/ui/platform";
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

export const library: LibraryServices = { choose, importProject: api.importProject };
