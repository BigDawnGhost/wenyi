import { useI18n } from "@/i18n";
import { useState } from "react";
import { Navigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, isProjectBusy, type ReviewRun } from "@/lib/api";
import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { StatusBadge } from "@/components/StatusBadge";
import { Disclosure } from "@/components/ui/disclosure";
import { ReviewIssues } from "./ReviewIssues";
import { ErrorNotice, StructuredData } from "@/components/ui/data";

export default function ReviewPage() {
  const { t: tr, locale } = useI18n();
  const { pid = "" } = useParams();
  const qc = useQueryClient();
  const [selected, setSelected] = useState<string>();
  const [autofix, setAutofix] = useState<boolean>();
  const project = useQuery({
    queryKey: ["project", pid],
    queryFn: () => api.getProject(pid),
    refetchInterval: 3000,
  });
  const config = useQuery({
    queryKey: ["config", pid],
    queryFn: () => api.getConfig(pid),
  });
  const subtitle = project.data?.fmt === "srt";
  const chapters = useQuery({
    queryKey: ["chapters", pid],
    queryFn: () => api.listChapters(pid),
    enabled: !subtitle,
    refetchInterval: 4000,
  });
  const runs = useQuery({
    queryKey: ["review-runs", pid],
    queryFn: () => api.listReviewRuns(pid),
    enabled: !subtitle,
    refetchInterval: 3000,
  });
  const rid = selected || runs.data?.[0]?.id;
  const run = useQuery({
    queryKey: ["review-run", pid, rid],
    queryFn: () => api.getReviewRun(pid, rid!),
    enabled: !!rid && !subtitle,
    refetchInterval: isProjectBusy(project.data?.status) ? 3000 : false,
  });
  const busy = isProjectBusy(project.data?.status);
  const configuredAutofix = Boolean(
    (config.data?.effective.pipeline as Record<string, unknown> | undefined)
      ?.review_autofix ?? true,
  );
  const allowAutofix = autofix ?? configuredAutofix;
  const review = useMutation({
    mutationFn: () => api.runAiReview(pid, { autofix: allowAutofix }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["project", pid] });
      qc.invalidateQueries({ queryKey: ["review-runs", pid] });
      toast.success(tr("review.wholeBookReviewSubmitted"));
    },
  });
  const translated =
    !!chapters.data?.length && chapters.data.every((c) => c.status === "done");
  if (subtitle) return <Navigate to={`/projects/${pid}/subtitles`} replace />;
  return (
    <>
      <PageHeader
        title={tr("common.wholeBookReview")}
        subtitle={tr("review.inspectReviewIssuesEvidenceSuggestedRevisionsAnd")}
      />
      <PageContainer className="space-y-4">
        <ErrorNotice
          error={
            project.error ||
            config.error ||
            chapters.error ||
            runs.error ||
            run.error ||
            review.error
          }
        />
        <Card>
          <CardContent className="p-5 space-y-4">
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={allowAutofix}
                disabled={busy || review.isPending}
                onChange={(e) => setAutofix(e.target.checked)}
              />
              {tr("review.applyAutofixesToTheSavedTranslationAfter")}
            </label>
            <p className="text-sm text-muted-foreground">
              {tr("review.completedResultsAreReusedWhenContentConfiguration")}
            </p>
            <Button
              disabled={!translated || busy || review.isPending}
              onClick={() => review.mutate()}
            >
              {review.isPending
                ? tr("common.submitting")
                : tr("review.runWholeBookReview")}
            </Button>
            {!translated && (
              <p className="text-sm text-muted-foreground">
                {tr("review.wholeBookReviewIsAvailableOnceAll")}
              </p>
            )}
            {busy && (
              <p role="status" className="text-sm text-muted-foreground">
                {tr("review.aProjectTaskIsRunningReviewResults")}
              </p>
            )}
          </CardContent>
        </Card>
        <Disclosure
          title={tr("review.reviewRuns")}
          summary={tr("review.runCount", { count: runs.data?.length || 0 })}
          error={runs.error}
        >
          {!runs.data?.length && (
            <p className="text-sm text-muted-foreground">
              {tr("review.noReviewYet")}
            </p>
          )}
          {runs.data?.map((r) => (
            <button
              key={r.id}
              onClick={() => setSelected(r.id)}
              className={`w-full text-left rounded border p-3 text-sm ${rid === r.id ? "border-primary bg-accent" : "hover:bg-muted"}`}
            >
              <div className="break-all">
                {r.created_at
                  ? new Date(r.created_at).toLocaleString(locale)
                  : r.id}
              </div>
              <StatusBadge status={r.status} context="review" />
            </button>
          ))}
        </Disclosure>
        {selected && selected !== runs.data?.[0]?.id && (
          <div className="flex items-center gap-3 text-sm">
            <span>{tr("review.viewingHistory")}</span>
            <Button variant="outline" onClick={() => setSelected(undefined)}>
              {tr("review.latestResult")}
            </Button>
          </div>
        )}
        <Card>
          <CardContent className="p-5 space-y-5">
            {run.data ? (
              <RunDetail key={run.data.id} run={run.data} />
            ) : (
              <p className="text-sm text-muted-foreground">
                {tr("review.selectARunToViewResultsAn")}
              </p>
            )}
          </CardContent>
        </Card>
      </PageContainer>
    </>
  );
}

