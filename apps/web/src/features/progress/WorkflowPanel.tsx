import { StatusBadge } from "@/components/StatusBadge";
import { useI18n } from "@/i18n";
import { workflowStageLabel } from "@/i18n/labels";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ProgressMessage } from "@/lib/ws";
import { Disclosure } from "@/components/ui/disclosure";
import { Card, CardContent } from "@/components/ui/card";
import { ErrorNotice } from "@/components/ui/data";

export function WorkflowPanel({
  pid,
  msg,
}: {
  pid: string;
  msg: ProgressMessage | null;
}) {
  const { t: tr } = useI18n();
  const TASKS: Record<string, string> = {
    parse: tr("workflowPanel.sourceParsing"),
    prepare: tr("common.preparation"),
    translation: tr("workflowPanel.bookTranslation"),
    chapter_translation: tr("workflowPanel.chapterTranslation"),
    review: tr("common.wholeBookReview"),
    srt: tr("workflowPanel.subtitleTranslation"),
  };

  const query = useQuery({
    queryKey: ["workflow", pid],
    queryFn: () => api.getWorkflow(pid),
    refetchInterval: 2500,
  });
  const workflow = query.data;
  const live =
    msg?.run_id && msg.run_id === workflow?.run_id && msg.label
      ? msg
      : workflow?.progress;
  return (
    <Card>
      <CardContent className="p-5 space-y-4">
        <div>
          <h2 className="font-medium">{tr("workflowPanel.currentWorkflow")}</h2>
        </div>
        <ErrorNotice error={query.error} />
        {workflow && (
          <>
            <div className="flex flex-wrap gap-3 text-sm">
              <strong>{TASKS[workflow.kind] || workflow.kind}</strong>
              <StatusBadge status={workflow.status} />
            </div>
            <div role="status" className="rounded border p-3 text-sm">
              <span className="text-muted-foreground">
                {tr("workflowPanel.latestStage")}
              </span>
              {live?.label
                ? String(live.label)
                : tr("workflowPanel.waitingForProgressUpdates")}
              {live && Number(live.total) > 0 && (
                <span className="ml-2">
                  {tr("workflow.stageCount", {
                    done: Number(live.done) || 0,
                    total: Number(live.total),
                  })}
                </span>
              )}
            </div>
            <Disclosure title={tr("progress.workflowDetails")}>
              <p className="text-xs text-muted-foreground mt-1">
                {workflow?.source === "snapshot"
                  ? tr("workflowPanel.thisIsTheWorkflowFromTheLatest")
                  : tr(
                      "workflowPanel.showingTheCurrentProjectConfigurationNoTask",
                    )}
              </p>
              <ol className="space-y-2">
                {workflow.stages.map((stage, index) => (
                  <li
                    key={stage.id}
                    className={`flex items-center gap-4 py-2 ${stage.enabled ? "" : "text-muted-foreground"}`}
                  >
                    <div className="text-xs text-muted-foreground">
                      {index + 1} ·{" "}
                      {stage.enabled
                        ? tr("common.enabled")
                        : tr("workflowPanel.disabled")}
                    </div>
                    <div className="text-sm font-medium mt-1">
                      {workflowStageLabel(stage.id, stage.label, tr)}
                    </div>
                  </li>
                ))}
              </ol>
              <p className="text-xs text-muted-foreground">
                {tr(
                  "workflowPanel.polishingRunsWithTranslationBatchesTheseCards",
                )}
              </p>
            </Disclosure>
          </>
        )}
      </CardContent>
    </Card>
  );
}
