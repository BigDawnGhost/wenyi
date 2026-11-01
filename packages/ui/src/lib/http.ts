/** Standard HTTP decoding; hosts own endpoint selection and authorization. */

// A failed request carries the backend envelope: a stable `code`, the English
// `message`, and the same text again as `detail` for clients that still read
// the old field. Keep all three on the thrown error so a caller can branch on
// the code and still show something when the code is one it does not know.
export type ApiErrorDetails = {
  status: number;
  code?: string;
  detail: string;
};

export class ApiError extends Error {
  readonly status: number;
  readonly code?: string;
  readonly detail: string;

  constructor({ status, code, detail }: ApiErrorDetails) {
    super(`${status}: ${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

export async function decodeResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let detail = response.statusText;
    let code: string | undefined;
    try {
      const json = await response.json();
      const raw = json.detail || json.message || detail;
      detail = typeof raw === "string" ? raw : JSON.stringify(raw);
      if (typeof json.code === "string") code = json.code;
    } catch {
      // Non-JSON errors still retain their HTTP status.
    }
    // The message stays "status: detail" so anything that only prints the
    // error reads exactly as it did before the envelope existed.
    throw new ApiError({ status: response.status, code, detail });
  }
  if (response.status === 204) return undefined as T;
  if ((response.headers.get("content-type") || "").includes("application/json"))
    return await response.json() as T;
  return await response.text() as T;
}

export function requestHeaders(init: RequestInit | undefined, token: string | null) {
  const headers = new Headers(init?.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init?.body && !(init.body instanceof FormData) && !headers.has("Content-Type"))
    headers.set("Content-Type", "application/json");
  return headers;
}