function RunDetail({ run }: { run: ReviewRun }) {
  const { t: tr } = useI18n();
  return (
    <>
      <div className="flex justify-between gap-3">
        <h2 className="font-medium break-all">
          {tr("common.review")}
          {run.id}
        </h2>
        <StatusBadge status={run.status} context="review" />
      </div>
      <section>
        <h3 className="font-medium mb-3">{tr("review.runSummary")}</h3>
        <ReviewSummary summary={run.summary} />
      </section>
      <section>
        <h3 className="font-medium mb-3">{tr("review.issuesEvidence")}</h3>
        <ReviewIssues
          issues={run.issues}
          empty={
            run.status === "completed"
              ? tr("review.noIssuesRecordedInThisReview")
              : tr("review.noIssuesRecordedYetTheRunMay")
          }
        />
      </section>
      <Disclosure
        title={tr("common.suggestedChanges")}
        summary={run.changes.length}
      >
        <StructuredData value={run.changes} />
      </Disclosure>
      <Disclosure title={tr("review.autofixPublicationRecords")}>
        <StructuredData
          value={Object.fromEntries(
            Object.entries(run.autofix || {}).filter(
              ([key]) => key !== "index",
            ),
          )}
          empty={tr("review.noPublicationRecordsYet")}
        />
      </Disclosure>
      <details>
        <summary className="cursor-pointer text-sm text-muted-foreground">
          {tr("review.fullRunCheckpointDetails")}
        </summary>
        <pre className="mt-3 text-xs whitespace-pre-wrap break-all overflow-auto max-h-96">
          {JSON.stringify(run, null, 2)}
        </pre>
      </details>
    </>
  );
}

function ReviewSummary({ summary }: { summary: Record<string, unknown> }) {
  const { t: tr } = useI18n();
  const keys: [string, string][] = [
    ["issue_count", tr("common.reviewIssues")],
    ["change_count", tr("common.suggestedChanges")],
    ["conflict_count", tr("review.conflicts")],
    ["review_round_count", tr("common.reviewRounds")],
    ["autofix_applied_segment_count", tr("review.fixedParagraphs")],
    ["autofix_failed_issue_count", tr("review.failedFixes")],
  ];
  return (
    <div className="space-y-3">
      <dl className="flex flex-wrap gap-x-6 gap-y-3 border-y py-3">
        {keys.map(([key, label]) => (
          <div key={key} className="flex items-baseline gap-2">
            <dt className="text-xs text-muted-foreground">{label}</dt>
            <dd className="font-semibold">
              {typeof summary[key] === "number" ? String(summary[key]) : "—"}
            </dd>
          </div>
        ))}
      </dl>
      <details>
        <summary className="cursor-pointer text-xs text-muted-foreground">
          {tr("review.viewAllRunCounts")}
        </summary>
        <div className="mt-3">
          <StructuredData value={summary} />
        </div>
      </details>
    </div>
  );
}
