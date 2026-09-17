import { useState } from "react";
import { Link, Navigate, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { useI18n } from "@/i18n";
import { statusLabel } from "@/i18n/status";
import { api, isProjectBusy } from "@/lib/api";
import { useProjectProgress } from "@/lib/ws";
import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/form";
import { StatusBadge } from "@/components/StatusBadge";
import { ErrorNotice } from "@/components/ui/data";
import { ReviewIssues } from "./ReviewIssues";
import { ReviewActivity } from "./ReviewActivity";
import { reviewPhase, reviewProgress } from "./reviewData";

export default function ReviewPage() {
  const { pid = "" } = useParams();
  return <ReviewWorkbench key={pid} pid={pid} />;
}

function ReviewWorkbench({ pid }: { pid: string }) {
  const { t, locale } = useI18n();
  const qc = useQueryClient();
  const [selected, setSelected] = useState<string>();
  const project = useQuery({
    queryKey: ["project", pid],
    queryFn: () => api.getProject(pid),
    refetchInterval: 3000,
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
  const historical = !!selected && selected !== runs.data?.[0]?.id;
  const busy = isProjectBusy(project.data?.status);
  const workflow = useQuery({
    queryKey: ["workflow", pid],
    queryFn: () => api.getWorkflow(pid),
    enabled: !subtitle,
    refetchInterval: 3000,
  });
  const { msg } = useProjectProgress(subtitle ? undefined : pid);
  const run = useQuery({
    queryKey: ["review-run", pid, rid],
    queryFn: () => api.getReviewRun(pid, rid!),
    enabled: !!rid && !subtitle,
    refetchInterval: !historical && busy ? 3000 : false,
  });
  const progress = reviewProgress(pid, workflow.data, msg);
  const job = workflow.data;
  const active = job?.status === "running" || job?.status === "queued";
  const reviewing =
    !historical &&
    active &&
    (job?.kind === "review" || !!reviewPhase(progress?.label));
  const sameRun = !!rid && job?.review_id === rid;
  const status =
    sameRun &&
    run.data?.status === "running" &&
    ["paused", "error", "interrupted"].includes(job?.status || "")
      ? job!.status
      : run.data?.status;
  const previousResults = reviewing && !!run.data && !sameRun;
  const showActivity =
    !historical &&
    (reviewing ||
      (sameRun &&
        (!!progress ||
          ["paused", "error", "interrupted"].includes(job?.status || ""))));
  const review = useMutation({
    mutationFn: () => api.runAiReview(pid),
    onSuccess: () => {
      for (const key of ["project", "workflow", "review-runs"])
        qc.invalidateQueries({ queryKey: [key, pid] });
      toast.success(t("review.wholeBookReviewSubmitted"));
    },
  });
  const translated =
    !!chapters.data?.length && chapters.data.every((c) => c.status === "done");
  const items = run.data?.items || [];
  const date = run.data?.created_at
    ? new Date(run.data.created_at).toLocaleString(locale)
    : undefined;
  const loading =
    project.isLoading ||
    runs.isLoading ||
    (!!rid && run.isLoading) ||
    workflow.isLoading;
  const error =
    project.error ||
    chapters.error ||
    runs.error ||
    run.error ||
    workflow.error ||
    review.error;
  if (subtitle) return <Navigate to={`/projects/${pid}/subtitles`} replace />;
  return (
    <>
      <PageHeader
        title={t("common.wholeBookReview")}
        subtitle={t("review.inspectReviewIssuesEvidenceSuggestedRevisionsAnd")}
        actions={
          <>
            {(runs.data?.length || 0) > 1 && (
              <Select
                aria-label={t("review.history")}
                className="w-full sm:w-auto sm:max-w-72"
                value={rid || ""}
                onChange={(event) =>
                  setSelected(
                    event.target.value === runs.data?.[0]?.id
                      ? undefined
                      : event.target.value,
                  )
                }
              >
                {runs.data?.map((entry) => (
                  <option key={entry.id} value={entry.id}>
                    {entry.created_at
                      ? new Date(entry.created_at).toLocaleString(locale)
                      : t("review.results")}{" "}
                    · {statusLabel(entry.status, t, "review")}
                  </option>
                ))}
              </Select>
            )}
            {!historical &&
              !busy &&
              !active &&
              translated &&
              !loading &&
              !error && (
                <Button
                  disabled={review.isPending}
                  onClick={() => review.mutate()}
                >
                  {review.isPending
                    ? t("common.submitting")
                    : t("review.runWholeBookReview")}
                </Button>
              )}
          </>
        }
      />
      <PageContainer className="space-y-5">
        <ErrorNotice error={error} />
        {historical && (
          <div className="flex flex-wrap items-center gap-3 text-sm">
            <span className="text-muted-foreground">
              {t("review.viewingHistory")}
            </span>
            <Button variant="outline" onClick={() => setSelected(undefined)}>
              {t("review.latestResult")}
            </Button>
          </div>
        )}
        {showActivity && (
          <ReviewActivity pid={pid} status={job!.status} progress={progress} />
        )}
        {!historical && busy && !reviewing && !showActivity && (
          <p className="text-sm text-muted-foreground">
            {t("review.waitingTask")}{" "}
            <Link
              className="underline underline-offset-4"
              to={`/projects/${pid}`}
            >
              {t("review.openOverview")}
            </Link>
          </p>
        )}
        {reviewing && (
          <p role="status" className="text-sm text-muted-foreground">
            {t("review.generating")}
          </p>
        )}
        {loading && (
          <p role="status" className="text-sm text-muted-foreground">
            {t("progress.loading")}
          </p>
        )}
        {run.data && (
          <section className="space-y-5 rounded-lg border p-5">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <h2 className="font-medium">
                {t(
                  previousResults
                    ? "review.previousResults"
                    : historical
                      ? "review.results"
                      : "review.currentRun",
                )}
                {date && (
                  <span className="ml-2 text-sm font-normal text-muted-foreground">
                    · {date}
                  </span>
                )}
              </h2>
              {!showActivity || previousResults ? (
                <StatusBadge status={status || "unknown"} context="review" />
              ) : null}
            </div>
            {status !== "completed" && !reviewing && (
              <p className="text-sm text-muted-foreground">
                {t("review.incomplete")}
              </p>
            )}
            {!!items.length && (
              <ReviewIssues key={run.data.id} pid={pid} items={items} />
            )}
            {!items.length &&
              status === "completed" &&
              (!reviewing || previousResults) && (
                <p className="text-sm text-muted-foreground">
                  {t("review.noIssues")}
                </p>
              )}
            <details className="border-t pt-4">
              <summary className="w-fit cursor-pointer text-xs text-muted-foreground">
                {t("review.technicalDetails")}
              </summary>
              <pre className="mt-3 max-h-96 overflow-auto whitespace-pre-wrap text-xs [overflow-wrap:anywhere]">
                {JSON.stringify(run.data, null, 2)}
              </pre>
            </details>
          </section>
        )}
        {!loading && !error && !run.data && !reviewing && (
          <div className="space-y-2 rounded-lg border p-5 text-sm text-muted-foreground">
            <p>{t("review.noReviewYet")}</p>
            {!translated && (
              <p>{t("review.wholeBookReviewIsAvailableOnceAll")}</p>
            )}
          </div>
        )}
      </PageContainer>
    </>
  );
}
