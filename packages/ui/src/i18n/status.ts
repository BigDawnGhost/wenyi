import type { createTranslator, MessageKey } from "./catalog";

type Translator = ReturnType<typeof createTranslator>;
export type StatusContext = "default" | "chapter" | "review";
type Tone = "secondary" | "success" | "warning" | "destructive" | "info";

const statuses: Record<string, [MessageKey, Tone]> = {
  created: ["api.created", "secondary"],
  uploaded: ["api.uploaded", "secondary"],
  prepared: ["api.prepared", "success"],
  ready: ["api.ready", "secondary"],
  pending: ["common.pending", "secondary"],
  not_started: ["workflowPanel.notStarted", "secondary"],
  queued: ["common.queued", "secondary"],
  running: ["common.running", "info"],
  parsing: ["api.parsing", "info"],
  preparing: ["api.preparing", "info"],
  translating: ["api.translating", "info"],
  reviewing: ["api.reviewingBook", "info"],
  autofixing: ["api.applyingAutofixes", "info"],
  postprocessing: ["api.postprocessing", "info"],
  pausing: ["api.savingAndPausing", "warning"],
  paused: ["common.paused", "warning"],
  interrupted: ["common.interrupted", "warning"],
  done: ["common.completed", "success"],
  completed: ["common.completed", "success"],
  reviewed: ["api.reviewCompleted", "success"],
  applied: ["status.applied", "success"],
  published: ["data.published", "success"],
  failed: ["common.failed", "destructive"],
  error: ["common.failed", "destructive"],
  skipped: ["data.skipped", "secondary"],
};

/** Translate presentation only; API state codes retain their original meaning. */
export function statusLabel(
  status: string,
  t: Translator,
  context: StatusContext = "default",
) {
  if (context === "chapter" && status === "pending")
    return t("status.awaitingTranslation");
  if (context === "review" && status === "running")
    return t("review.reviewing");
  return t(statuses[status]?.[0] ?? "status.unknown");
}

export function statusTone(status: string): Tone {
  return statuses[status]?.[1] ?? "secondary";
}
