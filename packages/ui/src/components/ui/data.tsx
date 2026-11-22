import { useI18n, type MessageKey } from "../../i18n";
import { statusLabel } from "../../i18n/status";
import { errorMessage } from "../../lib/error-codes";
import { cn } from "../../lib/utils";

export function ErrorNotice({
  error,
  children,
  className,
}: {
  error?: unknown;
  /** A caller-built message, for notices that are not a caught error. */
  children?: React.ReactNode;
  className?: string;
}) {
  // The hook runs before the early return so the call order stays stable.
  const { t } = useI18n();
  // Either an error to format or a caller-built message: both are this same
  // panel, and a page that had its own copy was one edit away from drifting.
  if (!error && !children) return null;
  return (
    <div
      role="alert"
      className={cn(
        "rounded-md border-l-4 border-l-destructive bg-destructive/10 p-3 text-sm text-destructive",
        className,
      )}
    >
      {children ?? errorMessage(error, t)}
    </div>
  );
}

/** Preserve nested evidence, model usage and provider errors without truncating data. */
export function StructuredData({
  value,
  empty,
  depth = 0,
}: {
  value: unknown;
  empty?: string;
  depth?: number;
}) {
  const { t: tr } = useI18n();

  if (
    value === undefined ||
    value === null ||
    (Array.isArray(value) && !value.length)
  )
    return (
      <p className="text-sm text-muted-foreground">
        {empty ?? tr("data.noRecordsYet")}
      </p>
    );
  if (typeof value !== "object")
    return (
      <span className="whitespace-pre-wrap break-words text-sm">
        {typeof value === "boolean"
          ? value
            ? tr("data.yes")
            : tr("data.no")
          : String(value)}
      </span>
    );
  if (Array.isArray(value))
    return (
      <div className="space-y-3">
        {value.map((item, i) => (
          <div key={i} className="rounded border border-subtle p-3">
            <StructuredData value={item} depth={depth + 1} />
          </div>
        ))}
      </div>
    );
  return (
    <dl className="space-y-2 text-sm">
      {Object.entries(value as Record<string, unknown>).map(([key, item]) => (
        <div
          key={key}
          className={
            depth < 2
              ? "grid grid-cols-[minmax(100px,180px)_1fr] gap-1"
              : "space-y-1 border-l border-subtle pl-3"
          }
        >
          <dt className="break-words text-muted-foreground">
            {LABELS[key] ? tr(LABELS[key]) : key}
          </dt>
          <dd className="min-w-0">
            <StructuredData
              value={
                key === "status" && typeof item === "string"
                  ? statusLabel(item, tr)
                  : item
              }
              empty="—"
              depth={depth + 1}
            />
          </dd>
        </div>
      ))}
    </dl>
  );
}

const LABELS: Record<string, MessageKey> = {
  usage: "data.modelUsage",
  timing: "common.runTime",
  totals: "data.totals",
  calls: "data.calls",
  labels: "common.modelName",
  by_model: "data.byModel",
  by_provider: "data.byProvider",
  by_stage: "data.byStage",
  by_tier: "data.byTier",
  cache_hit_rate: "data.cacheHitRate",
  cache_hit_tokens: "data.cachedTokens",
  cache_miss_tokens: "accounting.legacyMiss",
  cache_write_tokens: "accounting.writtenInput",
  uncached_tokens: "accounting.uncachedInput",
  cache_unknown_tokens: "accounting.unknownInput",
  total_seconds: "data.totalTimeSeconds",
  runs: "data.runs",
  schema_version: "data.schemaVersion",
  seconds: "data.timeSeconds",
  profile: "data.modelConfiguration",
  records: "data.publicationRecords",
  enabled: "common.enabled",
  issue_count: "data.reviewIssueCount",
  patch_count: "data.revisionPatchCount",
  change_count: "data.suggestedChangeCount",
  clean_streak: "data.consecutiveCleanConfirmations",
  conflict_count: "data.conflictCount",
  fix_round_count: "data.revisionRounds",
  review_round_count: "common.reviewRounds",
  blocked_issue_count: "data.blockedIssues",
  initial_issue_count: "data.initialIssues",
  fallback_agent_count: "data.fallbackChecks",
  dismissed_issue_count: "data.dismissedIssues",
  shadow_override_count: "data.shadowRevisions",
  unresolved_conflict_count: "data.unresolvedConflicts",
  autofix_failed_issue_count: "data.failedAutofixIssues",
  not_rereported_patch_count: "data.revisionsNotReportedAgain",
  pre_arbitration_issue_count: "data.issuesBeforeArbitration",
  arbitration_superseded_count: "data.arbitrationReplacements",
  autofix_applied_segment_count: "data.autofixedParagraphs",
  failed_issue_count: "data.failedFixes",
  failed_record_count: "data.failedPublications",
  applied_change_count: "data.appliedChanges",
  applied_segment_count: "data.fixedParagraphs",
  applied_issue_fix_count: "data.fixedIssues",

  status: "common.status",
  summary: "common.summary",
  issues: "data.issues",
  changes: "common.suggestedChanges",
  autofix: "data.autofix",
  type: "common.type",
  severity: "data.severity",
  detail: "data.details",
  explanation: "data.details",
  suggestion: "data.suggestion",
  source: "common.source",
  target: "common.translation",
  target_before: "data.beforeRevision",
  target_after: "data.afterRevision",
  before: "data.beforeRevision",
  after: "data.afterRevision",
  chapter_index: "data.chapterIndex",
  segment_index: "data.paragraphIndex",
  reason: "data.reason",
  evidence: "data.evidence",
  error: "data.error",
  model: "data.model",
  provider: "common.provider",
  operation: "common.actions",
  prompt_tokens: "data.inputTokens",
  completion_tokens: "data.outputTokens",
  total_tokens: "data.totalTokens",
  cost: "data.cost",
  total_cost: "data.totalCost",
  duration: "data.time",
  elapsed_seconds: "data.timeSeconds",
  chapters_total: "data.totalChapters",
  chapters_done: "data.completedChapters",
  chapters_reviewed: "data.reviewedChapters",
  terms: "common.terms",
  open_conflicts: "data.openConflicts",
  review_issues: "common.reviewIssues",
  empty_targets: "data.emptyTranslations",
  output: "data.output",
  results: "data.results",
  published: "data.published",
  failed: "common.failed",
  skipped: "data.skipped",
  created_at: "common.created",
};
