import { StatusBadge } from "@/components/StatusBadge";
import { useI18n } from "@/i18n";
import { useParams, Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, isProjectBusy, type ChapterSummary } from "@/lib/api";
import { useProjectProgress } from "@/lib/ws";
import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import { ErrorNotice, StructuredData } from "@/components/ui/data";
import { Disclosure } from "@/components/ui/disclosure";
import { WorkflowPanel } from "./WorkflowPanel";
import { Accounting } from "./Accounting";
import { toast } from "sonner";

export default function ProgressPage() {
  const { t: tr } = useI18n();
  const { pid = "" } = useParams();
  const qc = useQueryClient();
  const projectQuery = useQuery({
    queryKey: ["project", pid],
    queryFn: () => api.getProject(pid),
    refetchInterval: 2500,
  });
  const project = projectQuery.data;
  const subtitle = project?.fmt === "srt";
  const chapterQuery = useQuery({
    queryKey: ["chapters", pid],
    queryFn: () => api.listChapters(pid),
    enabled: !!project && !subtitle,
    refetchInterval: 3000,
  });
  const { data: cues } = useQuery({
    queryKey: ["subtitles", pid],
    queryFn: () => api.getSubtitles(pid),
    enabled: subtitle,
    refetchInterval: 3000,
  });
  const report = useQuery({
    queryKey: ["report", pid],
    queryFn: () => api.getReport(pid),
    enabled: !!project && !subtitle,
  });
  const stats = useQuery({
    queryKey: ["stats", pid],
    queryFn: () => api.getStats(pid),
    enabled: !!project,
    refetchInterval: isProjectBusy(project?.status) ? 5000 : false,
  });
  const { msg, connected } = useProjectProgress(pid);
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["project", pid] });
    qc.invalidateQueries({ queryKey: ["chapters", pid] });
    qc.invalidateQueries({ queryKey: ["stats", pid] });
  };
  const action = useMutation({
    mutationFn: async (kind: "pause" | "resume" | "translate") =>
      api[kind](pid),
    onSuccess: (_, kind) => {
      invalidate();
      toast.success(
        kind === "pause"
          ? tr("progress.pauseRequestedSavingCompletedWork")
          : tr("progress.taskSubmitted"),
      );
    },
  });
  const regenerate = useMutation({
    mutationFn: () => api.regenerateReport(pid),
    onSuccess: (result) => {
      qc.setQueryData(["report", pid], result);
      toast.success(tr("progress.reportUpdated"));
    },
  });
  const chapters = chapterQuery.data || [];
  // Match CLI translation progress: segment counts, not finished chapters.
  const done = subtitle
    ? cues?.completed || 0
    : chapters.reduce((sum, c) => sum + (c.target_word_count || 0), 0);
  const total = subtitle
    ? cues?.total || 0
    : chapters.reduce((sum, c) => sum + (c.word_count || 0), 0) ||
      project?.total_word_count ||
      0;
  const busy = isProjectBusy(project?.status);
  const paused = project?.status === "paused";
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;

  return (
    <>
      <PageHeader
        title={project?.name || tr("common.translationOverview")}
        subtitle={tr("progress.languageSummary", {
          source: project?.source_lang || tr("progress.detectAutomatically"),
          target: project?.target_lang || "—",
          title: project?.title || tr("progress.waitingForSource"),
        })}
        actions={
          <>
            {!busy &&
              !paused &&
              project?.status !== "error" &&
              project?.fmt && (
                <Button
                  disabled={action.isPending}
                  onClick={() => action.mutate("translate")}
                >
                  {tr("common.startTranslation")}
                </Button>
              )}
            {busy && project?.status !== "parsing" && (
              <Button
                variant="outline"
                disabled={action.isPending || project?.status === "pausing"}
                onClick={() => action.mutate("pause")}
              >
                {tr("progress.pause")}
              </Button>
            )}
            {(paused || project?.status === "error") && (
              <Button
                disabled={action.isPending}
                onClick={() => action.mutate("resume")}
              >
                {tr("progress.resumeTask")}
              </Button>
            )}
          </>
        }
      />
      <PageContainer className="space-y-4">
        <ErrorNotice
          error={
            projectQuery.error ||
            chapterQuery.error ||
            action.error ||
            project?.error
          }
        />
        <Card>
          <CardContent className="p-5 space-y-4">
            <div className="flex flex-wrap justify-between items-center gap-3">
              <h2 className="font-medium">
                {subtitle
                  ? tr("progress.subtitleTranslationOverview")
                  : tr("common.translationOverview")}
              </h2>
              {project && <StatusBadge status={project.status} />}
            </div>
            <div className="text-sm">
              {done} / {total}
            </div>
            <Progress value={pct} />
            <div className="border-t pt-4 space-y-3">
              <h3 className="text-sm font-medium">
                {tr("progress.totalUsageRunTime")}
              </h3>
              <ErrorNotice error={stats.error} />
              <Accounting value={stats.data} />
            </div>
          </CardContent>
        </Card>
        <WorkflowPanel pid={pid} msg={msg} />
        <p className="text-xs text-muted-foreground">
          {tr("progress.progressConnection")}:{" "}
          {connected ? tr("progress.live") : tr("progress.polling")}
        </p>
        {!project?.initialized && (
          <Link
            className="inline-block text-sm text-primary underline"
            to={`/projects/new?project=${pid}`}
          >
            {tr("progress.uploadPreviewSource")}
          </Link>
        )}
        {!subtitle && (
          <>
            <Disclosure
              title={tr("progress.projectReport")}
              error={report.error || regenerate.error}
            >
              <Button
                variant="outline"
                disabled={regenerate.isPending || busy}
                onClick={() => regenerate.mutate()}
              >
                {tr("progress.updateReport")}
              </Button>
              <ErrorNotice error={report.error || regenerate.error} />
              <StructuredData
                value={
                  report.data?.summary &&
                  Object.fromEntries(
                    Object.entries(report.data.summary).filter(
                      ([key]) => !["usage", "timing"].includes(key),
                    ),
                  )
                }
              />
            </Disclosure>
            <ChapterTable pid={pid} chapters={chapters} busy={busy} />
          </>
        )}
      </PageContainer>
    </>
  );
}

