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

/** Requests that must not wait forever. */
export const REQUEST_TIMEOUT_MS = {
  /** Reads are expected to be answered from the server immediately. */
  read: 30_000,
  /** Writes and long tasks legitimately take minutes. */
  write: 120_000,
  /** Exports can render a whole book before the first byte. */
  download: 600_000,
} as const;

/** A request timeout budget chosen from the method the caller is about to use. */
export function timeoutForMethod(method: string | undefined): number {
  const verb = (method ?? "GET").toUpperCase();
  return verb === "GET" || verb === "HEAD"
    ? REQUEST_TIMEOUT_MS.read
    : REQUEST_TIMEOUT_MS.write;
}

/** Bound a request so a server that never answers cannot leave it pending.
 *
 * `AbortSignal.any` keeps the caller's own cancellation working, which is why
 * this composes signals instead of replacing the one already on the init.
 */
export function withTimeout(
  init: RequestInit | undefined,
  ms: number = timeoutForMethod(init?.method),
): RequestInit {
  const signal = init?.signal
    ? AbortSignal.any([AbortSignal.timeout(ms), init.signal])
    : AbortSignal.timeout(ms);
  return { ...init, signal };
}

/** Whether an abort came from our own deadline rather than from the caller.
 *
 * Only the deadline is translated: an abort the caller asked for stays an abort,
 * so a canceled query is still cancelled rather than reported as a failure.
 */
function isTimeout(error: unknown): boolean {
  return error instanceof DOMException && error.name === "TimeoutError";
}

/** Run a request, translating an expired deadline into the `ApiError` shape. */
export async function sendRequest<T>(
  request: () => Promise<Response>,
  decode: (response: Response) => Promise<T> = decodeResponse,
): Promise<T> {
  let response: Response;
  try {
    response = await request();
  } catch (error) {
    // Without this the caller sees a host-specific `DOMException` that carries
    // neither a status nor a code, and no shared surface can describe it.
    if (isTimeout(error))
      throw new ApiError({ status: 0, code: "request_timeout", detail: "request_timeout" });
    throw error;
  }
  return await decode(response);
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
