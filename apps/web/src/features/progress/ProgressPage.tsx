import { projectStatusLabel } from "@/i18n/labels";
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
import { WorkflowPanel } from "./WorkflowPanel";
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
    mutationFn: async (
      kind: "pause" | "resume" | "prepare" | "translate" | "assemble",
    ) => api[kind](pid),
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
        title={project?.name || tr("common.translationProgress")}
        subtitle={tr("progress.languageSummary", {
          source: project?.source_lang || tr("progress.detectAutomatically"),
          target: project?.target_lang || "—",
          title: project?.title || tr("progress.waitingForSource"),
        })}
        actions={
          <>
            {!busy && !paused && project?.fmt && (
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
            <Link to={`/projects/${pid}/export`}>
              <Button variant="outline">{tr("common.export")}</Button>
            </Link>
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
        <WorkflowPanel pid={pid} msg={msg} />
        <div className="grid gap-3 md:grid-cols-4">
          <Stat
            label={
              subtitle
                ? tr("progress.subtitleTranslationProgress")
                : tr("common.translationProgress")
            }
            value={`${done}/${total}`}
          >
            <Progress value={pct} className="mt-2" />
          </Stat>
          <Stat
            label={tr("progress.currentStatus")}
            value={
              project
                ? projectStatusLabel(project.status, tr)
                : tr("progress.loading")
            }
          />
          <Stat
            label={tr("progress.currentStage")}
            value={busy ? msg?.label || tr("progress.waitingForProgress") : "—"}
          />
          <Stat
            label={tr("progress.progressConnection")}
            value={connected ? tr("progress.live") : tr("progress.polling")}
          />
        </div>
        <Card>
          <CardContent className="p-4 space-y-3">
            <div className="flex flex-wrap gap-2">
              {!subtitle && (
                <Button
                  variant="outline"
                  disabled={busy || !project?.fmt || action.isPending}
                  onClick={() => action.mutate("prepare")}
                >
                  {tr("common.preparation")}
                </Button>
              )}
              {!subtitle && (
                <Link to={`/projects/${pid}/review`}>
                  <Button variant="outline">
                    {tr("common.wholeBookReview")}
                  </Button>
                </Link>
              )}
              {subtitle && (
                <Link to={`/projects/${pid}/subtitles`}>
                  <Button variant="outline">
                    {tr("common.subtitleEditor")}
                  </Button>
                </Link>
              )}
              <Link to={`/projects/${pid}/settings`}>
                <Button variant="outline">
                  {tr("common.projectSettingsModels")}
                </Button>
              </Link>
              {!project?.initialized && (
                <Link to={`/projects/new?project=${pid}`}>
                  <Button variant="outline">
                    {tr("progress.uploadPreviewSource")}
                  </Button>
                </Link>
              )}
              <Button
                variant="outline"
                disabled={done === 0 || action.isPending}
                onClick={() => action.mutate("assemble")}
              >
                {tr("progress.reassembleInTheDefaultFormat")}
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">
              {tr("progress.exportsUseAConsistentSnapshotOfSaved")}
            </p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-4 space-y-3">
            <h2 className="font-medium">{tr("progress.totalUsageRunTime")}</h2>
            <ErrorNotice error={stats.error} />
            <Accounting value={stats.data} />
          </CardContent>
        </Card>
        {!subtitle && (
          <>
            <Card>
              <CardContent className="p-4 space-y-3">
                <div className="flex justify-between items-center">
                  <h2 className="font-medium">
                    {tr("progress.projectReport")}
                  </h2>
                  <Button
                    variant="outline"
                    disabled={regenerate.isPending || busy}
                    onClick={() => regenerate.mutate()}
                  >
                    {tr("progress.updateReport")}
                  </Button>
                </div>
                <ErrorNotice error={report.error || regenerate.error} />
                <StructuredData value={report.data?.summary} />
              </CardContent>
            </Card>
            <ChapterTable pid={pid} chapters={chapters} busy={busy} />
          </>
        )}
      </PageContainer>
    </>
  );
}

function Stat({
  label,
  value,
  children,
}: {
  label: string;
  value: string;
  children?: React.ReactNode;
}) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="text-xs text-muted-foreground">{label}</div>
        <div className="font-semibold mt-2 break-words">{value}</div>
        {children}
      </CardContent>
    </Card>
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
        <table className="w-full text-sm">
          <thead className="border-b text-xs text-muted-foreground">
            <tr>
              {[
                tr("common.chapter"),
                tr("progress.sourceParagraphs"),
                tr("progress.translationStatus"),
                tr("progress.reviewStatus"),
                tr("common.actions"),
              ].map((h) => (
                <th key={h} className="text-left p-3 font-medium">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {chapters.map((c) => (
              <tr key={c.index} className="border-b last:border-0">
                <td className="p-3">
                  {c.index + 1}. {c.title_translated || c.title}
                </td>
                <td className="p-3">{c.word_count}</td>
                <td className="p-3">
                  <Badge
                    variant={c.status === "done" ? "success" : "secondary"}
                  >
                    {projectStatusLabel(c.status, tr)}
                  </Badge>
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
                  {c.status === "done" ? (
                    <Link
                      className="text-primary underline"
                      to={`/projects/${pid}/review/${c.index}`}
                    >
                      {tr("progress.manualProofreading")}
                    </Link>
                  ) : (
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={busy || translate.isPending}
                      onClick={() => translate.mutate(c.index)}
                    >
                      {tr("progress.translateChapter")}
                    </Button>
                  )}
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

function Accounting({
  value,
}: {
  value?: { usage: Record<string, unknown>; timing: Record<string, unknown> };
}) {
  const { t: tr, locale } = useI18n();
  if (!value)
    return (
      <p className="text-sm text-muted-foreground">
        {tr("progress.noUsageRecordedYet")}
      </p>
    );
  const usage = value.usage || {};
  const totals = (usage.totals || usage) as Record<string, unknown>;
  const timing = value.timing || {};
  const numeric = (value: unknown) =>
    typeof value === "number" ? value.toLocaleString(locale) : "—";
  const fields: [string, string][] = [
    [tr("progress.cumulativeTokens"), numeric(totals.total_tokens)],
    [
      tr("progress.inputOutputTokens"),
      `${numeric(totals.prompt_tokens)} / ${numeric(totals.completion_tokens)}`,
    ],
    [tr("progress.modelCalls"), numeric(totals.calls)],
    [
      tr("common.runTime"),
      typeof timing.total_seconds === "number"
        ? tr("progress.seconds", { seconds: timing.total_seconds.toFixed(2) })
        : "—",
    ],
  ];
  return (
    <div className="space-y-4">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {fields.map(([label, count]) => (
          <div key={label} className="rounded border p-3">
            <div className="text-xs text-muted-foreground">{label}</div>
            <div className="font-semibold mt-2">{count}</div>
          </div>
        ))}
      </div>
      <details>
        <summary className="cursor-pointer text-sm text-muted-foreground">
          {tr("progress.viewUsageAndTimingByModelProvider")}
        </summary>
        <div className="mt-3">
          <StructuredData value={value} />
        </div>
      </details>
    </div>
  );
}
