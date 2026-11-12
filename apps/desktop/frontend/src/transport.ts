import { decodeResponse, requestHeaders, sendRequest, withTimeout } from "@wenyi/ui/http";
import type { GlossaryExportFormat } from "@wenyi/ui/api/types";
import { apiBase, authToken } from "./runtime";
import { saveNativeExport, saveNativeGlossary } from "./nativeExport";
import { reportDiagnostic } from "./diagnostics";

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  try {
    return await sendRequest<T>(
      () => fetch(`${apiBase()}${path}`, withTimeout({
        ...init, headers: requestHeaders(init, authToken()),
      })),
      async (answer) => {
        try {
          return await decodeResponse<T>(answer);
        } catch (error) {
          const errorId = answer.headers.get("X-Error-ID");
          if (error instanceof Error && errorId && /^[0-9a-f]{32}$/.test(errorId))
            error.message += ` [${errorId}]`;
          throw error;
        }
      },
    );
  } catch (error) {
    reportDiagnostic("frontend_request_failed");
    throw error;
  }
}

/** Webview downloads must use native saving, never browser-only blob anchors. */
export async function download(path: string, _fallback: string) {
  const glossary = path.match(/^\/projects\/([\w-]+)\/glossary\/export\?format=(json|csv|apkg)$/);
  if (glossary)
    return saveNativeGlossary(glossary[1], glossary[2] as GlossaryExportFormat);
  const book = path.match(/^\/projects\/([\w-]+)\/exports\/(\d+)\/download$/);
  if (book)
    return saveNativeExport(book[1], {}, Number(book[2]));
  throw new Error("This download does not support native saving.");
}
