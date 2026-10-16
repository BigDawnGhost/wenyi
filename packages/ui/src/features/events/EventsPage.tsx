import { useI18n, translate as tr, type MessageKey } from "@/i18n";
import { useState } from "react";
import { useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, isProjectSettled, type EventOut } from "@/lib/api";
import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Card, CardContent } from "@/components/ui/card";
import { ErrorNotice, StructuredData } from "@/components/ui/data";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { amount, formatDuration, record } from "@/features/progress/accountingData";
import { cn } from "@/lib/utils";
import { operationLabel } from "@/i18n/labels";

// A provider that answers inside a minute is ordinary. Past that the reader is
// watching a run that is slow for a reason worth naming, and the row has to say
// so on its own rather than leaving them to compare timestamps by hand.
const SLOW_CALL_SECONDS = 60;
// The server rejects anything above 1000, so that is the whole history we can
// reach; asking for more would return an error instead of more events.
const EVENT_PAGE = 200;
const MAX_EVENTS = 1000;

type BadgeTone = "info" | "success" | "warning" | "secondary" | "destructive";

function badgeFor(type: string) {
  const BADGE: Record<string, { variant: BadgeTone; label: string }> = {
    project: { variant: "info", label: tr("events.project") },
    run_initialized: { variant: "secondary", label: tr("events.preparation") },
    language_detected: {
      variant: "secondary",
      label: tr("events.preparation"),
    },
    analysis_saved: { variant: "secondary", label: tr("events.preparation") },
    book_synopsis_saved: {
      variant: "secondary",
      label: tr("events.preparation"),
    },
    batch_translated: { variant: "info", label: tr("events.translation") },
    batch_skipped: { variant: "secondary", label: tr("events.translation") },
    chapter_done: { variant: "success", label: tr("events.completed") },
    chapter_reviewed: { variant: "warning", label: tr("common.review") },
    batch_glossary_extracted: {
      variant: "secondary",
      label: tr("common.terms"),
    },
    assembled: { variant: "success", label: tr("common.export") },
    llm_request_started: { variant: "secondary", label: tr("events.modelCall") },
    llm_request_completed: { variant: "secondary", label: tr("events.modelCall") },
    llm_request_failed: { variant: "destructive", label: tr("events.modelCall") },
    llm_request_waiting: { variant: "secondary", label: tr("events.modelWait") },
    llm_response_unusable: {
      variant: "warning",
      label: tr("events.modelCall"),
    },
    llm_retry_wait: { variant: "warning", label: tr("events.modelRetry") },
    llm_retry_exhausted: {
      variant: "destructive",
      label: tr("events.modelRetry"),
    },
  };

  return (
    BADGE[type] || {
      variant: "secondary" as const,
      label: type.split("_")[0] || tr("events.event"),
    }
  );
}

// Retry reasons are provider-reported identifiers, not interface text; each one
// that a reader can act on gets a sentence, and the rest say so plainly.
const REASONS: Record<string, MessageKey> = {
  timeout: "events.reason.timeout",
  connection: "events.reason.connection",
  empty_response: "events.reason.empty_response",
  truncated_response: "events.reason.truncated_response",
  invalid_json: "events.reason.invalid_json",
  refused: "events.reason.refused",
  rate_limited: "events.reason.rate_limited",
  server_requested_retry: "events.reason.server_requested_retry",
  insufficient_balance: "events.reason.insufficient_balance",
  authentication_failed: "events.reason.authentication_failed",
  connection_concurrency: "events.reason.connection_concurrency",
  requests_per_minute: "events.reason.requests_per_minute",
  tokens_per_minute: "events.reason.tokens_per_minute",
  token_budget: "events.reason.token_budget",
  // A provider answer arrives as an HTTP status rather than a named category.
  http_408: "events.reason.timeout",
  http_429: "events.reason.rate_limited",
};

function reasonText(reason: unknown): string {
  const key = typeof reason === "string" ? reason : "";
  const message = REASONS[key] ?? "events.reason.unknown";
  if (message !== "events.reason.unknown" || !key.startsWith("http_")) return tr(message);
  return tr("events.reason.httpStatus", { code: key.slice(5) });
}

function callSeconds(e: EventOut): number | undefined {
  return amount(record(e.payload).attempt_seconds);
}