function ChapterTable({
  pid,
  chapters,
  busy,
}: {
  pid: string;
  chapters: ChapterSummary[];
  busy: boolean;
}) {
  const { t: tr } = useI18n();
  const qc = useQueryClient();
  const translate = useMutation({
    mutationFn: (ci: number) => api.translateChapter(pid, ci),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["project", pid] });
      qc.invalidateQueries({ queryKey: ["chapters", pid] });
      toast.success(tr("progress.chapterTranslationStarted"));
    },
  });
  return (
    <Card>
      <CardContent className="p-0 overflow-x-auto">
        <ErrorNotice error={translate.error} />
        <table className="w-full min-w-[44rem] text-sm">
          <thead className="border-b text-xs text-muted-foreground">
            <tr>
              {[
                tr("common.chapter"),
                tr("progress.sourceParagraphs"),
                tr("progress.translationStatus"),
                tr("progress.reviewStatus"),
                tr("common.actions"),
              ].map((h) => (
                <th
                  key={h}
                  className="whitespace-nowrap text-left p-3 font-medium"
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {chapters.map((c) => (
              <tr key={c.index} className="border-b last:border-0">
                <td className="w-full min-w-64 max-w-0 p-3 [overflow-wrap:anywhere]">
                  {c.title_translated?.trim() ||
                    c.title.trim() ||
                    tr("common.untitledChapter")}
                </td>
                <td className="p-3 tabular-nums">{c.word_count}</td>
                <td className="p-3">
                  <StatusBadge status={c.status} context="chapter" />
                </td>
                <td className="p-3">
                  {c.review_issue_count > 0 ? (
                    <Badge variant="warning">
                      {tr("progress.reviewIssueCount", {
                        count: c.review_issue_count,
                      })}
                    </Badge>
                  ) : ["completed", "ok", "done"].includes(
                      c.review_status || "",
                    ) ? (
                    <Badge variant="success">{tr("progress.reviewed")}</Badge>
                  ) : (
                    <Badge variant="secondary">
                      {tr("progress.notReviewed")}
                    </Badge>
                  )}
                </td>
                <td className="p-3">
                  <div className="flex flex-col items-start gap-2">
                    <Link
                      className="whitespace-nowrap text-primary underline"
                      to={`/projects/${pid}/proofreading/${c.index}`}
                    >
                      {tr("progress.manualProofreading")}
                    </Link>
                    {c.status !== "done" && (
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={busy || translate.isPending}
                        onClick={() => translate.mutate(c.index)}
                      >
                        {tr("progress.translateChapter")}
                      </Button>
                    )}
                  </div>
                </td>
              </tr>
            ))}
            {!chapters.length && (
              <tr>
                <td
                  colSpan={5}
                  className="p-8 text-center text-muted-foreground"
                >
                  {tr("progress.chaptersWillAppearAfterParsing")}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}
