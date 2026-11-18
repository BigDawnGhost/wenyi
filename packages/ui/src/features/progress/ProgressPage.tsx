import { StatusBadge } from "../../components/StatusBadge";
import { useI18n } from "../../i18n";
import { useParams, Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pause, Play, type LucideIcon } from "lucide-react";
import { api, isProjectBusy } from "../../api/client";
import type { ChapterSummary } from "../../api/types";
import { useProjectQuery } from "../../lib/projectQuery";
import { useProjectProgress } from "../../lib/ws";
import { PageContainer, PageHeader } from "../../components/layout/AppLayout";
import { Button } from "../../components/ui/button";
import { Card, CardContent } from "../../components/ui/card";
import { Badge } from "../../components/ui/badge";
import { Progress } from "../../components/ui/progress";
import { ErrorNotice } from "../../components/ui/data";
import { WorkflowPanel } from "./WorkflowPanel";
import { Accounting } from "./Accounting";
import { toast } from "../../lib/toast";
import { progressInterval } from "../../lib/runtime";

export default function ProgressPage() {
  const { t: tr } = useI18n();
  const { pid = "" } = useParams();
  const qc = useQueryClient();
  const { msg, failure, connected } = useProjectProgress(pid);
  const projectQuery = useProjectQuery(pid, progressInterval(connected, 2500));
  const project = projectQuery.data;
  const busy = isProjectBusy(project?.status);
  const chapterQuery = useQuery({
    queryKey: ["chapters", pid],
    queryFn: () => api.listChapters(pid),
    enabled: !!project,
    refetchInterval: busy ? progressInterval(connected, 3000) : false,
  });
  const stats = useQuery({
    queryKey: ["stats", pid],
    queryFn: () => api.getStats(pid),
    enabled: !!project,
    refetchInterval: busy
      ? progressInterval(connected, 5000)
      : false,
  });
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
  const chapters = chapterQuery.data || [];
  // Match CLI translation progress: segment counts, not finished chapters.
  const done = chapters.reduce((sum, c) => sum + (c.target_word_count || 0), 0);
  const total =
    chapters.reduce((sum, c) => sum + (c.word_count || 0), 0) ||
    project?.total_word_count ||
    0;
  const paused = project?.status === "paused";
  // Which control the header offers, as data rather than as three conditional
  // elements: the button survives the switch and keeps its hover state.
  const taskControl =
    busy && project?.status !== "parsing"
      ? {
          kind: "pause" as const,
          label: "progress.pause" as const,
          icon: Pause,
          pending: action.isPending || project?.status === "pausing",
        }
      : paused || project?.status === "error"
        ? {
            kind: "resume" as const,
            label: "progress.resumeTask" as const,
            icon: Play,
            pending: action.isPending,
          }
        : !busy && !paused && project?.status !== "error" && project?.fmt
          ? {
              kind: "translate" as const,
              label: "common.startTranslation" as const,
              icon: Play,
              pending: action.isPending,
            }
          : null;
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
          /* One button that changes role, not three that take turns being
             mounted. Swapping the element under a stationary cursor unmounts the
             hovered control and mounts a fresh one, and the replacement has never
             been hovered, so its background animates in from transparent — the
             flash. Keeping the same node keeps the hover state with it. */
          taskControl && (
            <TaskControlButton
              label={tr(taskControl.label)}
              icon={taskControl.icon}
              pending={taskControl.pending}
              onClick={() => action.mutate(taskControl.kind)}
            />
          )
        }
      />
      <PageContainer className="mx-auto w-full max-w-5xl space-y-4">
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
                {tr("common.translationOverview")}
              </h2>
              {project && <StatusBadge status={project.status} />}
            </div>
            <div className="text-sm">
              {/* Held until the chapter list arrives, so the pair does not read
                  "0 / 0" and then jump to real numbers a beat later. */}
              {chapterQuery.isPending ? "—" : `${done} / ${total}`}
            </div>
            <Progress value={pct} />
            <div className="pt-4 space-y-3">
              <h3 className="text-sm font-medium">
                {tr("progress.totalUsageRunTime")}
              </h3>
              <ErrorNotice error={stats.error} />
              <Accounting value={stats.data} running={busy} />
            </div>
          </CardContent>
        </Card>
        <WorkflowPanel
          pid={pid}
          msg={msg}
          failure={failure}
          connected={connected}
          busy={busy}
        />
        <Card className="flex flex-wrap items-center justify-between gap-2 p-4 text-xs text-muted-foreground">
          <p>
            {tr("progress.progressConnection")}:{" "}
            {connected ? tr("progress.live") : tr("progress.polling")}
          </p>
          {chapterQuery.isPending ? null : chapters.length > 0 && (
            <Link
              className="text-xs text-primary underline underline-offset-4"
              to={`/projects/${pid}/contents`}
            >
              {tr("contents.readContentsSource")}
            </Link>
          )}
        </Card>
        <ChapterTable
          pid={pid}
          chapters={chapters}
          busy={busy || !project?.initialized}
        />
      </PageContainer>
    </>
  );
}

function TaskControlButton({
  label,
  icon,
  pending,
  onClick,
}: {
  label: string;
  icon: LucideIcon;
  pending: boolean;
  onClick: () => void;
}) {
  const Idle = icon;
  return (
    <Button
      type="button"
      variant="ghost"
      size="icon"
      className="relative shrink-0 border border-border text-muted-foreground"
      aria-label={label}
      title={label}
      aria-busy={pending || undefined}
      // Not `disabled`: that applies the disabled face (half opacity, no shadow)
      // for the length of the request, so the icon dims and comes back — a blink
      // at the corner of the header. The ref guard stops the second press instead.
      onClick={() => { if (!pending) onClick(); }}
    >
      {/* Play or pause, nothing else. A spinner in between added a third glyph —
          one that also animates — for the length of the request, so the control
          flickered through a state the role never has. */}
      <Idle className="h-4 w-4" aria-hidden="true" />
    </Button>
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
        <table className="w-full min-w-176 text-sm">
          <thead className="border-b border-subtle text-xs tracking-wide text-muted-foreground">
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
                  className="whitespace-nowrap text-left px-4 py-3.5 font-medium"
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {chapters.map((c) => (
              <tr key={c.index} className="border-b border-subtle last:border-0">
                <td className="w-full min-w-64 max-w-0 px-4 py-3.5 [overflow-wrap:anywhere]">
                  {c.title_translated?.trim() ||
                    c.title.trim() ||
                    tr("common.untitledChapter")}
                </td>
                <td className="px-4 py-3.5 tabular-nums">{c.word_count}</td>
                <td className="px-4 py-3.5">
                  <StatusBadge status={c.status} context="chapter" />
                </td>
                <td className="px-4 py-3.5">
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
                <td className="px-4 py-3.5">
                  {c.status !== "done" && (
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={busy}
                      aria-busy={translate.isPending || undefined}
                      onClick={() => { if (!translate.isPending) translate.mutate(c.index); }}
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