function describe(e: EventOut, duration: string | undefined): string {
  const p = record(e.payload);
  const operation = operationLabel(
    typeof p.operation === "string" ? p.operation : "",
    tr,
  );
  switch (e.type) {
    case "language_detected":
      return tr("events.sourceLanguageDetected", { language: p.source_lang });
    case "analysis_saved":
      return tr("events.styleAnalysisCompleted");
    case "book_synopsis_saved":
      return tr("events.bookSynopsisGenerated");
    case "batch_translated":
      return tr("events.chapterBatchCompletedParagraphs", {
        chapter: (Number(p.chapter) ?? 0) + 1,
        count: p.count,
      });
    case "batch_skipped":
      return tr("events.chapterTranslatedBatchSkipped", {
        chapter: (Number(p.chapter) ?? 0) + 1,
      });
    case "chapter_done":
      return tr("events.chapterTranslated", {
        chapter: (Number(p.chapter) ?? 0) + 1,
        title: p.title,
      });
    case "chapter_reviewed":
      return tr("events.chapterReviewIssues", {
        chapter: (Number(p.chapter) ?? 0) + 1,
        count: p.issue_count,
      });
    case "assembled":
      return tr("events.exportedFiles", {
        files: (p.outputs as string[])?.join(", ") || "",
      });
    case "run_initialized":
      return tr("events.projectInitializedChapters", { count: p.chapters });
    case "llm_request_completed":
      return tr("events.modelCallCompleted", { operation, duration: duration ?? "" });
    case "llm_request_failed":
      return tr("events.modelCallFailed", { operation, duration: duration ?? "" });
    case "llm_request_waiting":
      return tr("events.modelCallWaiting", {
        operation,
        reason: reasonText(p.reason),
      });
    case "llm_retry_wait":
      return tr("events.modelRetryWaiting", {
        operation,
        reason: reasonText(p.reason),
        failed: Number(p.failed_attempt) || 1,
        total: Number(p.max_attempts) || 0,
        wait: duration ?? "",
      });
    case "llm_retry_exhausted":
      return tr("events.modelRetriesExhausted", {
        operation,
        attempts: Number(p.attempts) || 0,
        reason: reasonText(p.reason),
      });
    case "llm_response_unusable":
      return tr("events.modelReplyUnusable", { operation });
    default:
      return e.type;
  }
}

export default function EventsPage() {
  const { t: tr, locale } = useI18n();
  const { pid = "" } = useParams();
  // The log is the only place a long provider call is visible, and it is also
  // the heaviest page to reload: a busy book's log runs to megabytes. Start at
  // the server's own page size and let the reader ask for more.
  const [limit, setLimit] = useState(EVENT_PAGE);
  const project = useQuery({
    queryKey: ["project", pid],
    queryFn: () => api.getProject(pid),
    staleTime: 10_000,
  });
  const { data: events, error } = useQuery({
    queryKey: ["events", pid, limit],
    queryFn: () => api.listEvents(pid, undefined, limit),
    select: (items) => [...items].sort((a, b) => b.id - a.id),
    enabled: !!pid,
    // A settled book's log only grows when the reader runs something.
    refetchInterval: isProjectSettled(project.data?.status) ? false : 5000,
  });

  return (
    <>
      <PageHeader
        title={tr("common.eventLog")}
        subtitle={tr("events.keyProjectEventsRefreshedEvery5Seconds")}
      />
      <PageContainer>
        <ErrorNotice error={error} />
        <Card>
          <CardContent className="p-4">
            {!events?.length ? (
              <p className="text-sm text-muted-foreground text-center py-8">
                {tr("events.noEventsYet")}
              </p>
            ) : (
              <div className="space-y-2">
                {events.map((e) => {
                  const b = badgeFor(e.type);
                  const seconds = callSeconds(e);
                  const slow = seconds !== undefined && seconds >= SLOW_CALL_SECONDS;
                  // A retry wait reports a pause between attempts, not a provider
                  // round trip, so it carries its own number rather than attempt_seconds.
                  const waitSeconds =
                    e.type === "llm_retry_wait"
                      ? amount(record(e.payload).wait_seconds)
                      : undefined;
                  const duration =
                    waitSeconds !== undefined
                      ? formatDuration(waitSeconds, tr)
                      : seconds !== undefined
                        ? formatDuration(seconds, tr)
                        : undefined;
                  return (
                    <div
                      key={e.id}
                      className={cn(
                        "flex items-start gap-3 text-sm py-1.5",
                        slow && "rounded border border-amber-500/40 bg-amber-500/5 px-2",
                      )}
                    >
                      <span className="text-xs text-muted-foreground whitespace-nowrap mt-0.5">
                        {e.created_at
                          ? new Date(e.created_at).toLocaleString(locale)
                          : ""}
                      </span>
                      <Badge variant={b.variant}>{b.label}</Badge>
                      {duration !== undefined && (
                        <Badge variant={slow ? "warning" : "outline"}>
                          {slow
                            ? tr("events.callWasSlow", { duration })
                            : tr("events.callTookTime", { duration })}
                        </Badge>
                      )}
                      <details className="min-w-0 flex-1">
                        <summary className="cursor-pointer break-words">
                          {describe(e, duration)}
                        </summary>
                        <div className="mt-3 rounded border p-3">
                          <StructuredData value={e.payload} />
                        </div>
                      </details>
                    </div>
                  );
                })}
                {limit < MAX_EVENTS && (
                  <div className="flex justify-center pt-2">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => setLimit((value) => Math.min(value * 2, MAX_EVENTS))}
                    >
                      {tr("events.loadOlderEvents")}
                    </Button>
                  </div>
                )}
              </div>
            )}
          </CardContent>
        </Card>
      </PageContainer>
    </>
  );
}
