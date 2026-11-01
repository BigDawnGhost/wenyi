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
};

/** The HTTP failure shape `decodeResponse` produces. */
export type ApiFailure = Error & {
  status?: number;
  code?: string;
  detail?: string;
};

export function isApiFailure(error: unknown): error is ApiFailure {
  return error instanceof Error;
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
  const failure = isApiFailure(error) ? error : undefined;
  const key = failure?.code ? errorCodeMessages[failure.code] : undefined;
  if (key) return translate(key);
  if (failure?.detail) return failure.detail;
  if (failure?.message) return failure.message;
  return String(error);
}
