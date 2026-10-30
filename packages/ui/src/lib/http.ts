/** Standard HTTP decoding; hosts own endpoint selection and authorization. */
export async function decodeResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const json = await response.json();
      const raw = json.detail || json.message || detail;
      detail = typeof raw === "string" ? raw : JSON.stringify(raw);
    } catch {
      // Non-JSON errors still retain their HTTP status.
    }
    throw new Error(`${response.status}: ${detail}`);
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
