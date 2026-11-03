/** Codes the API can return, mapped to the copy the interface shows.
 *
 * The backend writes `{ code, message, detail }` on every recognized failure,
 * where `message` is the English text the API used to send and `detail` is the
 * same string for clients that have not migrated. Reading `code` lets the
 * interface answer in the user's language instead of echoing that English.
 *
 * An unknown code must never lose the server's text: `errorMessage()` falls back
 * to the message it arrived with, which is why adding a code here is optional
 * and never safety-critical.
 */

import type { MessageKey } from "../i18n/catalog";

export const errorCodeMessages: Record<string, MessageKey> = {
  project_not_found: "error.projectNotFound",
  project_busy: "error.projectBusy",
  queue_unavailable: "error.queueUnavailable",
  export_not_found: "error.exportNotFound",
  source_not_ready: "error.sourceNotReady",
  settings_conflict: "error.settingsConflict",
  chapter_not_found: "error.chapterNotFound",
  segment_not_found: "error.segmentNotFound",
  invalid_configuration: "error.invalidConfiguration",
  unsupported_client: "error.unsupportedClient",
  source_too_large: "error.sourceTooLarge",
  // A validation rejection (400/422) has no stable code of its own; the envelope only
  // carries a field-error list, so the UI needs its own copy for that shape.
  validation_failed: "error.validationFailed",
  // Raised by the client itself when a request exceeds its deadline; the server
  // never sends it, so it has no backend envelope behind it.
  request_timeout: "error.requestTimeout",
};

/** The HTTP failure shape `decodeResponse` produces. */
export type ApiFailure = Error & {
  status?: number;
  code?: string;
  detail?: string;
};

/** Anything that already carries the envelope fields, with or without a status.
 *
 * A progressed run reports its failure over the socket as plain JSON rather than
 * as a thrown response, so the same decoding has to accept that shape. Requiring
 * an `Error` instance would send those frames to the string fallback and render
 * an object as `[object Object]` instead of the localized copy.
 */
export type ErrorEnvelope = {
  status?: number;
  code?: string;
  detail?: string;
  message?: string;
};

export function isErrorEnvelope(error: unknown): error is ErrorEnvelope {
  if (!error || typeof error !== "object") return false;
  const candidate = error as ErrorEnvelope;
  // A numeric status means a real HTTP response; otherwise the code or detail
  // is what makes the value an envelope rather than an arbitrary object.
  return (
    typeof candidate.status === "number" ||
    typeof candidate.code === "string" ||
    typeof candidate.detail === "string"
  );
}

/** A failure with a numeric status: a request that actually reached a response. */
export function isApiFailure(error: unknown): error is ApiFailure {
  return error instanceof Error && typeof (error as ApiFailure).status === "number";
}

/**
 * Resolve what to show for a failed request.
 *
 * A known code wins: it has deliberate localized copy. Anything else keeps the
 * server text (`detail`, then `message`), so a new code degrades to a readable
 * sentence rather than a bare number.
 */
export function errorMessage(
  error: unknown,
  translate: (key: MessageKey, values?: Record<string, unknown>) => string,
): string {
  if (isErrorEnvelope(error)) {
    const key = error.code ? errorCodeMessages[error.code] : undefined;
    if (key) return translate(key);
    if (error.detail) return error.detail;
    if (error.message) return error.message;
  }
  return error instanceof Error ? error.message : String(error);
}
