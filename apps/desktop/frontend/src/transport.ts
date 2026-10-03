import { decodeResponse, requestHeaders } from "@wenyi/ui/lib/http";
import { translate } from "@wenyi/ui/i18n";
import { apiBase, authToken } from "./runtime";

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  return decodeResponse<T>(await fetch(`${apiBase()}${path}`, {
    ...init, headers: requestHeaders(init, authToken()),
  }));
}

/** Ancillary artifacts (for example glossary CSV); book exports use native saving. */
export async function download(path: string, fallback: string) {
  const response = await fetch(`${apiBase()}${path}`, {
    headers: requestHeaders(undefined, authToken()),
  });
  if (!response.ok)
    throw new Error(translate("api.downloadFailed", {
      status: response.status, detail: response.statusText,
    }));
  const disposition = response.headers.get("content-disposition") || "";
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  const filename = encoded ? decodeURIComponent(encoded)
    : disposition.match(/filename="?([^";]+)"?/i)?.[1] || fallback;
  const url = URL.createObjectURL(await response.blob());
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
