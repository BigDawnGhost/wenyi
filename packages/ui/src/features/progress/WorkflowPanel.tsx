import { useId } from "react";
import { StatusBadge } from "../../components/StatusBadge";
import { useI18n } from "../../i18n";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import type { ProgressMessage } from "../../lib/ws";
import { ErrorNotice } from "../../components/ui/data";
import { progressInterval } from "../../lib/runtime";

export function WorkflowPanel({
  pid,
  msg,
  connected = false,
  busy,
}: {
  pid: string;
  msg: ProgressMessage | null;
  connected?: boolean;
  busy: boolean;
}) {
  const { t: tr } = useI18n();
  const headingId = useId();
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
    refetchInterval: busy ? progressInterval(connected, 2500) : false,
  });
  const workflow = query.data;
  const cached = workflow?.progress;
  const cachedIsNewer =
    typeof cached?.updated_at === "string" &&
    !!msg?.updated_at &&
    Date.parse(cached.updated_at) > Date.parse(msg.updated_at);
  const live =
    msg?.run_id &&
    msg.run_id === workflow?.run_id &&
    msg.label &&
    !cachedIsNewer
      ? msg
      : cached;
  const showProgress =
    workflow &&
    !["not_started", "queued", "done", "completed"].includes(workflow.status) &&
    (!!live?.label || workflow.status === "running");

  return (
    <section aria-labelledby={headingId} className="space-y-3 py-2">
      <h2 id={headingId} className="font-medium">
        {tr("workflowPanel.currentWorkflow")}
      </h2>
      <ErrorNotice error={query.error} />
      {workflow && (
        <div role="status" aria-atomic="true" className="space-y-2 text-sm">
          <div className="flex flex-wrap items-center gap-3">
            <span className="font-medium">{TASKS[workflow.kind] || workflow.kind}</span>
            <StatusBadge status={workflow.status} />
          </div>
          {showProgress && (
            <p className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
              <span className="min-w-0 [overflow-wrap:anywhere]">
                {live?.label
                  ? String(live.label)
                  : tr("workflowPanel.waitingForProgressUpdates")}
              </span>
              {!!live?.label && Number(live.total) > 0 && (
                <span className="shrink-0 tabular-nums text-muted-foreground">
                  {tr("workflow.stageCount", {
                    done: Number(live.done) || 0,
                    total: Number(live.total),
                  })}
                </span>
              )}
            </p>
          )}
        </div>
      )}
    </section>
  );
}
