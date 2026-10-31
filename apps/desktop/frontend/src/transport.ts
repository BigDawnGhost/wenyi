import { decodeResponse, requestHeaders } from "@wenyi/ui/lib/http";
import { apiBase, authToken } from "./runtime";
import { saveNativeExport, saveNativeGlossary } from "./nativeExport";

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  return decodeResponse<T>(await fetch(`${apiBase()}${path}`, {
    ...init, headers: requestHeaders(init, authToken()),
  }));
}

/** Webview downloads must use native saving, never browser-only blob anchors. */
export async function download(path: string, _fallback: string) {
  const glossary = path.match(/^\/projects\/([\w-]+)\/glossary\/export\?format=(json|csv)$/);
  if (glossary)
    return saveNativeGlossary(glossary[1], glossary[2] as "json" | "csv");
  const book = path.match(/^\/projects\/([\w-]+)\/exports\/(\d+)\/download$/);
  if (book)
    return saveNativeExport(book[1], {}, Number(book[2]));
  throw new Error("This download does not support native saving.");
}
