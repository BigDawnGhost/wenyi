import type { MessageKey } from "@/i18n";
import type { ReviewItem, Workflow } from "@/lib/api";
import type { ProgressMessage } from "@/lib/ws";

export const itemStatuses = {
  pending: { key: "review.pending", tone: "secondary" },
  fixed: { key: "review.fixed", tone: "success" },
  failed: { key: "review.failed", tone: "destructive" },
  unchanged: { key: "review.unchanged", tone: "secondary" },
} as const;

const phases: [RegExp, MessageKey][] = [
  [/^Loading review chapters/, "review.phaseLoading"],
  [/^Restoring review checkpoint/, "review.phaseRestoring"],
  [/^Preparing review R(\d+)/, "review.phasePreparing"],
  [/^Whole-book review R(\d+)/, "review.phaseScan"],
  [/^Blind whole-book review R(\d+)/, "review.phaseBlind"],
  [/^Conflict arbitration R(\d+)/, "review.phaseArbitration"],
  [/^Shadow revision R(\d+)/, "review.phaseRevision"],
  [/^Clean confirmation/, "review.phaseConfirm"],
  [/^Final automatic revision/, "review.phaseAutofix"],
  [/^Publishing Review Autofix/, "review.phasePublish"],
];

export function reviewPhase(label?: string) {
  for (const [pattern, key] of phases) {
    const match = label?.match(pattern);
    if (match) return { key, round: match[1] };
  }
  return undefined;
}

/** Both cached and socket events must belong to the displayed project's task. */
export function reviewProgress(
  pid: string,
  workflow: Workflow | undefined,
  socket: ProgressMessage | null,
): ProgressMessage | undefined {
  if (!workflow?.run_id) return undefined;
  const candidates = [workflow.progress, socket].filter(
    (value): value is ProgressMessage =>
      !!value && value.project_id === pid && value.run_id === workflow.run_id,
  );
  return candidates.sort(
    (a, b) =>
      (Date.parse(b.updated_at || "") || 0) -
      (Date.parse(a.updated_at || "") || 0),
  )[0];
}

const issueTypes: Record<string, MessageKey> = {
  missing: "review.typeOmission",
  omission: "review.typeOmission",
  added: "review.typeAddition",
  mistranslation: "review.typeMeaning",
  meaning: "review.typeMeaning",
  terminology: "review.typeTerm",
  term: "review.typeTerm",
  style: "review.typeStyle",
  pronoun: "review.typeReference",
  reference: "review.typeReference",
};

export function itemType(item: ReviewItem): MessageKey {
  if (item.kind === "change") return "review.itemChange";
  if (item.kind === "publication") return "review.itemPublication";
  return issueTypes[item.type || ""] || "review.typeOther";
}

export function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}
